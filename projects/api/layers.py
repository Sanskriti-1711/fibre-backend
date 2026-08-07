import requests

from django.db.models import Count, Q, Max
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.models import Project, Feature, LayerFieldConfig
from .serializers import FeatureSerializer, FeatureUpdateSerializer


MICROSERVICE_BASE_URL = "https://fiber-import.zeabur.app"


class ProjectLayerListAPIView(APIView):
    """GET /api/projects/<project_id>/layers/"""

    def get(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        features_qs = Feature.objects.filter(project=project)

        layer_summaries = (
            features_qs.values("layer_id", "layer_name")
            .annotate(
                feature_count=Count("id"),
                pending_count=Count(
                    "id", filter=Q(status=Feature.STATUS_PENDING)
                ),
                assigned_count=Count(
                    "id", filter=Q(status=Feature.STATUS_ASSIGNED)
                ),
                under_review_count=Count(
                    "id", filter=Q(status=Feature.STATUS_UNDER_REVIEW)
                ),
                approved_count=Count(
                    "id", filter=Q(status=Feature.STATUS_APPROVED)
                ),
                redo_count=Count("id", filter=Q(status=Feature.STATUS_REDO)),
                last_feature_update=Max("updated_at"),
            )
            .order_by("layer_name")
        )

        layers = [
            {
                "layer_id": entry["layer_id"],
                "layer_name": entry["layer_name"],
                "feature_count": entry["feature_count"],
                "status_counts": {
                    "pending": entry["pending_count"],
                    "assigned": entry["assigned_count"],
                    "under_review": entry["under_review_count"],
                    "approved": entry["approved_count"],
                    "redo": entry["redo_count"],
                },
                "last_feature_update": entry["last_feature_update"],
            }
            for entry in layer_summaries
        ]

        return Response(
            {
                "project_id": str(project.id),
                "layers": layers,
            }
        )


class ProjectLayerDetailAPIView(APIView):
    """GET /api/projects/<project_id>/layers/<layer_id>/"""

    def get(self, request, project_id, layer_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        layer_features = Feature.objects.filter(
            project=project,
            layer_id=layer_id,
        ).order_by("created_at")

        if not layer_features.exists():
            return Response(
                {"detail": "Layer not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        first_feature = layer_features.first()

        aggregations = layer_features.aggregate(
            feature_count=Count("id"),
            pending_count=Count("id", filter=Q(status=Feature.STATUS_PENDING)),
            assigned_count=Count("id", filter=Q(status=Feature.STATUS_ASSIGNED)),
            under_review_count=Count(
                "id", filter=Q(status=Feature.STATUS_UNDER_REVIEW)
            ),
            approved_count=Count("id", filter=Q(status=Feature.STATUS_APPROVED)),
            redo_count=Count("id", filter=Q(status=Feature.STATUS_REDO)),
        )

        serialized_features = FeatureSerializer(layer_features, many=True).data

        return Response(
            {
                "project_id": str(project.id),
                "layer": {
                    "layer_id": layer_id,
                    "layer_name": first_feature.layer_name,
                    "feature_count": aggregations["feature_count"],
                    "status_counts": {
                        "pending": aggregations["pending_count"],
                        "assigned": aggregations["assigned_count"],
                        "under_review": aggregations["under_review_count"],
                        "approved": aggregations["approved_count"],
                        "redo": aggregations["redo_count"],
                    },
                },
                "features": serialized_features,
            }
        )


class ProjectFeatureDetailAPIView(APIView):
    """GET /api/projects/<project_id>/features/<feature_id>/"""

    def get(self, request, project_id, feature_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            feature = Feature.objects.get(id=feature_id, project=project)
        except Feature.DoesNotExist:
            return Response(
                {"detail": "Feature not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        feature_data = FeatureSerializer(feature, context={'request': request}).data

        # If an admin has configured this layer's fields, serve that schema
        # instead of the auto-derived one so the engineer clients use it.
        # Broad except so a not-yet-migrated table (deploy ordering) degrades
        # to the derived schema rather than 500-ing this endpoint.
        try:
            cfg = LayerFieldConfig.objects.get(
                project=project, layer_id=feature.layer_id
            )
            feature_data['field_schema'] = cfg.schema
        except LayerFieldConfig.DoesNotExist:
            pass
        except Exception:
            pass

        # ── Try microservice for authoritative geometry; fall back to local ──
        microservice_url = (
            f"{MICROSERVICE_BASE_URL}/geo/projects/{project_id}/features/{feature_id}"
        )

        try:
            response = requests.get(microservice_url, timeout=30)
            response.raise_for_status()
            geojson_payload = response.json()
            geojson_feature = geojson_payload.get("feature")
            layer_source = geojson_payload.get("layer", feature.layer_name)
        except requests.RequestException:
            # Microservice unavailable — fall back to the local geometry stored
            # in Django's Feature model.  This is a best-effort copy that may
            # be stale if the authoritative PostGIS was updated directly.
            local_geom = feature.geometry
            if local_geom:
                geojson_feature = {
                    "type": "Feature",
                    "geometry": local_geom,
                    "properties": feature.properties,
                }
            else:
                geojson_feature = None
            layer_source = feature.layer_name

        return Response(
            {
                "project_id": str(project.id),
                "layer_name": feature.layer_name,
                "feature": feature_data,
                "geojson": geojson_feature,
                "layer_source": layer_source,
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
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            feature = Feature.objects.get(id=feature_id, project=project)
        except Feature.DoesNotExist:
            return Response(
                {"detail": "Feature not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = FeatureUpdateSerializer(
            feature,
            data=request.data,
            partial=True,
            context={"request": request},
        )

        if not serializer.is_valid():
            return Response(
                {"detail": "Validation error", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        updated_feature = serializer.save()

        # Return the full feature representation
        response_serializer = FeatureSerializer(
            updated_feature,
            context={"request": request},
        )

        return Response(
            {
                "project_id": str(project.id),
                "feature": response_serializer.data,
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
                {"detail": "Feature not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if "photo" not in request.FILES:
            return Response(
                {"detail": "No photo provided"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        photo = request.FILES["photo"]

        # Validate file type
        allowed_types = ["image/jpeg", "image/png", "image/jpg"]
        if photo.content_type not in allowed_types:
            return Response(
                {"detail": "Invalid file type. Only JPEG and PNG are allowed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate file size (max 10MB)
        max_size = 10 * 1024 * 1024  # 10MB
        if photo.size > max_size:
            return Response(
                {"detail": "File too large. Maximum size is 10MB."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Save the photo
        feature.photo = photo
        feature.save()

        return Response(
            {
                "id": str(feature.id),
                "photo_url": request.build_absolute_uri(feature.photo.url) if feature.photo else None,
                "uploaded_at": feature.updated_at.isoformat(),
            },
            status=status.HTTP_200_OK,
        )
