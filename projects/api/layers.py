import requests

from django.db.models import Count, Q, Max
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.models import Project, Feature
from .serializers import FeatureSerializer


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

        microservice_url = (
            f"{MICROSERVICE_BASE_URL}/geo/projects/{project_id}/features/{feature_id}"
        )

        try:
            response = requests.get(microservice_url, timeout=30)
            response.raise_for_status()
            geojson_payload = response.json()
        except requests.RequestException as exc:
            return Response(
                {
                    "detail": "Failed to fetch feature geometry from microservice",
                    "error": str(exc),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        feature_data = FeatureSerializer(feature).data

        return Response(
            {
                "project_id": str(project.id),
                "layer_name": feature.layer_name,
                "feature": feature_data,
                "geojson": geojson_payload.get("feature"),
                "layer_source": geojson_payload.get("layer", feature.layer_name),
            }
        )
