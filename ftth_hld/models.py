"""Database models for the FTTH HLD pipeline module.

Stores metadata about each pipeline run so it can be queried alongside regular
projects in the Django admin / API. The LLD stage lives in the separate
``ftth_lld`` app.
"""

import uuid

from django.db import models


class FtthProject(models.Model):
    """Tracks an FTTH HLD pipeline run.

    The actual pipeline output files live on disk under
    ``settings.MEDIA_ROOT / "ftth_outputs" / project_id /``.
    This model stores metadata so the results can be browsed and managed
    through the Django API alongside regular projects.
    """

    STATUS_QUEUED = 'queued'
    STATUS_RUNNING = 'running'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'

    STATUS_CHOICES = [
        (STATUS_QUEUED, 'Queued'),
        (STATUS_RUNNING, 'Running'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_FAILED, 'Failed'),
    ]

    # Use a 32-char hex string as the primary key (matching what the
    # pipeline runner generates).
    project_id = models.CharField(
        max_length=64,
        primary_key=True,
        editable=False,
    )

    # Human-readable name supplied by the user at submission time
    name = models.CharField(max_length=255, blank=True, default='')

    # Who triggered this pipeline run (nullable for anonymous triggers)
    created_by = models.ForeignKey(
        'users.User',
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
    stage_name = models.CharField(max_length=255, blank=True, default='')
    stage_index = models.IntegerField(default=0)
    stage_count = models.IntegerField(default=6)
    progress = models.IntegerField(default=0)

    # Error message if failed
    error_message = models.TextField(blank=True, default='')

    # File references
    excel_filename = models.CharField(max_length=255, blank=True, default='')
    roads_filename = models.CharField(max_length=255, blank=True, default='')

    # Field engineer this HLD run is assigned to (survey stage).
    # The actual survey work happens on the Survey copy (Project row with
    # source_ftth_project_id set); this field is bookkeeping for the UI.
    assigned_engineer = models.ForeignKey(
        'users.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='assigned_ftth_projects',
    )
    assigned_at = models.DateTimeField(null=True, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'ftth_projects'
        ordering = ['-created_at']

    def __str__(self):
        return self.name or self.project_id[:16]


class FtthLayer(models.Model):
    """Persisted HLD output layer for a pipeline run.

    Stores the full GeoJSON FeatureCollection of a generated layer so the
    results map reads from the database instead of depending on the FastAPI
    engine being reachable. This is the GIS data store for the per-layer
    input/output values produced by the HLD pipeline.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ftth_project = models.ForeignKey(
        'ftth_hld.FtthProject',
        on_delete=models.CASCADE,
        related_name='hld_layers',
    )
    name = models.CharField(max_length=255)  # canonical layer name
    geojson = models.JSONField(default=dict)  # FeatureCollection
    feature_count = models.IntegerField(default=0)
    # The CONTENT revision of whatever this layer was derived from — set by the
    # pass that builds it (trench sections record the trench revision they were
    # built from). A derived layer must be rebuilt when its SOURCE CONTENT
    # changed, and that cannot be judged from `updated_at`: re-publishing a
    # layer rewrites its rows and bumps the timestamp even when the geometry is
    # identical, which made `sections_are_fresh()` false after every re-ingest.
    source_revision = models.CharField(max_length=64, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ftth_hld_layers'
        ordering = ['name']
        unique_together = [['ftth_project', 'name']]

    def __str__(self):
        return f'{self.name} ({self.ftth_project_id})'


class BoqRate(models.Model):
    """Rate card line item used to price BOQ/BOM quantities.

    Seeded from the BOQ.xlsx template (item codes + names + units) via the
    ``seed_boq_rates`` management command; prices are editable via Django
    admin. ``material_rate`` / ``labour_rate`` / ``rent_rate`` are per-unit
    prices in the project currency.
    """

    item_code = models.CharField(max_length=20, unique=True)
    section = models.CharField(max_length=60, blank=True, default='')
    item_name = models.CharField(max_length=255)
    unit = models.CharField(max_length=20, blank=True, default='')
    material_rate = models.FloatField(default=0.0)
    labour_rate = models.FloatField(default=0.0)
    rent_rate = models.FloatField(default=0.0)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'ftth_boq_rates'
        ordering = ['item_code']

    def __str__(self):
        return f'{self.item_code} {self.item_name}'


class BoqSnapshot(models.Model):
    """Immutable per-project BOQ/BOM computed from the HLD output layers.

    Created once the HLD run completes; regenerated only on demand. The
    ``boq_json`` / ``bom_json`` hold the computed rows (section, code,
    item, unit, quantity, unit prices, amounts). This decouples the
    deliverable from the mutable layers and gives each HLD run a stable,
    reproducible BOQ.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ftth_project = models.OneToOneField(
        'ftth_hld.FtthProject',
        on_delete=models.CASCADE,
        related_name='boq_snapshot',
    )
    boq_json = models.JSONField(default=list)  # computed BOQ rows
    bom_json = models.JSONField(default=list)  # computed BOM rows
    boq_totals = models.JSONField(default=dict)
    bom_totals = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    regenerated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'ftth_boq_snapshots'

    def __str__(self):
        return f'BOQ snapshot for {self.ftth_project_id}'


class HldPostProcess(models.Model):
    """Durable guard for the work that follows a completed HLD run.

    The chain — layer sync, road-class attribution, trench sections, permit
    matrix, preliminary permit package — used to run **inside the status GET**,
    guarded by *derived* data: "any ``gis.trench_layer`` row with a NULL
    ``fclass``" and ``FtthLayer.updated_at``. Both guards are re-armed by
    re-publishing layers, which is routine — the engine's own trench payload
    carries no ``fclass``, and every re-publish bumps ``trenches.updated_at`` —
    so a completed project re-ran the whole chain on every poll and the request
    took **234.7 s** (544 trenches attributed against a 405,599-road extract).

    This row is the guard that cannot be re-armed: it records what ran, against
    which trench CONTENT revision (``ftth_hld.posthld.trench_content_revision``),
    so re-publishing unchanged layers is a no-op and a genuinely changed
    trench is what triggers a rebuild.
    """

    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_DONE = 'done'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_RUNNING, 'Running'),
        (STATUS_DONE, 'Done'),
        (STATUS_FAILED, 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project_id = models.CharField(max_length=64, unique=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    # trench content revision the chain completed against (empty = never run)
    trench_revision = models.CharField(max_length=64, blank=True, default='')
    # step name -> {"ok": bool, "seconds": float, "detail": str}
    steps = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default='')
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ftth_hld_post_process'

    def __str__(self):
        return f'post-HLD {self.project_id}: {self.status}'
