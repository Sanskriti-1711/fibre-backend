"""LLD → supervision handoff (INTEGRATION_PLAN.md §3 Stage 4).

A supervision project is opened *from* a completed LLD run, so its planned
baseline is that run — not a GeoPackage the contractor happened to supply.
This module copies the run's output layers into the same ``Feature`` table
every other stage already uses, and writes nothing back: ``LldRun`` and
``LldLayer`` are read only (§5.3), and no second geometry store is created
(§5.6). GPKG upload stays the fallback for files that never went through the
design stages.

Identity has two halves, and they are deliberately separate:

* ``properties`` is copied verbatim, so the design's own ``feature_id`` keeps
  naming the feature in every lineage query (``FeatureLineageView`` reads it);
* ``Feature.id`` is a UUID derived from ``(project, layer, feature)``, because
  it is a primary key shared by every project on the platform. Reusing the
  design's id would collide with the survey copy's rows, and a random id would
  duplicate the whole baseline on every re-sync.
"""

from __future__ import annotations

import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ftth_lld.models import LldRun
from projects.api.field_schema import derive_field_schema
from projects.layer_rules import normalize_layer_id
from projects.models import Feature, ImportSession, Project

from .import_views import IMPORT_BATCH_SIZE
from .projects import find_lld_run, record_stage_event

# Fixed namespace: the same (project, layer, feature) always maps to the same
# Feature id, which is what makes the handoff idempotent instead of additive.
HANDOFF_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, 'fiber360/lld-handoff')

# Attributes that carry a feature's own identity. `feature_id` is the key the
# LLD engine threads through from the HLD baseline; `id` is what the civil
# layers (drops, structures) publish. Physical position is the last resort, and
# only for a layer that has neither.
_IDENTITY_KEYS = ('feature_id', 'id')


def _feature_key(properties, index):
    """The design's identity for one feature, or its position in the layer."""
    for key in _IDENTITY_KEYS:
        value = (properties or {}).get(key)
        if value not in (None, ''):
            return str(value)
    return f'#{index}'


def _field_schema(properties):
    """Engineer form schema derived from the design's attributes.

    Same rules the ZIP import uses (``projects.api.field_schema``), so a
    supervision feature's form matches a survey feature's. Measurement-like
    attributes come back ``editable``; without that flag the engineer form had
    nothing to render for a freshly handed-off layer.
    """
    return derive_field_schema(properties)


def _layer_rows(project, run):
    """Yield ``(feature_id, fields)`` for every geometry the run carries."""
    for layer in run.layers.all():
        layer_name = layer.name
        layer_id = normalize_layer_id(layer_name) or layer_name
        features = (layer.geojson or {}).get('features') or []
        if not isinstance(features, list):
            continue
        for index, payload in enumerate(features):
            if not isinstance(payload, dict):
                continue
            geometry = payload.get('geometry')
            if not geometry or not isinstance(geometry, dict):
                continue
            properties = payload.get('properties')
            if not isinstance(properties, dict):
                properties = {}
            yield (
                uuid.uuid5(
                    HANDOFF_NAMESPACE,
                    f'{project.id}|{layer_id}|{_feature_key(properties, index)}',
                ),
                {
                    'layer_name': layer_name,
                    'layer_id': layer_id,
                    'properties': properties,
                    'geometry': geometry,
                    'field_schema': _field_schema(properties),
                },
            )


@transaction.atomic
def import_lld_run(project, run):
    """Copy one completed run's layers into ``project``'s feature rows.

    Idempotent, and deliberately *not* a re-baseline: a row that has moved past
    ``pending`` (assigned, submitted, approved, redo) keeps its planned values,
    because the geometry the engineer measured against is what their sign-off
    refers to. A new LLD version gets a new supervision project (§3 Stage 4.4).
    """
    now = timezone.now()
    existing = dict(Feature.objects.filter(project=project).values_list('id', 'status'))

    to_create: list[Feature] = []
    to_update: dict[uuid.UUID, dict] = {}
    layers: dict[str, int] = {}
    features_seen = 0

    for feature_id, fields in _layer_rows(project, run):
        features_seen += 1
        layers[fields['layer_id']] = layers.get(fields['layer_id'], 0) + 1
        if feature_id not in existing:
            to_create.append(
                Feature(project=project, status=Feature.STATUS_PENDING, id=feature_id, **fields)
            )
        elif existing[feature_id] == Feature.STATUS_PENDING:
            to_update[feature_id] = {**fields, 'updated_at': now}

    if to_create:
        Feature.objects.bulk_create(to_create, batch_size=IMPORT_BATCH_SIZE)
    if to_update:
        fields_to_write = [
            'layer_name',
            'layer_id',
            'properties',
            'geometry',
            'field_schema',
            'updated_at',
        ]
        Feature.objects.bulk_update(
            [Feature(id=feature_id, **fields) for feature_id, fields in to_update.items()],
            fields_to_write,
            batch_size=IMPORT_BATCH_SIZE,
        )

    kept = features_seen - len(to_create) - len(to_update)

    ImportSession.objects.update_or_create(
        project=project,
        source_kind=ImportSession.SOURCE_LLD_RUN,
        defaults={
            'original_filename': f'{run.lld_version} — LLD handoff',
            'stored_file_path': '',
            'status': 'imported',
            'source_ref': getattr(project, 'source_lld_run_id', '') or '',
            'validation_summary': {
                'lld_version': run.lld_version,
                'hld_version': run.hld_version,
                'layers': [
                    {'layer_id': layer_id, 'feature_count': count}
                    for layer_id, count in sorted(layers.items())
                ],
                'features_created': len(to_create),
                'features_updated': len(to_update),
                'features_kept': kept,
            },
        },
    )

    if (to_create or to_update) and project.status != 'active':
        project.status = 'active'
        project.save(update_fields=['status', 'updated_at', 'last_activity_at'])

    return {
        'lld_version': run.lld_version,
        'layers': len(layers),
        'features_created': len(to_create),
        'features_updated': len(to_update),
        'features_kept': kept,
    }


class LldHandoffView(APIView):
    """POST /api/projects/<uuid:project_id>/import/from-lld/

    Materialise the planned baseline of the LLD run this supervision project
    was opened from. Safe to call again: only rows still ``pending`` are
    refreshed, so a re-sync after a fix cannot erase approval work.

    The body may carry ``source_lld_run_id`` to re-point the project at a newer
    completed run before refreshing — the "the design was re-exported" case
    (INTEGRATION_PLAN.md §3 Stage 4.4). Re-binding is explicit and recorded on
    the timeline; without it the project keeps the run it was opened from.
    """

    def post(self, request, project_id):
        project = Project.objects.filter(id=project_id).first()
        if project is None:
            return Response({'detail': 'Project not found'}, status=status.HTTP_404_NOT_FOUND)

        bound_ref = (project.source_lld_run_id or '').strip()
        requested_ref = str((request.data or {}).get('source_lld_run_id') or '').strip()
        source_lld_run_id = requested_ref or bound_ref
        if not source_lld_run_id:
            return Response(
                {
                    'detail': (
                        'This project was not opened from an LLD run. Use the GeoPackage '
                        'upload for contractor-supplied data.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        run = find_lld_run(source_lld_run_id)
        if run is None:
            return Response(
                {'detail': f'LLD run not found: {source_lld_run_id}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if run.status != LldRun.STATUS_COMPLETED:
            return Response(
                {
                    'detail': (
                        f'LLD run {run.lld_version} is {run.status}; only a completed '
                        'run can be supervised'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        rebind = source_lld_run_id != bound_ref
        if rebind:
            project.source_lld_run_id = source_lld_run_id
            project.save(update_fields=['source_lld_run_id', 'updated_at'])

        result = import_lld_run(project, run)
        record_stage_event(
            project,
            'supervision_resynced',
            actor=request.user,
            entity_id=run.lld_version,
            metadata={
                'source_lld_run_id': source_lld_run_id,
                'rebound_from': bound_ref,
                **{key: result[key] for key in ('features_created', 'features_updated', 'features_kept')},
            },
        )
        return Response(
            {
                'project_id': str(project.id),
                'source_lld_run_id': source_lld_run_id,
                'rebound': rebind,
                **result,
            }
        )
