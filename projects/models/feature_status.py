# projects/models/feature_status.py
import uuid
from django.db import models
from .project import Project
from .project_layer import ProjectLayer

class FeatureStatus(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    project_layer = models.ForeignKey(ProjectLayer, on_delete=models.CASCADE)

    feature_pk = models.CharField(max_length=255)

    status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("completed", "Completed"),
            ("failed", "Failed"),
        ],
        default="pending"
    )

    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "feature_status"
        indexes = [
            models.Index(fields=["project", "project_layer"]),
        ]
