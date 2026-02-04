# projects/models/project_layer.py
import uuid
from django.db import models
from .project import Project
from .import_session import ImportSession

class ProjectLayer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    import_session = models.ForeignKey(ImportSession, on_delete=models.CASCADE)

    layer_name = models.CharField(max_length=255)
    table_name = models.CharField(max_length=255)

    geometry_type = models.CharField(max_length=20)
    srid = models.IntegerField()

    feature_count = models.IntegerField(default=0)

    status = models.CharField(
        max_length=20,
        choices=[("active", "Active"), ("archived", "Archived")],
        default="active"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "project_layers"
