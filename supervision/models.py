import hashlib
import uuid

from django.conf import settings
from django.db import models

from projects.models import Feature, Project
from supervision.storage import get_evidence_storage


def _digest(upload):
    """(sha256 hex digest, byte count) of an upload, read once and rewound.

    The digest is what makes a photo provable: the same bytes re-uploaded, or a
    file that changed after review, can be told apart without keeping a second
    copy. ``upload.chunks()`` streams, so a 10 MB photo is never held twice.
    """
    digest = hashlib.sha256()
    size = 0
    upload.seek(0)
    for chunk in upload.chunks():
        digest.update(chunk)
        size += len(chunk)
    upload.seek(0)
    return digest.hexdigest(), size


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
    # Per-file cap, overridable by EVIDENCE_MAX_BYTES. With MAX_PER_FEATURE it
    # bounds a feature's evidence at 200 MB by default.
    MAX_BYTES = getattr(settings, 'EVIDENCE_MAX_BYTES', 10 * 1024 * 1024)
    ALLOWED_CONTENT_TYPES = ['image/jpeg', 'image/jpg', 'image/png']

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    feature = models.ForeignKey(
        Feature,
        on_delete=models.CASCADE,
        related_name='photos',
    )
    # Denormalised project link. The feature already carries it, but every
    # supervision table answers "what happened on this project?" directly: an
    # evidence row found in a project query must not need a join through
    # features to say which project it belongs to.
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='feature_photos',
        db_index=True,
    )
    # The bytes go wherever get_evidence_storage() points: an S3-compatible
    # bucket when one is configured, local disk otherwise. A callable rather
    # than an instance so the field follows the settings and the migration keeps
    # referring to the function.
    image = models.ImageField(
        upload_to='feature_photos/%Y/%m/%d/',
        storage=get_evidence_storage,
    )
    description = models.CharField(max_length=255, blank=True)
    # Evidence provenance, written once on upload and never recomputed: a
    # checksum that changes after the fact would prove nothing.
    sha256 = models.CharField(max_length=64, blank=True, default='', db_index=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    storage_backend = models.CharField(max_length=16, blank=True, default='')
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

    def save(self, *args, **kwargs):
        # The project is never supplied by the caller: it is a fact about the
        # feature, so deriving it here keeps every insert consistent and makes
        # the old photo-upload call sites correct without a second argument.
        if not self.project_id and self.feature_id:
            self.project_id = self.feature.project_id
        if self.image and not self.sha256:
            self.sha256, self.size_bytes = _digest(self.image)
        if not self.storage_backend:
            self.storage_backend = getattr(self.image.storage, 'label', '')
        super().save(*args, **kwargs)

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
            # The supervision project page reads one project's review ledger.
            models.Index(
                fields=['project', 'decision', 'created_at'],
                name='supervision_proj_dec_idx',
            ),
        ]

    def __str__(self):
        return f'{self.decision} | feature {self.feature_id}'
