import uuid

from django.db import models


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    region = models.CharField(max_length=255, blank=True)

    # ── Business aspects ────────────────────────────────────────────
    client_name = models.CharField(max_length=255, blank=True, default='')
    contract_ref = models.CharField(max_length=255, blank=True, default='')
    priority = models.CharField(
        max_length=10,
        choices=[('low', 'Low'), ('medium', 'Medium'), ('high', 'High')],
        default='medium',
    )
    business_meta = models.JSONField(default=dict, blank=True)

    # ── Technical aspects ───────────────────────────────────────────
    technical_meta = models.JSONField(default=dict, blank=True)

    # Project lifecycle status. The engineer-facing workflow is:
    #   assigned → (engineer accepts) → active → (engineer submits) → submitted
    #   submitted → (admin) → under_review → reviewed → accepted   (final)
    #   under_review / reviewed → (admin) → redo → (engineer re-does) → active/submitted
    status = models.CharField(
        max_length=20,
        choices=[
            ('draft', 'Draft'),
            ('in_progress', 'In Progress'),
            ('assigned', 'Assigned'),
            ('active', 'Active'),
            ('submitted', 'Submitted'),
            ('under_review', 'Under Review'),
            ('reviewed', 'Reviewed'),
            ('accepted', 'Accepted'),
            ('redo', 'Redo'),
            ('completed', 'Completed'),
            ('archived', 'Archived'),
        ],
        default='draft',
    )

    # When a Survey project is the copy of an HLD pipeline run, this holds
    # the source FtthProject.project_id so we can find/reuse it on re-assign.
    source_ftth_project_id = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
    )

    # When this project is a supervision project opened from a completed LLD
    # run, this holds that run's identity ("<ftth_project_id>:<lld_version>")
    # and ``business_meta['origin']`` records how it was created:
    # ``lld_run`` (handoff) or ``manual_gpkg`` (contractor upload). Both are
    # additive: survey copies keep NULL here.
    #
    # Kept as text rather than an FK because a run lives in the LLD app and the
    # only thing later stages need is a readable link back to "which design was
    # this compared against" — INTEGRATION_PLAN.md §3 Stage 3.2.
    source_lld_run_id = models.CharField(
        max_length=128,
        blank=True,
        null=True,
        db_index=True,
    )

    standard_completion = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'projects'

    def __str__(self):
        return self.name
