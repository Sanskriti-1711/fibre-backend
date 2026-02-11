import uuid

from django.db import models

from projects.models import Project, Feature
from users.models import User


class AssignmentJob(models.Model):
    SCOPE_PROJECT = "project"
    SCOPE_LAYER = "layer"
    SCOPE_FEATURE = "feature"

    SCOPE_CHOICES = [
        (SCOPE_PROJECT, "Project"),
        (SCOPE_LAYER, "Layer"),
        (SCOPE_FEATURE, "Feature"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="assignment_jobs")
    layer_id = models.CharField(max_length=255, blank=True, null=True)
    feature = models.ForeignKey(Feature, on_delete=models.CASCADE, null=True, blank=True, related_name="assignment_jobs")
    assignee = models.ForeignKey(User, on_delete=models.CASCADE, related_name="assignment_jobs")
    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "assignment_jobs"
        constraints = [
            models.UniqueConstraint(
                fields=["project"],
                condition=models.Q(scope="project"),
                name="unique_project_assignment",
            ),
            models.UniqueConstraint(
                fields=["project", "layer_id"],
                condition=models.Q(scope="layer"),
                name="unique_layer_assignment",
            ),
            models.UniqueConstraint(
                fields=["feature"],
                condition=models.Q(scope="feature"),
                name="unique_feature_assignment",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(scope="project", layer_id__isnull=True, feature__isnull=True)
                    | models.Q(scope="layer", layer_id__isnull=False, feature__isnull=True)
                    | models.Q(scope="feature", feature__isnull=False)
                ),
                name="valid_scope_constraints",
            ),
        ]

    def save(self, *args, **kwargs):
        instance = getattr(self, "instance", None)
        scope = getattr(instance, "scope", None) if instance else self.scope
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

    def _mark_features_assigned(self, scope: str) -> None:
        if scope == self.SCOPE_FEATURE and self.feature_id:
            Feature.objects.filter(pk=self.feature_id).exclude(
                status=Feature.STATUS_ASSIGNED
            ).update(status=Feature.STATUS_ASSIGNED)
        elif scope == self.SCOPE_LAYER and self.layer_id:
            Feature.objects.filter(
                project=self.project,
                layer_id=self.layer_id,
            ).exclude(status=Feature.STATUS_ASSIGNED).update(
                status=Feature.STATUS_ASSIGNED
            )

    def __str__(self):
        target = self.project.name
        if self.scope == self.SCOPE_LAYER:
            target = f"{target} / {self.layer_id or 'layer'}"
        elif self.scope == self.SCOPE_FEATURE and self.feature:
            target = f"{target} / Feature {self.feature.id}"
        return f"{target} -> {self.assignee.email}"
