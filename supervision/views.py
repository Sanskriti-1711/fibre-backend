from django.db.models import Max
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.models import Feature, Project
from supervision.models import FeaturePhoto
from supervision.serializers import FeaturePhotoSerializer


def _validate_photo(upload):
    """Return an error string for an unacceptable upload, or None."""
    if upload.content_type not in FeaturePhoto.ALLOWED_CONTENT_TYPES:
        return 'Invalid file type. Only JPEG and PNG are allowed.'
    if upload.size > FeaturePhoto.MAX_BYTES:
        return 'File too large. Maximum size is 10MB.'
    return None


def _attach_photos(feature, uploads, user):
    """Append uploads to a feature's gallery.

    Returns (created_photos, error_response). Validates the whole batch first so
    a bad file in the middle does not leave a half-saved gallery.
    """
    existing = feature.photos.count()
    remaining = FeaturePhoto.MAX_PER_FEATURE - existing
    if len(uploads) > remaining:
        return None, Response(
            {
                'detail': (
                    f'Maximum {FeaturePhoto.MAX_PER_FEATURE} photos per feature. '
                    f'{existing} already uploaded, {max(remaining, 0)} slot(s) left.'
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    for upload in uploads:
        error = _validate_photo(upload)
        if error:
            return None, Response(
                {'detail': f'{upload.name}: {error}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

    next_order = feature.photos.aggregate(Max('order')).get('order__max') if existing else None
    next_order = 0 if next_order is None else next_order + 1

    created = []
    for offset, upload in enumerate(uploads):
        created.append(
            FeaturePhoto.objects.create(
                feature=feature,
                image=upload,
                uploaded_by=user if user and user.is_authenticated else None,
                order=next_order + offset,
            )
        )
    return created, None


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
            photo = FeaturePhoto.objects.get(id=photo_id, feature_id=feature_id)
        except FeaturePhoto.DoesNotExist:
            return Response(
                {'detail': 'Photo not found'},
                status=status.HTTP_404_NOT_FOUND,
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

        features = Feature.objects.filter(project_id=project_id).values(
            'id', 'layer_id', 'status'
        )

        return Response(
            {
                'project_id': str(project_id),
                'features': list(features),
            }
        )
