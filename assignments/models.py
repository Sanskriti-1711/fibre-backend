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
    SCOPE_POLYGON = 'polygon'
    SCOPE_SPAN = 'span'

    SCOPE_CHOICES = [
        (SCOPE_PROJECT, 'Project'),
        (SCOPE_LAYER, 'Layer'),
        (SCOPE_FEATURE, 'Feature'),
        (SCOPE_POLYGON, 'Polygon'),
        (SCOPE_SPAN, 'Span'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='assignment_jobs')
    layer_id = models.CharField(max_length=255, blank=True, null=True)
    feature = models.ForeignKey(
        Feature, on_delete=models.CASCADE, null=True, blank=True, related_name='assignment_jobs'
    )
    # A polygon assignment is an area, not a layer: the engineer may edit only
    # the features the area covers and sees the rest greyed out. ``polygon`` is
    # the GeoJSON ring copied from the service-area feature, ``polygon_id`` the
    # id that features inside it carry in their properties, and
    # ``covered_feature_ids`` the snapshot computed when the job was saved.
    polygon = models.JSONField(null=True, blank=True)
    polygon_id = models.CharField(max_length=255, blank=True, null=True)
    polygon_name = models.CharField(max_length=255, blank=True, default='')
    # A span is a layerwise unit of supervision work: the trench span, duct
    # span or cable span that shares ``SPAN_ID`` (``TRENCH_ID`` /
    # ``PARENT_TRENCH`` on older civil layers). ``layer_id`` keeps that span in
    # one layer, so the same span key can have a separate trench, duct and
    # cable job. ``covered_feature_ids`` holds the components that shared it
    # when the job was saved. One span is one job, so a 78-component span no
    # longer becomes 78 rows on an engineer's list.
    span_id = models.CharField(max_length=255, blank=True, null=True)
    covered_feature_ids = models.JSONField(default=list, blank=True)
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
            models.UniqueConstraint(
                # An area can be held by several engineers; only the same
                # engineer's claim on the same polygon is replaced.
                fields=['project', 'polygon_id', 'assignee'],
                condition=models.Q(scope='polygon'),
                name='unique_polygon_assignment',
            ),
            models.UniqueConstraint(
                # One span per layer per engineer: the same claim is replaced,
                # while a second engineer can still join the same span. The
                # layer is part of the identity, so TR-000010 can hold a
                # trench span, a duct span and a cable span side by side.
                fields=['project', 'span_id', 'layer_id', 'assignee'],
                condition=models.Q(scope='span'),
                name='unique_span_assignment',
            ),
            # ``condition`` is the Django 5 argument; django-stubs 5.0.0 still
            # types the older ``check`` kwarg, so this is stub drift, not a bug.
            models.CheckConstraint(  # type: ignore[call-arg]
                condition=(
                    models.Q(scope='project', layer_id__isnull=True, feature__isnull=True)
                    | models.Q(scope='layer', layer_id__isnull=False, feature__isnull=True)
                    | models.Q(scope='feature', feature__isnull=False)
                    | models.Q(
                        scope='span',
                        feature__isnull=True,
                        polygon__isnull=True,
                        span_id__isnull=False,
                    )
                    | models.Q(
                        scope='polygon',
                        layer_id__isnull=True,
                        feature__isnull=True,
                        polygon__isnull=False,
                    )
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
            self.polygon = None
            self.polygon_id = None
            self.covered_feature_ids = []
        if scope == self.SCOPE_LAYER:
            self.feature = None
            self.polygon = None
            self.polygon_id = None
            self.covered_feature_ids = []
        if scope == self.SCOPE_FEATURE and self.feature:
            self.project = self.feature.project
            self.layer_id = self.feature.layer_id
            self.polygon = None
            self.polygon_id = None
            self.covered_feature_ids = []
        if scope == self.SCOPE_SPAN:
            # The components are sent with the job: nothing on the span id says
            # which components belong to it, so there is nothing to resolve
            # here and the list is kept exactly as the caller built it. The
            # layer is part of the span's identity and must survive the save.
            self.feature = None
            self.polygon = None
            self.polygon_id = None
            self.covered_feature_ids = list(self.covered_feature_ids or [])
        if scope == self.SCOPE_POLYGON:
            self.layer_id = None
            self.feature = None
            if self.polygon:
                # An assignment service that has already resolved the area for
                # this project sets the ids it computed, so a planner assigning
                # one area to five engineers does not walk the project five
                # times.
                if getattr(self, '_coverage_precomputed', False):
                    self.covered_feature_ids = list(self.covered_feature_ids or [])
                else:
                    from assignments.coverage import feature_ids_in_polygon

                    self.covered_feature_ids = feature_ids_in_polygon(
                        self.project_id, self.polygon, self.polygon_id or ''
                    )
            else:
                self.covered_feature_ids = []
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
            Feature.objects.filter(pk=self.feature_id, status=Feature.STATUS_PENDING).update(
                status=Feature.STATUS_ASSIGNED
            )
        elif scope == self.SCOPE_LAYER and self.layer_id:
            Feature.objects.filter(
                project=self.project,
                layer_id=self.layer_id,
                status=Feature.STATUS_PENDING,
            ).update(status=Feature.STATUS_ASSIGNED)
        elif scope in (self.SCOPE_POLYGON, self.SCOPE_SPAN) and self.covered_feature_ids:
            Feature.objects.filter(
                id__in=self.covered_feature_ids,
                status=Feature.STATUS_PENDING,
            ).update(status=Feature.STATUS_ASSIGNED)

    def __str__(self):
        target = self.project.name
        if self.scope == self.SCOPE_LAYER:
            target = f"{target} / {self.layer_id or 'layer'}"
        elif self.scope == self.SCOPE_FEATURE and self.feature:
            target = f'{target} / Feature {self.feature.id}'
        elif self.scope == self.SCOPE_POLYGON:
            target = f'{target} / {self.polygon_name or self.polygon_id or "polygon"}'
        elif self.scope == self.SCOPE_SPAN:
            target = f'{target} / span {self.span_id or "span"}'
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
    elif job.scope in (AssignmentJob.SCOPE_POLYGON, AssignmentJob.SCOPE_SPAN) and job.covered_feature_ids:
        candidates = Feature.objects.filter(id__in=job.covered_feature_ids)
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
    for polygon_ids in remaining.filter(
        scope__in=[AssignmentJob.SCOPE_POLYGON, AssignmentJob.SCOPE_SPAN]
    ).values_list(
        'covered_feature_ids', flat=True
    ):
        covered_features.update(polygon_ids or [])

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
