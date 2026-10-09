"""Database models for the FTTH LLD stage.

The LLD stage is a separate app from ``ftth_hld`` so the survey-review and
detailed-design responsibilities stay isolated (mirroring the separation of the
Survey and HLD apps). These models track the immutable Approved Survey
snapshot, each LLD run, and its persisted output layers.

Table names are kept identical to their original ``ftth_hld`` homes
(``ftth_approved_survey_versions`` / ``ftth_lld_runs`` / ``ftth_lld_layers``)
so no data migration is required — the move is a pure app reorganisation.
"""

import uuid

from django.db import models


class ApprovedSurveyVersion(models.Model):
    """Immutable Approved Survey snapshot used as the LLD input dataset.

    Constructed once from HLD + approved survey changes, then never
    modified — a new review cycle creates a new version (V1, V2, ...).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ftth_project = models.ForeignKey(
        'ftth_hld.FtthProject',
        on_delete=models.CASCADE,
        related_name='approved_survey_versions',
    )
    version = models.CharField(max_length=32)  # AS-V01, AS-V02 ...
    hld_version = models.CharField(max_length=32, blank=True, default='')

    # Frozen dataset: approved GeoJSON features (HLD + approved changes).
    dataset = models.JSONField(default=dict)
    summary = models.JSONField(default=dict)  # counts per status etc.

    created_by = models.ForeignKey(
        'users.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ftth_approved_survey_versions'
        ordering = ['-created_at']
        unique_together = [['ftth_project', 'version']]

    def __str__(self):
        return f'{self.version} ({self.ftth_project_id})'


class LldRun(models.Model):
    """A single LLD run and its full provenance.

    Records the exact inputs (HLD version, Approved Survey version, algorithm
    version) so any LLD output can be reproduced later.
    """

    STATUS_RUNNING = 'running'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'

    STATUS_CHOICES = [
        (STATUS_RUNNING, 'Running'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_FAILED, 'Failed'),
    ]

    MODE_VERIFY = 'verify'
    MODE_REPLAN = 'replan'
    MODE_CHOICES = [
        (MODE_VERIFY, 'Verify (apply survey changes)'),
        (MODE_REPLAN, 'Full re-plan (re-run design algorithm)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ftth_project = models.ForeignKey(
        'ftth_hld.FtthProject',
        on_delete=models.CASCADE,
        related_name='lld_runs',
    )
    lld_version = models.CharField(max_length=32)  # LLD-V01, LLD-V02 ...
    hld_version = models.CharField(max_length=32, blank=True, default='')
    approved_survey_version = models.ForeignKey(
        'ApprovedSurveyVersion',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='lld_runs',
    )
    algorithm_version = models.CharField(max_length=64, blank=True, default='')
    input_dataset_version = models.CharField(max_length=32, blank=True, default='')

    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default=MODE_VERIFY)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    outputs = models.IntegerField(null=True, blank=True)  # number of output files
    error_message = models.TextField(blank=True, default='')

    # LLD engine progress (0-100) + the continuity/attribute validation summary.
    progress = models.IntegerField(default=0)
    validation = models.JSONField(default=dict)

    run_by = models.ForeignKey(
        'users.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    run_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ftth_lld_runs'
        ordering = ['-run_date']

    def __str__(self):
        return f'{self.lld_version} ({self.ftth_project_id})'


class LldLayer(models.Model):
    """A single LLD output layer (GeoJSON FeatureCollection) for a run.

    The final LLD design is the HLD layer set with the approved survey
    changes applied — persisted per layer so the LLD results map reads from
    the database and the output can be downloaded as a zip.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lld_run = models.ForeignKey(
        'LldRun',
        on_delete=models.CASCADE,
        related_name='layers',
    )
    name = models.CharField(max_length=255)
    geojson = models.JSONField(default=dict)
    feature_count = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ftth_lld_layers'
        ordering = ['name']
        unique_together = [['lld_run', 'name']]

    def __str__(self):
        return f'{self.name} ({self.lld_run_id})'
