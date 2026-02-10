import uuid

from django.db import models

from .project import Project


class Feature(models.Model):
    STATUS_PENDING = "pending"
    STATUS_ASSIGNED = "assigned"
    STATUS_UNDER_REVIEW = "under_review"
    STATUS_APPROVED = "approved"
    STATUS_REDO = "redo"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ASSIGNED, "Assigned"),
        (STATUS_UNDER_REVIEW, "Under Review"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REDO, "Redo"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE)

    layer_name = models.CharField(max_length=255)
    layer_id = models.CharField(max_length=255)

    properties = models.JSONField(default=dict)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )

    field_measurements = models.JSONField(null=True, blank=True)
    comparison_notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "features"
        indexes = [
            models.Index(fields=["project", "layer_name"]),
            models.Index(fields=["layer_id"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.layer_name} | {self.id}"
