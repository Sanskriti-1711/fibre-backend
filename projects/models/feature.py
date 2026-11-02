import uuid

from django.db import models

from .project import Project


class Feature(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_ASSIGNED = 'assigned'
    STATUS_UNDER_REVIEW = 'under_review'
    STATUS_APPROVED = 'approved'
    STATUS_REDO = 'redo'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_ASSIGNED, 'Assigned'),
        (STATUS_UNDER_REVIEW, 'Under Review'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REDO, 'Redo'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE)

    layer_name = models.CharField(max_length=255)
    layer_id = models.CharField(max_length=255)

    properties = models.JSONField(default=dict)

    # Engineer field schema for this feature's layer, derived from the imported
    # GIS column names. Tells the mobile app which fields the engineer fills in
    # (and with what widget) vs. which are read-only reference data.
    field_schema = models.JSONField(null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )

    field_measurements = models.JSONField(null=True, blank=True)
    comparison_notes = models.TextField(blank=True)

    # What the reviewer recorded when they approved the feature ("accepted
    # 0.2 m deviation", "re-surveyed on 12 Nov"). Supervision-only, and the
    # reason `FeatureApproveAPIView` no longer throws the note away.
    approval_notes = models.TextField(blank=True, default='')

    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    # Edited GeoJSON geometry (stored locally for fast reads;
    # the authoritative copy may live in the PostGIS microservice).
    # This is the DESIGN baseline the LLD handed over.
    geometry = models.JSONField(null=True, blank=True)

    # Where the crew actually laid it. A separate column, never an overwrite:
    # supervision records evidence *beside* the design, so planned-vs-as-built
    # stays a comparison and the baseline cannot be destroyed
    # (INTEGRATION_PLAN.md §1 rule 3). Null until someone submits a survey.
    as_built_geometry = models.JSONField(null=True, blank=True)

    # Audit trail for geometry edits
    edited_by = models.UUIDField(null=True, blank=True)
    edited_at = models.DateTimeField(null=True, blank=True)

    photo = models.ImageField(upload_to='feature_photos/%Y/%m/%d/', blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'features'
        indexes = [
            models.Index(fields=['project', 'layer_name']),
            models.Index(fields=['layer_id']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f'{self.layer_name} | {self.id}'
