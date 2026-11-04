import uuid

from django.db import models
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver

from projects.models import Feature, Project
from users.models import User


class AssignmentJob(models.Model):
    SCOPE_PROJECT = 'project'
    SCOPE_LAYER = 'layer'
    SCOPE_FEATURE = 'feature'

    SCOPE_CHOICES = [
        (SCOPE_PROJECT, 'Project'),
        (SCOPE_LAYER, 'Layer'),
        (SCOPE_FEATURE, 'Feature'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='assignment_jobs')
    layer_id = models.CharField(max_length=255, blank=True, null=True)
    feature = models.ForeignKey(
        Feature, on_delete=models.CASCADE, null=True, blank=True, related_name='assignment_jobs'
    )
    assignee = models.ForeignKey(User, on_delete=models.CASCADE, related_name='assignment_jobs')
    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'assignment_jobs'
        constraints = [
            models.UniqueConstraint(
                # One project-scope job PER ENGINEER (multi-engineer assignment).
                fields=['project', 'assignee'],
                condition=models.Q(scope='project'),
                name='unique_project_assignment',
            ),
            models.UniqueConstraint(
                # One layer-scope job PER ENGINEER, like the project scope: a
                # layer is big enough to need several people on it, and the
                # constraint should stop a duplicate of the same claim rather
                # than stop a second engineer joining.
                fields=['project', 'layer_id', 'assignee'],
                condition=models.Q(scope='layer'),
                name='unique_layer_assignment',
            ),
            models.UniqueConstraint(
                fields=['feature'],
                condition=models.Q(scope='feature'),
                name='unique_feature_assignment',
            ),
            # ``condition`` is the Django 5 argument; django-stubs 5.0.0 still
            # types the older ``check`` kwarg, so this is stub drift, not a bug.
            models.CheckConstraint(  # type: ignore[call-arg]
                condition=(
                    models.Q(scope='project', layer_id__isnull=True, feature__isnull=True)
                    | models.Q(scope='layer', layer_id__isnull=False, feature__isnull=True)
                    | models.Q(scope='feature', feature__isnull=False)
                ),
                name='valid_scope_constraints',
            ),
        ]

    def save(self, *args, **kwargs):
        instance = getattr(self, 'instance', None)
        scope = getattr(instance, 'scope', None) if instance else self.scope
        if scope == self.SCOPE_PROJECT:
            self.layer_id = None
            self.feature = None
        if scope == self.SCOPE_LAYER:
            self.feature = None
        if scope == self.SCOPE_FEATURE and self.feature:
            self.project = self.feature.project
            self.layer_id = self.feature.layer_id
        super().save(*args, **kwargs)
        self._mark_features_assigned(scope)

    def _mark_features_assigned(self, scope: str | None) -> None:
        # Only ``pending`` moves to ``assigned``. ``assigned`` already says it;
        # ``under_review``, ``approved`` and ``redo`` are the engineer's work,
        # and an assignment must not erase them -- a layer or project job covers
        # rows at every stage, so an update that excluded only ``assigned``
        # quietly reopened approved features and then released them to
        # ``pending`` when the job was deleted.
        if scope == self.SCOPE_FEATURE and self.feature_id:
            Feature.objects.filter(
                pk=self.feature_id, status=Feature.STATUS_PENDING
            ).update(status=Feature.STATUS_ASSIGNED)
        elif scope == self.SCOPE_LAYER and self.layer_id:
            Feature.objects.filter(
                project=self.project,
                layer_id=self.layer_id,
                status=Feature.STATUS_PENDING,
            ).update(status=Feature.STATUS_ASSIGNED)

    def __str__(self):
        target = self.project.name
        if self.scope == self.SCOPE_LAYER:
            target = f"{target} / {self.layer_id or 'layer'}"
        elif self.scope == self.SCOPE_FEATURE and self.feature:
            target = f'{target} / Feature {self.feature.id}'
        return f'{target} -> {self.assignee.email}'


def _release_assigned_features(job: AssignmentJob) -> None:
    """Return to ``pending`` the rows this job was the only reason to call assigned.

    ``save()`` puts a feature in front of an engineer by setting ``assigned``,
    and until now nothing took that back: deleting the job left rows claiming
    an engineer who no longer has them, which also made them look unavailable to
    the next assignment. Only ``assigned`` is released — submitted, approved and
    redo are the engineer's work, and a row keeps that state whether or not
    someone is currently assigned to it.
    """
    if job.scope == AssignmentJob.SCOPE_FEATURE and job.feature_id:
        candidates = Feature.objects.filter(pk=job.feature_id)
    elif job.scope == AssignmentJob.SCOPE_LAYER and job.layer_id:
        candidates = Feature.objects.filter(project_id=job.project_id, layer_id=job.layer_id)
    elif job.scope == AssignmentJob.SCOPE_PROJECT:
        candidates = Feature.objects.filter(project_id=job.project_id)
    else:
        return

    assigned = list(candidates.filter(status=Feature.STATUS_ASSIGNED).only('id', 'layer_id'))
    if not assigned:
        return

    # The job is already gone (``post_delete``), so what is left is the whole
    # truth: a project job still covers everything, a layer job still covers its
    # layer, a feature job still covers its feature.
    remaining = AssignmentJob.objects.filter(project_id=job.project_id)
    if remaining.filter(scope=AssignmentJob.SCOPE_PROJECT).exists():
        return
    covered_layers = set(
        remaining.filter(Q(scope=AssignmentJob.SCOPE_LAYER)).values_list('layer_id', flat=True)
    )
    covered_features = set(
        remaining.filter(scope=AssignmentJob.SCOPE_FEATURE).values_list('feature_id', flat=True)
    )

    release = [
        feature.id
        for feature in assigned
        if feature.id not in covered_features and feature.layer_id not in covered_layers
    ]
    if release:
        Feature.objects.filter(id__in=release).update(status=Feature.STATUS_PENDING)


@receiver(post_delete, sender=AssignmentJob)
def _on_assignment_deleted(sender, instance, **kwargs) -> None:  # noqa: ARG001
    # A queryset delete (admin bulk delete, a cascade) never calls the model's
    # ``delete()``, but it does send this signal once per row.
    _release_assigned_features(instance)
