from django.core.exceptions import ValidationError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.api.access import engineer_can_access
from projects.api.layers import editable_feature_ids
from projects.models import Feature, Project
from projects.photo_gallery import attach_photos
from supervision import ai_measure, tape_measure
from supervision.design_change import (
    REQUEST_PENDING,
    project_requests,
    request_design_revision,
    requests_for,
)
from supervision.models import FeatureDecisionRecord, FeaturePhoto
from supervision.serializers import FeatureDecisionRecordSerializer, FeaturePhotoSerializer


def _can_edit_feature_photos(request, feature) -> bool:
    """The engineer assigned to this feature's scope, or a planner/admin.

    Reading the gallery stays open to any signed-in user (the review pages show
    the same evidence); writing follows the same assignment scopes as the map's
    edit lock. Checking only project membership would let a feature- or
    layer-scoped engineer write evidence onto every feature in the project.
    """
    if not engineer_can_access(request, feature.project):
        return False

    editable_ids = editable_feature_ids(request, feature.project)
    return editable_ids is None or str(feature.id) in editable_ids


def _attach_photos(feature, uploads, user):
    """Append uploads to a feature's gallery (projects.photo_gallery owns the rules)."""
    return attach_photos(
        photos=feature.photos,
        model=FeaturePhoto,
        owner_field='feature',
        owner=feature,
        uploads=uploads,
        user=user,
        allowed_content_types=FeaturePhoto.ALLOWED_CONTENT_TYPES,
        max_bytes=FeaturePhoto.MAX_BYTES,
        max_per_feature=FeaturePhoto.MAX_PER_FEATURE,
    )


class FeaturePhotosView(APIView):
    """GET/POST /api/features/<feature_id>/photos/

    POST accepts one or more files under the ``photos`` key (repeat the key for
    multiple files); ``photo`` is also accepted for convenience.
    """

    def get(self, request, feature_id):
        try:
            feature = Feature.objects.get(id=feature_id)
        except Feature.DoesNotExist:
            return Response(
                {'detail': 'Feature not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                'count': feature.photos.count(),
                'max_photos': FeaturePhoto.MAX_PER_FEATURE,
                'results': FeaturePhotoSerializer(
                    feature.photos.all(), many=True, context={'request': request}
                ).data,
            }
        )

    def post(self, request, feature_id):
        try:
            feature = Feature.objects.get(id=feature_id)
        except Feature.DoesNotExist:
            return Response(
                {'detail': 'Feature not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not _can_edit_feature_photos(request, feature):
            return Response(
                {'detail': 'Not allowed to add photos to this feature'},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not feature.is_inspection_editable:
            return Response(
                {
                    'detail': (
                        'This inspection has been submitted and its evidence is locked for review.'
                    ),
                    'status': feature.status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        uploads = request.FILES.getlist('photos') or request.FILES.getlist('photo')
        if not uploads:
            return Response(
                {'detail': 'No photos provided'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created, error = _attach_photos(feature, uploads, request.user)
        if error:
            return error

        return Response(
            {
                'count': feature.photos.count(),
                'max_photos': FeaturePhoto.MAX_PER_FEATURE,
                'uploaded': FeaturePhotoSerializer(
                    created, many=True, context={'request': request}
                ).data,
            },
            status=status.HTTP_201_CREATED,
        )


class FeaturePhotoDetailView(APIView):
    """DELETE /api/features/<feature_id>/photos/<photo_id>/"""

    def delete(self, request, feature_id, photo_id):
        try:
            photo = FeaturePhoto.objects.select_related('feature').get(
                id=photo_id, feature_id=feature_id
            )
        except FeaturePhoto.DoesNotExist:
            return Response(
                {'detail': 'Photo not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not _can_edit_feature_photos(request, photo.feature):
            return Response(
                {'detail': 'Not allowed to remove photos from this feature'},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not photo.feature.is_inspection_editable:
            return Response(
                {
                    'detail': (
                        'This inspection has been submitted and its evidence is locked for review.'
                    ),
                    'status': photo.feature.status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        photo.image.delete(save=False)
        photo.delete()

        return Response(
            {
                'detail': 'Photo deleted',
                'count': FeaturePhoto.objects.filter(feature_id=feature_id).count(),
            },
            status=status.HTTP_200_OK,
        )


class ProjectFeatureStatusesAPIView(APIView):
    """GET /api/projects/<project_id>/feature-statuses/

    Every feature's id and status across all layers, in one small payload. The
    project map only needs these to colour features; the per-layer detail
    endpoint repeats the full field schema per feature (MBs per layer).
    """

    def get(self, request, project_id):
        if not Project.objects.filter(id=project_id).exists():
            return Response(
                {'detail': 'Project not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        features = Feature.objects.filter(project_id=project_id).values('id', 'layer_id', 'status')

        return Response(
            {
                'project_id': str(project_id),
                'features': list(features),
            }
        )


class FeatureDecisionListView(APIView):
    """GET /api/features/<feature_id>/decisions/

    The append-only decision history of one feature: every time a reviewer
    accepted work outside the design tolerance, with the comparison as it stood
    at that moment. Read-only by design — a decision record is evidence, so
    there is deliberately no write, edit or delete route (INTEGRATION_PLAN.md
    §3 Stage 6)."""

    def get(self, request, feature_id):
        try:
            feature = Feature.objects.get(id=feature_id)
        except Feature.DoesNotExist:
            return Response(
                {'detail': 'Feature not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        records = FeatureDecisionRecord.objects.filter(feature_id=feature_id).select_related(
            'project', 'feature', 'reviewer'
        )

        return Response(
            {
                'feature_id': str(feature_id),
                'count': records.count(),
                'results': FeatureDecisionRecordSerializer(records, many=True).data,
                # The design-change loop's side of the history: which records
                # have already been taken to the LLD stage, and when. Read here
                # rather than in a second request so the audit page can label
                # each entry on first paint.
                'revision_requests': requests_for(feature),
            }
        )


class FeatureDesignRevisionView(APIView):
    """POST /api/features/<feature_id>/request-design-revision/

    The design-change loop's only write (INTEGRATION_PLAN.md §3 Stage 6.3): the
    reviewer of an accepted deviation asks for the next LLD version. It records
    the request on the LLD timeline and notifies the planners, then points at
    the LLD review page; it never creates or edits a design version itself.

    ``409`` when the feature has no approved-with-deviation record — there is
    nothing for the design stage to act on, and a request without a deviation
    would be an unexplained timeline entry. Repeating the call for the same
    record returns the first request with ``duplicate: true`` rather than
    writing a second one.
    """

    def post(self, request, feature_id):
        try:
            feature = Feature.objects.select_related('project').get(id=feature_id)
        except Feature.DoesNotExist:
            return Response(
                {'detail': 'Feature not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        note = (request.data.get('note') or '').strip()[:2000]
        payload = request_design_revision(feature, actor=request.user, note=note)
        if payload is None:
            return Response(
                {
                    'detail': (
                        'This feature has no approved deviation to revise. A design revision '
                        'is only meaningful after work was accepted outside the design.'
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            {**payload, 'requests': requests_for(feature)},
            status=status.HTTP_200_OK if payload['duplicate'] else status.HTTP_201_CREATED,
        )


class ProjectDesignRevisionRequestsView(APIView):
    """GET /api/ftth/lld/projects/<project_id>/revision-requests/

    The design-change loop read from the design side: every request raised
    against this project's designs, with the ones still waiting on a new LLD run
    first. Registered on the LLD URL space because the reader is the LLD review
    page, which is keyed by ``FtthProject.pk`` (see
    ``design_change.project_requests``).
    """

    def get(self, request, project_id):
        rows = project_requests(project_id)
        return Response(
            {
                'project_id': str(project_id),
                'count': len(rows),
                'pending_count': sum(1 for row in rows if row['status'] == REQUEST_PENDING),
                'results': rows,
            }
        )


class ProjectDecisionListView(APIView):
    """GET /api/projects/<project_id>/decisions/

    Every deviation recorded on a supervision project, newest first, so the
    project page can show what was accepted without walking the features.
    Optional ``layer`` narrows it to one layer.
    """

    def get(self, request, project_id):
        if not Project.objects.filter(id=project_id).exists():
            return Response(
                {'detail': 'Project not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        records = FeatureDecisionRecord.objects.filter(project_id=project_id).select_related(
            'project', 'feature', 'reviewer'
        )

        layer_id = (request.GET.get('layer') or '').strip()
        if layer_id:
            records = records.filter(feature__layer_id=layer_id)

        total = records.count()
        try:
            limit = max(1, min(int(request.GET.get('limit') or 100), 500))
        except ValueError:
            return Response(
                {'detail': 'limit must be an integer'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                'project_id': str(project_id),
                'count': total,
                'results': FeatureDecisionRecordSerializer(records[:limit], many=True).data,
            }
        )


class FeatureAiMeasurementView(APIView):
    """POST /api/features/<feature_id>/ai-measurements/

    Proposes dimensions from one stored photo and says where each number came
    from. Nothing is written here: the client applies the values it accepts as
    `estimated_*` readings, so a proposal can never overwrite a tape reading.
    """

    def post(self, request, feature_id):
        try:
            feature = Feature.objects.select_related('project').get(id=feature_id)
        except Feature.DoesNotExist:
            return Response(
                {'detail': 'Feature not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Estimating reads the photo and spends a model call, but it is still
        # evidence for this span: the same scopes that may add a photo may ask
        # for an estimate.
        if not _can_edit_feature_photos(request, feature):
            return Response(
                {'detail': 'Not allowed to estimate this feature'},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not feature.is_inspection_editable:
            return Response(
                {
                    'detail': (
                        'This inspection has been submitted and its evidence is locked for '
                        'review.'
                    ),
                    'status': feature.status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        element_type = str(request.data.get('element_type') or '').strip().lower()
        if element_type not in ai_measure.ELEMENT_SPECS:
            return Response(
                {'detail': 'Unknown element type'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # The photo must already be in this feature's gallery: an estimate is
        # tied to the bytes it was made from, and `FeaturePhoto` keeps the hash.
        try:
            photo = feature.photos.get(id=str(request.data.get('photo_id') or ''))
        except (FeaturePhoto.DoesNotExist, ValidationError, ValueError):
            return Response(
                {'detail': 'Photo not found on this feature'},
                status=status.HTTP_404_NOT_FOUND,
            )

        observation = ai_measure.observe(photo, element_type)
        basis, rows, note = ai_measure.estimate(feature, element_type, observation)
        return Response(
            {
                'basis': basis,
                'estimates': rows,
                'notes': note,
                'photo_id': str(photo.id),
            }
        )


class FeatureTapeMeasurementView(APIView):
    """POST /api/features/<feature_id>/tape-measurements/

    Reads the numbers printed on a tape in one stored photo and returns the
    distance between the outermost ones. Nothing is written: the app fills the
    inspection form with the reading as `actual_*`, and the engineer confirms
    it against the tape before saving.

    The OCR pass is the scale — a tape needs no known-size object and no camera
    calibration — so this route is the as-built counterpart to the estimator.
    """

    def post(self, request, feature_id):
        try:
            feature = Feature.objects.select_related('project').get(id=feature_id)
        except Feature.DoesNotExist:
            return Response(
                {'detail': 'Feature not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Reading a tape spends no model call, but it is still evidence for this
        # span: the same scopes that may add a photo may ask for a reading.
        if not _can_edit_feature_photos(request, feature):
            return Response(
                {'detail': 'Not allowed to measure this feature'},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not feature.is_inspection_editable:
            return Response(
                {
                    'detail': (
                        'This inspection has been submitted and its evidence is locked for '
                        'review.'
                    ),
                    'status': feature.status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        element_type = str(request.data.get('element_type') or '').strip().lower()
        if element_type not in tape_measure.DIMENSION_SPECS:
            return Response(
                {'detail': 'Unknown element type'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        dimension = str(request.data.get('dimension') or '').strip().lower()
        if tape_measure.dimension_spec(element_type, dimension) is None:
            return Response(
                {'detail': 'Unknown dimension for this element'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # As with an estimate, the photo must already be in this span's gallery
        # so the reading stays tied to the bytes it was made from.
        try:
            photo = feature.photos.get(id=str(request.data.get('photo_id') or ''))
        except (FeaturePhoto.DoesNotExist, ValidationError, ValueError):
            return Response(
                {'detail': 'Photo not found on this feature'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not tape_measure.ocr_available():
            return Response(
                {
                    'detail': (
                        'Tape OCR is not available on this server (tesseract is not '
                        'installed). Read the tape into the form by hand.'
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        read = tape_measure.read_tape(photo)
        if read is None:
            return Response(
                {
                    'measurement': None,
                    'readings': [],
                    'unit': 'cm',
                    'notes': (
                        'No tape numbers could be read. Lay the blade along the dimension, '
                        'keep two printed numbers in the frame and avoid glare.'
                    ),
                    'photo_id': str(photo.id),
                    'engine': 'tesseract',
                }
            )

        measurement, note = tape_measure.measure(
            element_type, dimension, read['readings'], read['unit']
        )
        bounds = read.get('bounds')
        if bounds and bounds.get('clipped'):
            # The numbers past the edge are the far side of the trench or duct,
            # not part of what was asked for: say so rather than silently
            # shortening the tape.
            note = (
                f"{note} Stopped at the {element_type} edge visible in the photo "
                f"({bounds['clipped']} number(s) past it ignored)."
            )
        return Response(
            {
                'measurement': measurement,
                'readings': read['readings'],
                'unit': read['unit'],
                'notes': note,
                'photo_id': str(photo.id),
                'engine': read['engine'],
                'bounds': bounds,
            }
        )
