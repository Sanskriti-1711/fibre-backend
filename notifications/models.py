"""In-app notifications — one row per (event, recipient).

The platform already mails permit reviewers (``permits.notifications``), but a
planner or a survey engineer working inside the product had no way to learn
that a change was waiting for them: the ``LLD Approval Queue`` only showed work
if somebody remembered to open it, and the engineer portal's "Notifications"
tab was really just the activity log replayed.

This table is the inbox behind both.  Rows are written by ``notifications.service``
at the places where work actually changes hands (a survey change is submitted,
a project is assigned, a decision is made, an LLD run finishes), so the answer
to "what needs me?" is one cheap query rather than a re-derivation from six
different tables.

Design notes:

* **Per recipient, not per event.** ``recipient`` is the person who must act;
  read state is theirs alone.  Fan-out happens in the service, inside one
  ``bulk_create``, so an event to ten planners is still one insert.
* **``url`` is relative and frontend-owned.** The backend does not know the
  hash routes of ``fiber-fe``, and a hard-coded absolute host would break the
  deployed/local split.  Senders pass the page they want opened.
* **``dedupe_key`` makes retries free.** A mobile client that posts the same
  survey edit twice (offline sync retry) must not ring the planner's bell
  twice, so senders can name the event; a repeat is dropped in the service.
* **Never load-bearing.** Every write is best-effort: a notification failure
  must not fail the assignment, review or run that produced it.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Notification(models.Model):
    """One message for one person, with its own read state."""

    # ── What happened ────────────────────────────────────────────────
    KIND_SURVEY_CHANGE = 'survey_change'
    KIND_SURVEY_DECISION = 'survey_decision'
    KIND_SURVEY_ASSIGNED = 'survey_assigned'
    KIND_LLD_READY = 'lld_ready'
    KIND_LLD_RUN = 'lld_run'
    KIND_PERMIT = 'permit'
    KIND_SYSTEM = 'system'

    KIND_CHOICES = [
        (KIND_SURVEY_CHANGE, 'Survey change submitted'),
        (KIND_SURVEY_DECISION, 'Survey change decided'),
        (KIND_SURVEY_ASSIGNED, 'Survey work assigned'),
        (KIND_LLD_READY, 'Approved Survey Version ready'),
        (KIND_LLD_RUN, 'LLD run'),
        (KIND_PERMIT, 'Permit'),
        (KIND_SYSTEM, 'System'),
    ]

    # ── Who it is for ────────────────────────────────────────────────
    # The two audiences the product actually has.  ``category`` is filterable
    # in the bell so a survey engineer is not shown planner-only design events
    # and vice versa; it is derived from the recipient at send time.
    CATEGORY_DESIGN = 'design'
    CATEGORY_SURVEY = 'survey'
    CATEGORY_PERMITS = 'permits'
    CATEGORY_SYSTEM = 'system'

    CATEGORY_CHOICES = [
        (CATEGORY_DESIGN, 'Design / planning'),
        (CATEGORY_SURVEY, 'Field survey'),
        (CATEGORY_PERMITS, 'Permits'),
        (CATEGORY_SYSTEM, 'System'),
    ]

    SEVERITY_INFO = 'info'
    SEVERITY_SUCCESS = 'success'
    SEVERITY_WARNING = 'warning'
    SEVERITY_CRITICAL = 'critical'

    SEVERITY_CHOICES = [
        (SEVERITY_INFO, 'Info'),
        (SEVERITY_SUCCESS, 'Success'),
        (SEVERITY_WARNING, 'Warning'),
        (SEVERITY_CRITICAL, 'Critical'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
        help_text='The person who has to see this.',
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='notifications_caused',
        help_text='Who caused it, when that is a person.',
    )

    kind = models.CharField(max_length=32, choices=KIND_CHOICES, db_index=True)
    category = models.CharField(
        max_length=16, choices=CATEGORY_CHOICES, default=CATEGORY_SYSTEM, db_index=True
    )
    severity = models.CharField(max_length=12, choices=SEVERITY_CHOICES, default=SEVERITY_INFO)

    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default='')

    # Context, all optional: a system notice may have none of it.
    project_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    project_name = models.CharField(max_length=200, blank=True, default='')
    entity_type = models.CharField(max_length=40, blank=True, default='')
    entity_id = models.CharField(max_length=64, blank=True, default='')
    url = models.CharField(
        max_length=300,
        blank=True,
        default='',
        help_text='Frontend page (relative) that opens the thing this is about.',
    )
    meta = models.JSONField(default=dict, blank=True)

    # Same (recipient, dedupe_key) is delivered once; a retrying client cannot
    # ring the bell twice for one event.
    dedupe_key = models.CharField(max_length=200, blank=True, default='', db_index=True)

    read_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = 'ftth_notifications'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'read_at', 'created_at']),
            models.Index(fields=['recipient', 'category', 'created_at']),
        ]

    def __str__(self):
        return f'{self.kind} -> {self.recipient_id}'

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def mark_read(self, *, at=None) -> bool:
        """Mark this row read. Returns True when it changed."""
        if self.read_at is not None:
            return False
        self.read_at = at or timezone.now()
        self.save(update_fields=['read_at'])
        return True
