"""
Database models for the FTTH HLD pipeline module.

Stores metadata about each pipeline run so it can be queried
alongside regular projects in the Django admin / API.
"""

import uuid

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

    # Field engineer this HLD run is assigned to (survey stage).
    # The actual survey work happens on the Survey copy (Project row with
    # source_ftth_project_id set); this field is bookkeeping for the UI.
    assigned_engineer = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_ftth_projects",
    )
    assigned_at = models.DateTimeField(null=True, blank=True)

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


class ApprovedSurveyVersion(models.Model):
    """Immutable Approved Survey snapshot used as the LLD input dataset.

    Constructed once from HLD + approved survey changes, then never
    modified — a new review cycle creates a new version (V1, V2, ...).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ftth_project = models.ForeignKey(
        "ftth_hld.FtthProject",
        on_delete=models.CASCADE,
        related_name="approved_survey_versions",
    )
    version = models.CharField(max_length=32)  # AS-V01, AS-V02 ...
    hld_version = models.CharField(max_length=32, blank=True, default="")

    # Frozen dataset: approved GeoJSON features (HLD + approved changes).
    dataset = models.JSONField(default=dict)
    summary = models.JSONField(default=dict)  # counts per status etc.

    created_by = models.ForeignKey(
        "users.User", null=True, blank=True, on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ftth_approved_survey_versions"
        ordering = ["-created_at"]
        unique_together = [["ftth_project", "version"]]

    def __str__(self):
        return f"{self.version} ({self.ftth_project_id})"


class LldRun(models.Model):
    """A single LLD run and its full provenance.

    Records the exact inputs (HLD version, Approved Survey version, algorithm
    version) so any LLD output can be reproduced later.
    """

    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = [
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ftth_project = models.ForeignKey(
        "ftth_hld.FtthProject",
        on_delete=models.CASCADE,
        related_name="lld_runs",
    )
    lld_version = models.CharField(max_length=32)  # LLD-V01, LLD-V02 ...
    hld_version = models.CharField(max_length=32, blank=True, default="")
    approved_survey_version = models.ForeignKey(
        "ftth_hld.ApprovedSurveyVersion",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="lld_runs",
    )
    algorithm_version = models.CharField(max_length=64, blank=True, default="")
    input_dataset_version = models.CharField(max_length=32, blank=True, default="")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    outputs = models.IntegerField(null=True, blank=True)  # number of output files
    error_message = models.TextField(blank=True, default="")

    run_by = models.ForeignKey(
        "users.User", null=True, blank=True, on_delete=models.SET_NULL,
    )
    run_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ftth_lld_runs"
        ordering = ["-run_date"]

    def __str__(self):
        return f"{self.lld_version} ({self.ftth_project_id})"
