from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, TypedDict

from django.db.models import Count, Q

from projects.models import Project, Feature, LayerWeight


class LayerCompletion(TypedDict):
    layer_id: str
    layer_name: str
    weight: float
    total_features: int
    approved_features: int
    progress_percentage: float
    contribution: float


class ProjectCompletionResult(TypedDict):
    project_id: str
    standard_completion: float
    dynamic_completion: float
    total_features: int
    approved_features: int
    weights_defined: bool
    layers: List[LayerCompletion]


class CompletionService:
    """Service for calculating project completion rates."""

    @staticmethod
    def calculate_project_completion(project: Project) -> ProjectCompletionResult:
        """
        Calculate both standard and dynamic completion for a project.

        Standard Completion: (approved_count / total_count) × 100
        Dynamic Completion: Sum of (layer_progress × layer_weight) for all layers

        Partial weighting: Layers without explicit weights get equal share of remaining %.
        """
        # Get all features for this project
        features_qs = Feature.objects.filter(project=project)

        # Get total counts
        total_features = features_qs.count()
        approved_features = features_qs.filter(
            status=Feature.STATUS_APPROVED
        ).count()

        # Standard completion
        standard_completion = Decimal("0")
        if total_features > 0:
            standard_completion = (
                Decimal(str(approved_features)) / Decimal(str(total_features)) * 100
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # Get layer statistics
        layer_stats = (
            features_qs.values("layer_id", "layer_name")
            .annotate(
                total=Count("id"),
                approved=Count("id", filter=Q(status=Feature.STATUS_APPROVED)),
            )
            .order_by("layer_name")
        )

        if not layer_stats:
            # No features in project
            return ProjectCompletionResult(
                project_id=str(project.id),
                standard_completion=float(standard_completion),
                dynamic_completion=0.0,
                total_features=0,
                approved_features=0,
                weights_defined=False,
                layers=[],
            )

        # Get layer weights
        layer_weights = {
            lw.layer_id: float(lw.weight_percentage)
            for lw in LayerWeight.objects.filter(project=project)
        }

        # Calculate partial weighting for layers without explicit weights
        total_defined_weight = sum(layer_weights.values())
        undefined_layers = [
            stat["layer_id"] for stat in layer_stats if stat["layer_id"] not in layer_weights
        ]

        remaining_weight = max(0, 100 - total_defined_weight)
        if undefined_layers and remaining_weight > 0:
            auto_weight = remaining_weight / len(undefined_layers)
            for layer_id in undefined_layers:
                layer_weights[layer_id] = auto_weight

        weights_defined = total_defined_weight > 0

        # Calculate dynamic completion
        dynamic_completion = Decimal("0")
        layers: List[LayerCompletion] = []

        for stat in layer_stats:
            layer_id = stat["layer_id"]
            layer_name = stat["layer_name"]
            total = stat["total"]
            approved = stat["approved"]

            # Layer progress percentage
            progress = Decimal("0")
            if total > 0:
                progress = (
                    Decimal(str(approved)) / Decimal(str(total)) * 100
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            # Weight for this layer (default to equal if no weights defined)
            weight = Decimal(str(layer_weights.get(layer_id, 0)))

            # Contribution to dynamic completion
            contribution = (progress * weight / 100).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            dynamic_completion += contribution

            layers.append(
                LayerCompletion(
                    layer_id=layer_id,
                    layer_name=layer_name,
                    weight=float(weight),
                    total_features=total,
                    approved_features=approved,
                    progress_percentage=float(progress),
                    contribution=float(contribution),
                )
            )

        # Cap dynamic completion at 100%
        dynamic_completion = min(dynamic_completion, Decimal("100"))

        return ProjectCompletionResult(
            project_id=str(project.id),
            standard_completion=float(standard_completion),
            dynamic_completion=float(dynamic_completion.quantize(Decimal("0.01"))),
            total_features=total_features,
            approved_features=approved_features,
            weights_defined=weights_defined,
            layers=layers,
        )

    @staticmethod
    def validate_layer_weights(project: Project, weights: Dict[str, float]) -> tuple[bool, str]:
        """
        Validate that layer weights sum to 100%.

        Returns: (is_valid, error_message)
        """
        total_weight = sum(weights.values())

        if total_weight > 100:
            return False, f"Total weight exceeds 100% (current: {total_weight}%)"

        # Allow partial weighting - remaining % will be auto-distributed
        return True, ""

    @staticmethod
    def update_layer_weights(project: Project, weights: Dict[str, float]) -> None:
        """
        Update layer weights for a project.

        weights: Dict mapping layer_id to weight_percentage
        """
        # Validate first
        is_valid, error = CompletionService.validate_layer_weights(project, weights)
        if not is_valid:
            raise ValueError(error)

        # Delete existing weights
        LayerWeight.objects.filter(project=project).delete()

        # Create new weights
        for layer_id, weight in weights.items():
            LayerWeight.objects.create(
                project=project,
                layer_id=layer_id,
                weight_percentage=Decimal(str(weight)).quantize(Decimal("0.01")),
            )
