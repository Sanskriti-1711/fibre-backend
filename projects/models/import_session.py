import uuid

from django.db import models

from .project import Project


class ImportSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    original_filename = models.TextField()
    stored_file_path = models.TextField()

    # Which source produced this project's features: a contractor GeoPackage /
    # zip upload, or an LLD run handoff (INTEGRATION_PLAN.md §3 Stage 4.1).
    # Existing rows are uploads, hence the default; the two kinds coexist on one
    # project without colliding, so a supervision project opened from a run can
    # still accept a fallback upload.
    SOURCE_GPKG = 'gpkg'
    SOURCE_LLD_RUN = 'lld_run'
    SOURCE_KIND_CHOICES = [
        (SOURCE_GPKG, 'GeoPackage / zip upload'),
        (SOURCE_LLD_RUN, 'LLD run handoff'),
    ]

    source_kind = models.CharField(max_length=20, choices=SOURCE_KIND_CHOICES, default=SOURCE_GPKG)
    # The run marker ("<ftth_project_id>:<lld_version>") for an LLD handoff.
    source_ref = models.CharField(max_length=128, blank=True, default='')

    status = models.CharField(
        max_length=20,
        choices=[
            ('uploaded', 'Uploaded'),
            ('validated', 'Validated'),
            ('imported', 'Imported'),
            ('failed', 'Failed'),
        ],
        default='uploaded',
    )

    validation_summary = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'import_sessions'
