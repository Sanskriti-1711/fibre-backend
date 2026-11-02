import uuid

from django.conf import settings
from django.db import models

from projects.models import Feature, Project


class FeaturePhoto(models.Model):
    """One of several site photos a field engineer attaches to a feature.

    Ported from the delivered supervision package
    (``Field_Supervision/fiber-backend-main/projects/models/feature_photo.py``).
    It lives in its own app because it is supervision-only: the design stages
    never attach evidence, they only produce the planned geometry.

    The old single ``Feature.photo`` column is kept (deprecated) until the UI
    stops reading it — see the integration plan G4. Ordering is ``order`` then
    ``uploaded_at`` so the first photo stays stable as the "primary" one.
    """

    MAX_PER_FEATURE = 20
    MAX_BYTES = 10 * 1024 * 1024  # 10 MB
    ALLOWED_CONTENT_TYPES = ['image/jpeg', 'image/jpg', 'image/png']

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    feature = models.ForeignKey(
        Feature,
        on_delete=models.CASCADE,
        related_name='photos',
    )
    image = models.ImageField(upload_to='feature_photos/%Y/%m/%d/')
    description = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='feature_photos',
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'feature_photos'
        ordering = ['order', 'uploaded_at']
        indexes = [
            models.Index(fields=['feature', 'order']),
        ]

    def __str__(self):
        return f'Photo {self.id} | feature {self.feature_id}'


class FeatureDecisionRecord(models.Model):
    """One append-only record of a supervision decision about a feature.

    The platform's core rule is that construction is judged *against* the
    design: a deviation becomes a proposal for a new design version, never an
    edit to the existing one (INTEGRATION_PLAN.md §5.2/§5.3). This table is
    where that proposal starts life.

    It exists because the decision used to live only in ``approval_notes``: free
    text on a mutable row, with no stage, no computed as-built comparison and no
    way to find it again. A record here is created when a reviewer approves work
    whose readings fall outside the layer's tolerance, and it is never updated
    or deleted — re-approving after a redo creates the next record, so the
    history of what was accepted, by whom and why stays intact.

    ``deviation`` is a snapshot of the comparison at decision time (the same
    shape ``projects.services.feature_deviation`` returns), because the planned
    and as-built values may both change later: a record has to keep saying what
    was true when the reviewer signed it.
    """

    DECISION_APPROVED = 'approved'
    DECISION_APPROVED_WITH_DEVIATION = 'approved_with_deviation'
    DECISION_REDO = 'redo'

    DECISION_CHOICES = [
        (DECISION_APPROVED, 'Approved as designed'),
        (DECISION_APPROVED_WITH_DEVIATION, 'Approved with deviation'),
        (DECISION_REDO, 'Sent back for rework'),
    ]

    STAGE_SUPERVISION = 'supervision'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    feature = models.ForeignKey(
        Feature,
        on_delete=models.CASCADE,
        related_name='decision_records',
    )
    # Denormalised so a project's records can be read without joining through
    # features, which is the query the supervision project page needs.
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='decision_records',
    )

    stage = models.CharField(max_length=20, default=STAGE_SUPERVISION)
    decision = models.CharField(max_length=32, choices=DECISION_CHOICES)

    # Why the deviation was accepted — the reviewer's note, verbatim.
    reason = models.TextField(blank=True, default='')
    deviation = models.JSONField(default=dict, blank=True)

    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='supervision_decisions',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'feature_decision_records'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['feature', 'created_at']),
            models.Index(fields=['project', 'created_at']),
        ]

    def __str__(self):
        return f'{self.decision} | feature {self.feature_id}'
