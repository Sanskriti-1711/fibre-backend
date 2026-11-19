"""Scoped planner-added form fields, and the resolver every client reads.

One resolver answers "what form does this feature render?" for the project
layer API, the feature detail API and the admin preview. Precedence:

1. a per-layer :class:`LayerFieldConfig` (Layer Details → Field Configuration),
   which is the full, explicit schema for that one layer;
2. otherwise the schema stored on the feature, or the one derived from its
   attributes;
3. plus any fields added by a template — the project's template for this layer
   kind first, then the all-projects one.

Templates only ever add fields; they never remove or redefine one (see
``field_spec.merge_extra_fields``).
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.api.field_schema import derive_field_schema, has_editable
from projects.api.field_spec import merge_extra_fields
from projects.api.layer_field_config import clean_schema
from projects.layer_rules import LAYER_KIND_CHOICES, layer_kind
from projects.models import Feature, FieldTemplate, LayerFieldConfig, Project
from users.permissions import IsSubadmin

VALID_LAYER_KINDS = {value for value, _label in LAYER_KIND_CHOICES}


def serialize_template(template: FieldTemplate) -> dict:
    return {
        'id': str(template.id),
        'scope': template.scope,
        'project': str(template.project_id) if template.project_id else None,
        'layer_kind': template.layer_kind,
        'fields': template.fields or [],
        'updated_at': template.updated_at.isoformat(),
    }


class SchemaResolver:
    """Resolve the effective field schema for a project's features.

    Built once per request (the layer list resolves every feature in it), so a
    layer page costs three queries for the whole page rather than one per row.
    """

    def __init__(self, project: Project):
        self.exact = {
            config.layer_id: config.schema or []
            for config in LayerFieldConfig.objects.filter(project=project)
        }
        self.project_templates = {
            template.layer_kind: template.fields or []
            for template in FieldTemplate.objects.filter(
                project=project, scope=FieldTemplate.SCOPE_PROJECT
            )
        }
        self.global_templates = {
            template.layer_kind: template.fields or []
            for template in FieldTemplate.objects.filter(
                scope=FieldTemplate.SCOPE_GLOBAL, project__isnull=True
            )
        }

    def extras_for(self, layer_id: str) -> list[dict]:
        kind = layer_kind(layer_id)
        # The project's own template first, then the all-projects one. Both
        # apply: a project template adds to the global form, it does not
        # replace it. merge_extra_fields drops a repeated key.
        return list(self.project_templates.get(kind) or []) + list(
            self.global_templates.get(kind) or []
        )

    @staticmethod
    def base_for(feature: Feature) -> list[dict]:
        """The layer's own fields, before any template is applied."""
        stored = feature.field_schema
        if has_editable(stored):
            return list(stored or [])
        derived = derive_field_schema(feature.properties) or stored or []
        return list(derived)

    def for_feature(self, feature: Feature) -> list[dict]:
        exact = self.exact.get(feature.layer_id)
        base = exact if exact is not None else self.base_for(feature)
        return merge_extra_fields(base, self.extras_for(feature.layer_id))


def _sample_feature(project: Project | None, kind: str) -> Feature | None:
    """One feature of the requested kind, to preview a template against."""
    queryset = Feature.objects.all()
    if project is not None:
        queryset = queryset.filter(project=project)

    layer_id = next(
        (
            candidate
            for candidate in queryset.values_list('layer_id', flat=True).distinct()
            if layer_kind(candidate) == kind
        ),
        None,
    )
    if not layer_id:
        return None

    rows = Feature.objects.filter(layer_id=layer_id)
    if project is not None:
        rows = rows.filter(project=project)
    return rows.order_by('created_at').first()


def _template_fields(project: Project | None, kind: str) -> list[dict]:
    fields = []
    if project is not None:
        template = FieldTemplate.objects.filter(
            project=project, layer_kind=kind, scope=FieldTemplate.SCOPE_PROJECT
        ).first()
        if template:
            fields.extend(template.fields or [])
    template = FieldTemplate.objects.filter(
        layer_kind=kind, scope=FieldTemplate.SCOPE_GLOBAL, project__isnull=True
    ).first()
    if template:
        fields.extend(template.fields or [])
    return fields


class FieldTemplateListAPIView(APIView):
    """GET /api/field-templates/  ·  PUT /api/field-templates/

    GET lists the all-projects templates plus, when ``?project=<id>`` is given,
    that project's own templates. PUT creates or replaces the template for the
    (scope, project, layer_kind) it names — one template per choice, so the
    planner never has to think about duplicates.
    """

    permission_classes = [IsSubadmin]

    def get(self, request):
        project_id = request.query_params.get('project')
        templates = FieldTemplate.objects.filter(scope=FieldTemplate.SCOPE_GLOBAL)
        if project_id:
            try:
                project = Project.objects.get(id=project_id)
            except (Project.DoesNotExist, ValueError, ValidationError):
                return Response(
                    {'detail': 'Project not found'}, status=status.HTTP_404_NOT_FOUND
                )
            templates = templates | FieldTemplate.objects.filter(project=project)

        return Response(
            {
                'templates': [
                    serialize_template(template)
                    for template in templates.order_by('layer_kind', 'scope')
                ],
                'layer_kinds': [
                    {'value': value, 'label': label} for value, label in LAYER_KIND_CHOICES
                ],
            }
        )

    def put(self, request):
        scope = request.data.get('scope') or FieldTemplate.SCOPE_PROJECT
        if scope not in (FieldTemplate.SCOPE_PROJECT, FieldTemplate.SCOPE_GLOBAL):
            return Response(
                {'detail': 'scope must be "project" or "global"'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        kind = request.data.get('layer_kind')
        if kind not in VALID_LAYER_KINDS:
            return Response(
                {'detail': f'layer_kind must be one of: {", ".join(sorted(VALID_LAYER_KINDS))}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        project = None
        if scope == FieldTemplate.SCOPE_PROJECT:
            project_id = request.data.get('project')
            if not project_id:
                return Response(
                    {'detail': 'A one-project template needs a project'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                project = Project.objects.get(id=project_id)
            except (Project.DoesNotExist, ValueError, ValidationError):
                return Response({'detail': 'Project not found'}, status=status.HTTP_404_NOT_FOUND)

        fields = request.data.get('fields')
        if not isinstance(fields, list):
            return Response(
                {'detail': 'fields must be a list of field objects'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # A template field is always something the engineer fills in; the
        # read-only toggle belongs to the per-layer editor.
        fields = [
            {**item, 'editable': True} for item in fields if isinstance(item, dict)
        ]
        cleaned = clean_schema(fields)

        template, _created = FieldTemplate.objects.update_or_create(
            scope=scope,
            project=project,
            layer_kind=kind,
            defaults={'fields': cleaned},
        )

        return Response(serialize_template(template), status=status.HTTP_200_OK)


class FieldTemplateDetailAPIView(APIView):
    """DELETE /api/field-templates/<id>/"""

    permission_classes = [IsSubadmin]

    def delete(self, request, template_id):
        deleted, _count = FieldTemplate.objects.filter(id=template_id).delete()
        if not deleted:
            return Response({'detail': 'Template not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


class FieldTemplatePreviewAPIView(APIView):
    """GET /api/field-templates/preview/?layer_kind=trench&project=<id>

    Returns the layer's own fields and the effective form after the applicable
    template, using one real feature as the sample.
    """

    permission_classes = [IsSubadmin]

    def get(self, request):
        kind = request.query_params.get('layer_kind')
        if kind not in VALID_LAYER_KINDS:
            return Response(
                {'detail': f'layer_kind must be one of: {", ".join(sorted(VALID_LAYER_KINDS))}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        project = None
        project_id = request.query_params.get('project')
        if project_id:
            try:
                project = Project.objects.get(id=project_id)
            except (Project.DoesNotExist, ValueError, ValidationError):
                return Response({'detail': 'Project not found'}, status=status.HTTP_404_NOT_FOUND)

        sample = _sample_feature(project, kind)
        if sample is not None:
            resolver = SchemaResolver(sample.project)
            base = resolver.base_for(sample)
            effective = resolver.for_feature(sample)
        else:
            base = []
            effective = merge_extra_fields([], _template_fields(project, kind))

        return Response(
            {
                'layer_kind': kind,
                'project_id': str(sample.project_id) if sample else (str(project.id) if project else None),
                'layer_id': sample.layer_id if sample else '',
                'layer_name': sample.layer_name if sample else '',
                'base': base,
                'schema': effective,
            }
        )
