# projects/models/import_session.py
import uuid
from django.db import models
from .project import Project

class ImportSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    original_filename = models.TextField()
    stored_file_path = models.TextField()

    status = models.CharField(
        max_length=20,
        choices=[
            ("uploaded", "Uploaded"),
            ("validated", "Validated"),
            ("imported", "Imported"),
            ("failed", "Failed"),
        ],
        default="uploaded"
    )

    validation_summary = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "import_sessions"
