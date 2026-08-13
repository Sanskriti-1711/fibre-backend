import uuid

from django.db import models

from .project import Project


class ProjectLayer(models.Model):
    """Per-layer state within a project.

    Gives each GIS layer its own row (counts, approval state, status) so the
    project can be navigated hierarchically: project -> layers -> features ->
    survey changes -> LLD output.
    """

    STATUS_PENDING = "pending"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_REVIEWED = "reviewed"
    STATUS_APPROVED = "approved"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_REVIEWED, "Reviewed"),
        (STATUS_APPROVED, "Approved"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="layers"
    )
    layer_id = models.CharField(max_length=255)
    layer_name = models.CharField(max_length=255, blank=True, default="")

    feature_count = models.IntegerField(default=0)
    survey_feature_count = models.IntegerField(default=0)
    approved_count = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "project_layers"
        unique_together = [["project", "layer_id"]]

    def __str__(self):
        return f"{self.project.name} / {self.layer_name or self.layer_id}"
