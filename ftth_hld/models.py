"""
Database models for the FTTH HLD pipeline module.

Stores metadata about each pipeline run so it can be queried
alongside regular projects in the Django admin / API.
"""

from django.db import models


class FtthProject(models.Model):
    """
    Tracks an FTTH HLD pipeline run.

    The actual pipeline output files live on disk under
    ``settings.MEDIA_ROOT / "ftth_outputs" / project_id /``.
    This model stores metadata so the results can be browsed and
    managed through the Django API alongside regular projects.
    """

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    # Use a 32-char hex string as the primary key (matching what the
    # pipeline runner generates).
    project_id = models.CharField(
        max_length=64,
        primary_key=True,
        editable=False,
    )

    # Human-readable name supplied by the user at submission time
    name = models.CharField(max_length=255, blank=True, default="")

    # Who triggered this pipeline run (nullable for anonymous triggers)
    created_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_QUEUED,
    )

    # Pipeline stage tracking
    stage_name = models.CharField(max_length=255, blank=True, default="")
    stage_index = models.IntegerField(default=0)
    stage_count = models.IntegerField(default=6)
    progress = models.IntegerField(default=0)

    # Error message if failed
    error_message = models.TextField(blank=True, default="")

    # File references
    excel_filename = models.CharField(max_length=255, blank=True, default="")
    roads_filename = models.CharField(max_length=255, blank=True, default="")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ftth_projects"
        ordering = ["-created_at"]

    def __str__(self):
        return self.name or self.project_id[:16]
