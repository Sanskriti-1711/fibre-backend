import os
from typing import Any

import requests
from django.db.models import Count, Max, Q
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.api.microservice import headers as microservice_headers
from projects.layer_rules import is_field_survey_layer
from projects.models import Feature, LayerFieldConfig, Project
from projects.services import feature_deviation

from .serializers import FeatureSerializer, FeatureUpdateSerializer

# Where the import/geo service runs. Local development starts it on :8180
# (see dev/start.sh); a deployment overrides this with its own host.
MICROSERVICE_BASE_URL = os.getenv('MICROSERVICE_BASE_URL', 'http://localhost:8180')

_GEOM_NAMES = {
    'Point': 'Point',
    'MultiPoint': 'Point',
    'LineString': 'LineString',
    'MultiLineString': 'LineString',
    'Polygon': 'Polygon',
    'MultiPolygon': 'Polygon',
}


def _engineer_can_access(request, project):
    """Engineers may only access projects they are assigned to (project-scope job).

    Defense-in-depth: anonymous users are already blocked by the DRF default
    IsAuthenticated permission, but we never let a non-authenticated request
    through here regardless of role.
    """
    if not getattr(request.user, 'is_authenticated', False):
        return False
    role = getattr(request.user, 'role', None)
    if role != 'ENGINEER':
        return True
    from assignments.models import AssignmentJob

    return AssignmentJob.objects.filter(
        project=project,
        scope=AssignmentJob.SCOPE_PROJECT,
        assignee=request.user,
    ).exists()


def _deny():
    return Response(
        {'detail': 'You do not have access to this project.'},
        status=status.HTTP_403_FORBIDDEN,
    )


class ProjectMapDataAPIView(APIView):
    """GET /api/projects/<project_id>/map-data/

    Serves the same ``{layers, features, geojson}`` shape the web map expects,
    but built from the local Django Feature rows (survey packages live in the
    Django DB, not in the external import microservice).
    """

    def get(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {'detail': 'Project not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not _engineer_can_access(request, project):
            return _deny()

        features_qs = Feature.objects.filter(project=project).order_by('layer_name', 'created_at')

        layers = []
        geojson: dict[str, dict[str, Any]] = {}
        all_features = []
        seen_layers = set()
        # Planned and laid are served side by side, never merged: the design
        # baseline is immutable, so the laid copy is its own collection.
        as_built_geojson: dict[str, dict[str, Any]] = {}
        as_built_features = []

        for f in features_qs:
            if not f.geometry:
                continue
            # A Survey copy can still hold rows an older package carried; the
            # field survey does not serve them (see projects.layer_rules).
            if not is_field_survey_layer(f.layer_id or f.layer_name):
                continue
            name = f.layer_name or f.layer_id or 'layer'
            if name not in seen_layers:
                seen_layers.add(name)
                geom_type = _GEOM_NAMES.get(f.geometry.get('type', ''), 'Point')
                layers.append({'name': name, 'type': geom_type})
                geojson[name] = {'type': 'FeatureCollection', 'features': []}
            feature = {
                'type': 'Feature',
                'geometry': f.geometry,
                'properties': {
                    **(f.properties or {}),
                    'id': str(f.id),
                    'feature_id': str(f.id),
                    'layer': name,
                    'status': f.status,
                },
            }
            geojson[name]['features'].append(feature)
            all_features.append(feature)

            if f.as_built_geometry:
                as_built_geojson.setdefault(name, {'type': 'FeatureCollection', 'features': []})
                laid = {
                    'type': 'Feature',
                    'geometry': f.as_built_geometry,
                    'properties': {
                        'id': str(f.id),
                        'feature_id': str(f.id),
                        'layer': name,
                        'status': f.status,
                        'as_built': True,
                    },
                }
                as_built_geojson[name]['features'].append(laid)
                as_built_features.append(laid)

        return Response(
            {
                'project_id': str(project.id),
                'layers': layers,
                'features': all_features,
                'geojson': geojson,
                'as_built_geojson': as_built_geojson,
                'as_built_features': as_built_features,
            }
        )


class ProjectLayerListAPIView(APIView):
    """GET /api/projects/<project_id>/layers/"""

    def get(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {'detail': 'Project not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not _engineer_can_access(request, project):
            return _deny()

        features_qs = Feature.objects.filter(project=project)

        layer_summaries = (
            features_qs.values('layer_id', 'layer_name')
            .annotate(
                feature_count=Count('id'),
                pending_count=Count('id', filter=Q(status=Feature.STATUS_PENDING)),
                assigned_count=Count('id', filter=Q(status=Feature.STATUS_ASSIGNED)),
                under_review_count=Count('id', filter=Q(status=Feature.STATUS_UNDER_REVIEW)),
                approved_count=Count('id', filter=Q(status=Feature.STATUS_APPROVED)),
                redo_count=Count('id', filter=Q(status=Feature.STATUS_REDO)),
                last_feature_update=Max('updated_at'),
            )
            .order_by('layer_name')
        )

        # Layers the field survey does not serve are not listed, whatever a
        # stored copy holds: the app builds its whole layer list — panel, map
        # and feature fetches — from this answer, so filtering here reaches an
        # already-installed app without a rebuild.
        layers = [
            {
                'layer_id': entry['layer_id'],
                'layer_name': entry['layer_name'],
                'feature_count': entry['feature_count'],
                'status_counts': {
                    'pending': entry['pending_count'],
                    'assigned': entry['assigned_count'],
                    'under_review': entry['under_review_count'],
                    'approved': entry['approved_count'],
                    'redo': entry['redo_count'],
                },
                'last_feature_update': entry['last_feature_update'],
            }
            for entry in layer_summaries
            if is_field_survey_layer(entry['layer_id'])
        ]

        return Response(
            {
                'project_id': str(project.id),
                'layers': layers,
            }
        )


class ProjectLayerDetailAPIView(APIView):
    """GET /api/projects/<project_id>/layers/<layer_id>/"""

    def get(self, request, project_id, layer_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {'detail': 'Project not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not _engineer_can_access(request, project):
            return _deny()

        # A layer the field survey does not serve has no detail, exactly as an
        # unknown id does — rather than a 200 with rows the app must ignore.
        if not is_field_survey_layer(layer_id):
            return Response(
                {'detail': 'Layer not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        layer_features = Feature.objects.filter(
            project=project,
            layer_id=layer_id,
        ).order_by('created_at')

        if not layer_features.exists():
            return Response(
                {'detail': 'Layer not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        first_feature = layer_features.first()
        assert first_feature is not None

        aggregations = layer_features.aggregate(
            feature_count=Count('id'),
            pending_count=Count('id', filter=Q(status=Feature.STATUS_PENDING)),
            assigned_count=Count('id', filter=Q(status=Feature.STATUS_ASSIGNED)),
            under_review_count=Count('id', filter=Q(status=Feature.STATUS_UNDER_REVIEW)),
            approved_count=Count('id', filter=Q(status=Feature.STATUS_APPROVED)),
            redo_count=Count('id', filter=Q(status=Feature.STATUS_REDO)),
        )

        serialized_features = FeatureSerializer(layer_features, many=True).data

        return Response(
            {
                'project_id': str(project.id),
                'layer': {
                    'layer_id': layer_id,
                    'layer_name': first_feature.layer_name,
                    'feature_count': aggregations['feature_count'],
                    'status_counts': {
                        'pending': aggregations['pending_count'],
                        'assigned': aggregations['assigned_count'],
                        'under_review': aggregations['under_review_count'],
                        'approved': aggregations['approved_count'],
                        'redo': aggregations['redo_count'],
                    },
                },
                'features': serialized_features,
            }
        )


class ProjectFeatureByDesignAPIView(APIView):
    """GET /api/projects/<project_id>/features/by-design/<design_id>/

    Resolves a design (engine) feature id to the supervision row that carries
    it, together with the assignment covering that row. The design map and the
    LLD results speak in engine ids while an assignment keys on a
    ``projects.Feature`` row, so this is the one place the two are joined — the
    mapping lives in the row's own ``properties``, written by the LLD handoff.
    """

    def get(self, request, project_id, design_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {'detail': 'Project not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not _engineer_can_access(request, project):
            return _deny()

        feature = (
            Feature.objects.filter(project=project, properties__feature_id__iexact=str(design_id))
            .order_by('created_at')
            .first()
        )
        if feature is None:
            return Response(
                {
                    'detail': 'No supervision feature carries this design id.',
                    'design_id': design_id,
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        from assignments.models import AssignmentJob

        # A feature job is the specific answer; a layer job covers the whole
        # layer it sits in; a project job covers everything. Narrowest wins.
        condition = Q(scope=AssignmentJob.SCOPE_FEATURE, feature=feature) | Q(
            scope=AssignmentJob.SCOPE_PROJECT
        )
        if feature.layer_id:
            condition |= Q(scope=AssignmentJob.SCOPE_LAYER, layer_id=feature.layer_id)

        jobs = list(
            AssignmentJob.objects.select_related('assignee')
            .filter(project=project)
            .filter(condition)
        )
        precedence = {
            AssignmentJob.SCOPE_FEATURE: 0,
            AssignmentJob.SCOPE_LAYER: 1,
            AssignmentJob.SCOPE_PROJECT: 2,
        }
        # Several engineers can hold the same layer, so equal scopes need a
        # stable tiebreak: the earliest claim answers, rather than whichever row
        # the database happened to return first.
        job = min(jobs, key=lambda row: (precedence[row.scope], row.created_at)) if jobs else None

        return Response(
            {
                'design_id': design_id,
                'feature': {
                    'id': str(feature.id),
                    'layer_id': feature.layer_id,
                    'layer_name': feature.layer_name,
                    'status': feature.status,
                },
                'assignment': None
                if job is None
                else {
                    'id': str(job.id),
                    'scope': job.scope,
                    # The scope target, so a client can say which job is
                    # covering the feature rather than only its kind.
                    'layer_id': job.layer_id or '',
                    'assignee': {
                        'id': str(job.assignee_id),
                        'email': job.assignee.email,
                        'full_name': job.assignee.full_name,
                    },
                    'created_at': job.created_at.isoformat(),
                },
            }
        )


class ProjectFeatureDetailAPIView(APIView):
    """GET /api/projects/<project_id>/features/<feature_id>/"""

    def get(self, request, project_id, feature_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {'detail': 'Project not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not _engineer_can_access(request, project):
            return _deny()

        try:
            feature = Feature.objects.get(id=feature_id, project=project)
        except Feature.DoesNotExist:
            return Response(
                {'detail': 'Feature not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        feature_data = FeatureSerializer(feature, context={'request': request}).data

        # If an admin has configured this layer's fields, serve that schema
        # instead of the auto-derived one so the engineer clients use it.
        # Broad except so a not-yet-migrated table (deploy ordering) degrades
        # to the derived schema rather than 500-ing this endpoint.
        try:
            cfg = LayerFieldConfig.objects.get(project=project, layer_id=feature.layer_id)
            feature_data['field_schema'] = cfg.schema
        except LayerFieldConfig.DoesNotExist:
            pass
        except Exception:
            pass

        # Planned vs as-built (INTEGRATION_PLAN.md §3 Stage 5.2). This endpoint
        # serves one feature, so the extra lookup for the layer's tolerance is
        # free — the layer lists deliberately do not carry it.
        try:
            layer_cfg = LayerFieldConfig.objects.get(project=project, layer_id=feature.layer_id)
            tolerance = layer_cfg.tolerance
        except LayerFieldConfig.DoesNotExist:
            tolerance = 0
        except Exception:
            tolerance = 0
        feature_data['deviation'] = feature_deviation(feature, tolerance)

        # ── Try microservice for authoritative geometry; fall back to local ──
        microservice_url = (
            f'{MICROSERVICE_BASE_URL}/geo/projects/{project_id}/features/{feature_id}'
        )

        try:
            response = requests.get(microservice_url, headers=microservice_headers(), timeout=30)
            response.raise_for_status()
            geojson_payload = response.json()
            geojson_feature = geojson_payload.get('feature')
            layer_source = geojson_payload.get('layer', feature.layer_name)
        except requests.RequestException:
            # Microservice unavailable — fall back to the local geometry stored
            # in Django's Feature model.  This is a best-effort copy that may
            # be stale if the authoritative PostGIS was updated directly.
            local_geom = feature.geometry
            if local_geom:
                geojson_feature = {
                    'type': 'Feature',
                    'geometry': local_geom,
                    'properties': feature.properties,
                }
            else:
                geojson_feature = None
            layer_source = feature.layer_name

        return Response(
            {
                'project_id': str(project.id),
                'layer_name': feature.layer_name,
                'feature': feature_data,
                'geojson': geojson_feature,
                'layer_source': layer_source,
            }
        )


class ProjectFeatureUpdateAPIView(APIView):
    """
    PATCH /api/projects/<project_id>/features/<feature_id>/

    Update a feature's geometry, properties, field_measurements, status, etc.
    All fields are optional — only provided fields are updated.
    """

    def patch(self, request, project_id, feature_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {'detail': 'Project not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not _engineer_can_access(request, project):
            return _deny()

        try:
            feature = Feature.objects.get(id=feature_id, project=project)
        except Feature.DoesNotExist:
            return Response(
                {'detail': 'Feature not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = FeatureUpdateSerializer(
            feature,
            data=request.data,
            partial=True,
            context={'request': request},
        )

        if not serializer.is_valid():
            return Response(
                {'detail': 'Validation error', 'errors': serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        updated_feature = serializer.save()

        # Return the full feature representation
        response_serializer = FeatureSerializer(
            updated_feature,
            context={'request': request},
        )

        return Response(
            {
                'project_id': str(project.id),
                'feature': response_serializer.data,
            },
            status=status.HTTP_200_OK,
        )


class FeaturePhotoUploadView(APIView):
    """POST /api/features/{feature_id}/upload-photo/"""

    def post(self, request, feature_id):
        try:
            feature = Feature.objects.get(id=feature_id)
        except Feature.DoesNotExist:
            return Response(
                {'detail': 'Feature not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not _engineer_can_access(request, feature.project):
            return _deny()

        if 'photo' not in request.FILES:
            return Response(
                {'detail': 'No photo provided'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        photo = request.FILES['photo']

        # Validate file type
        allowed_types = ['image/jpeg', 'image/png', 'image/jpg']
        if photo.content_type not in allowed_types:
            return Response(
                {'detail': 'Invalid file type. Only JPEG and PNG are allowed.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate file size (max 10MB)
        max_size = 10 * 1024 * 1024  # 10MB
        if photo.size > max_size:
            return Response(
                {'detail': 'File too large. Maximum size is 10MB.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Save the photo
        feature.photo = photo
        feature.save()

        return Response(
            {
                'id': str(feature.id),
                'photo_url': (
                    request.build_absolute_uri(feature.photo.url) if feature.photo else None
                ),
                'uploaded_at': feature.updated_at.isoformat(),
            },
            status=status.HTTP_200_OK,
        )
