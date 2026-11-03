from decimal import Decimal, InvalidOperation

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.models import Feature, LayerFieldConfig, Project

# Map an admin-chosen input "type" to the (role, widget, unit) the clients use.
TYPE_MAP = {
    'number': {'role': 'measurement', 'widget': 'number', 'unit': True},
    'text': {'role': 'qc', 'widget': 'text', 'unit': False},
    'location': {'role': 'measurement_text', 'widget': 'text', 'unit': False},
    'textarea': {'role': 'note', 'widget': 'textarea', 'unit': False},
}


def _layer_name(project, layer_id):
    f = Feature.objects.filter(project=project, layer_id=layer_id).first()
    return f.layer_name if f else None


def _derived_schema(project, layer_id):
    """The auto-derived schema captured at import from any feature in the layer."""
    f = (
        Feature.objects.filter(project=project, layer_id=layer_id)
        .exclude(field_schema__isnull=True)
        .first()
    )
    return f.field_schema if f and f.field_schema else []


class LayerFieldConfigAPIView(APIView):
    """
    GET/PUT /api/projects/<project_id>/layers/<layer_id>/field-config/

    GET  -> the effective schema (admin config if set, else auto-derived), plus
            the raw derived schema so the admin UI can offer a "reset".
    PUT  -> save the admin-edited schema for this layer.
    """

    def get(self, request, project_id, layer_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response({'detail': 'Project not found'}, status=status.HTTP_404_NOT_FOUND)

        derived = _derived_schema(project, layer_id)

        try:
            cfg = LayerFieldConfig.objects.get(project=project, layer_id=layer_id)
            effective = cfg.schema
            configured = True
        except LayerFieldConfig.DoesNotExist:
            effective = derived
            configured = False

        return Response(
            {
                'project_id': str(project.id),
                'layer_id': layer_id,
                'layer_name': _layer_name(project, layer_id),
                'configured': configured,
                'tolerance': float(cfg.tolerance) if configured else 0.0,
                'schema': effective,
                'derived': derived,
            }
        )

    def put(self, request, project_id, layer_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response({'detail': 'Project not found'}, status=status.HTTP_404_NOT_FOUND)

        schema = request.data.get('schema')
        if not isinstance(schema, list):
            return Response(
                {'detail': 'schema must be a list of field objects'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        clean = []
        for i, item in enumerate(schema):
            if not isinstance(item, dict) or not item.get('key'):
                continue

            editable = bool(item.get('editable'))
            if editable:
                # Prefer an explicit "type"; else fall back to the given widget.
                type_key = item.get('type') or item.get('widget') or 'number'
                spec = TYPE_MAP.get(type_key, TYPE_MAP['number'])
                role, widget, unit = spec['role'], spec['widget'], spec['unit']
            else:
                role, widget, unit = 'reference', 'readonly', False

            clean.append(
                {
                    'key': item['key'],
                    'label': item.get('label') or item['key'],
                    'role': role,
                    'widget': widget,
                    'unit': unit,
                    'editable': editable,
                    'order': item.get('order', i),
                }
            )

        # Absolute slack that still counts as "as designed" (§3 Stage 5.2).
        # Omitted means 0, the strictest reading: any difference is a deviation.
        try:
            tolerance = Decimal(str(request.data.get('tolerance', 0))).quantize(Decimal('0.001'))
        except (InvalidOperation, TypeError, ValueError):
            return Response(
                {'detail': 'tolerance must be a number'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cfg, _ = LayerFieldConfig.objects.update_or_create(
            project=project,
            layer_id=layer_id,
            defaults={
                'schema': clean,
                'layer_name': _layer_name(project, layer_id) or '',
                'tolerance': tolerance,
            },
        )

        return Response(
            {
                'project_id': str(project.id),
                'layer_id': layer_id,
                'layer_name': cfg.layer_name,
                'configured': True,
                'tolerance': float(cfg.tolerance),
                'schema': cfg.schema,
            }
        )
