import uuid
from django.db import models

class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    region = models.CharField(max_length=255, blank=True)

    status = models.CharField(
        max_length=20,
        choices=[
            ("draft", "Draft"),
            ("in_progress", "In Progress"),
            ("assigned", "Assigned"),
            ("active", "Active"),
            ("completed", "Completed"),
            ("archived", "Archived"),
        ],
        default="draft"
    )

    # When a Survey project is the copy of an HLD pipeline run, this holds
    # the source FtthProject.project_id so we can find/reuse it on re-assign.
    source_ftth_project_id = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
    )

    standard_completion = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "projects"

    def __str__(self):
        return self.name
