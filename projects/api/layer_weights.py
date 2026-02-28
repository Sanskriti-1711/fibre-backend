from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.models import Project, LayerWeight
from projects.services import CompletionService


class ProjectLayerWeightsAPIView(APIView):
    """
    GET /api/projects/<project_id>/layers/weights/
    PUT /api/projects/<project_id>/layers/weights/
    """

    def get(self, request, project_id):
        """Get current layer weights for a project."""
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        weights = LayerWeight.objects.filter(project=project)
        weights_data = {
            w.layer_id: float(w.weight_percentage)
            for w in weights
        }

        # Get all layers for context
        layer_stats = CompletionService.calculate_project_completion(project)
        all_layers = [
            {
                "layer_id": layer["layer_id"],
                "layer_name": layer["layer_name"],
                "has_weight": layer["layer_id"] in weights_data,
                "weight": weights_data.get(layer["layer_id"], 0),
            }
            for layer in layer_stats["layers"]
        ]

        total_defined = sum(weights_data.values())
        undefined_count = len([l for l in all_layers if not l["has_weight"]])

        return Response(
            {
                "project_id": str(project.id),
                "weights_defined": total_defined > 0,
                "total_defined_weight": total_defined,
                "auto_weight_for_undefined": (
                    (100 - total_defined) / undefined_count if undefined_count > 0 else 0
                ),
                "weights": weights_data,
                "layers": all_layers,
            }
        )

    def put(self, request, project_id):
        """Update layer weights for a project."""
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        weights = request.data.get("weights", {})
        if not isinstance(weights, dict):
            return Response(
                {"detail": "Weights must be a dictionary mapping layer_id to percentage"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate weights
        is_valid, error = CompletionService.validate_layer_weights(project, weights)
        if not is_valid:
            return Response(
                {"detail": error},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            CompletionService.update_layer_weights(project, weights)
        except ValueError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "project_id": str(project.id),
                "weights": weights,
                "total_weight": sum(weights.values()),
            }
        )
