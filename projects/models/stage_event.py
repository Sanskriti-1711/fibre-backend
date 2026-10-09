import uuid

from django.db import models

from users.models import User

from .project import Project


class StageEvent(models.Model):
    """A unified timeline/audit event across the project lifecycle.

    Every meaningful transition (HLD completed, survey submitted, change
    approved, LLD run completed, etc.) writes a StageEvent so the project has a
    single, queryable history — independent of the per-entity tables that hold
    the actual data.
    """

    class Stage(models.TextChoices):
        HLD = 'hld', 'HLD'
        SURVEY = 'survey', 'Survey'
        LLD = 'lld', 'LLD'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='stage_events')
    stage = models.CharField(max_length=10, choices=Stage.choices)
    event = models.CharField(
        max_length=100
    )  # e.g. hld_completed, survey_submitted, change_approved, lld_completed
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='stage_events'
    )
    entity_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Feature id / run version this event concerns',
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'stage_events'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['project', 'stage'])]

    def __str__(self):
        return f'{self.project.name} — {self.stage}:{self.event}'
