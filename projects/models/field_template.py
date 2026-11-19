import uuid

from django.db import models
from django.db.models import Q

from projects.layer_rules import LAYER_KIND_CHOICES

from .project import Project


class FieldTemplate(models.Model):
    """Extra engineer fields for a layer type, at a chosen scope.

    A layer already brings its own form: the attributes imported from the
    design or the GeoPackage, turned into a schema by
    :func:`projects.api.field_schema.derive_field_schema`. This model is the
    planner's addition to that form — fields the platform wants the engineer to
    record that the design never carried (a safety note, a photo reference, a
    measured clearance).

    Scope is the only decision the planner has to make:

    * ``project`` — this layer type in one project.
    * ``global``  — this layer type in every project.

    There is deliberately no per-job scope. A span or feature assignment
    decides *which* rows an engineer may edit; the form those rows render is a
    property of their layer, so a field scoped to "the trench layer" reaches a
    trench span job without a second configuration.

    ``fields`` holds only the added field specs. They are appended to the
    layer's own schema at read time (``projects.api.field_templates``), never
    substituted for it, so applying one template to a project whose layers have
    different attributes cannot erase those attributes.
    """

    SCOPE_PROJECT = 'project'
    SCOPE_GLOBAL = 'global'

    SCOPE_CHOICES = [
        (SCOPE_PROJECT, 'One project'),
        (SCOPE_GLOBAL, 'All projects'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='field_templates',
        help_text='Set for a one-project template, empty for an all-projects template.',
    )
    layer_kind = models.CharField(max_length=30, choices=LAYER_KIND_CHOICES)
    fields = models.JSONField(default=list)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'field_templates'
        constraints = [
            models.UniqueConstraint(
                fields=['project', 'layer_kind'],
                condition=Q(scope='project'),
                name='unique_project_field_template',
            ),
            models.UniqueConstraint(
                fields=['layer_kind'],
                condition=Q(scope='global'),
                name='unique_global_field_template',
            ),
            models.CheckConstraint(
                condition=(
                    Q(scope='global', project__isnull=True)
                    | Q(scope='project', project__isnull=False)
                ),
                name='field_template_scope_project',
            ),
        ]

    def __str__(self):
        target = self.project.name if self.project_id else 'all projects'
        return f'{self.layer_kind} fields — {target}'
