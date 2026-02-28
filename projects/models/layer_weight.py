import uuid

from django.db import models
from .project import Project


class LayerWeight(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="layer_weights"
    )
    layer_id = models.CharField(max_length=255)
    weight_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "layer_weights"
        unique_together = [["project", "layer_id"]]
        indexes = [
            models.Index(fields=["project", "layer_id"]),
        ]

    def __str__(self):
        return f"{self.project.name} | {self.layer_id}: {self.weight_percentage}%"
