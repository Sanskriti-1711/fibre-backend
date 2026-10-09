"""Permit rule engine — deterministic GIS/attribute analysis for a project.

Iterates the rule catalogue, runs each rule against the project's persisted
HLD/LLD layers, and upserts ``PermitMatrix`` rows with full traceability
(rule id + version snapshot + evidence). Missing reference data is recorded as
a gap note — never as "no permit needed".
"""

from __future__ import annotations

import json
import uuid

from django.db import connection

from ..analysis.grouping import assign_groups
from ..analysis.municipality import attribute_municipality
from ..analysis.road_authority import resolve_road_authority
from ..analysis.spatial_intersection import (
    gis_table_exists,
    intersections_with,
)
from ..generators.data import hld_layer_sections
from ..models import PermitAuthority, PermitEvent, PermitMatrix, PermitRule
from .registry import RULE_CATALOGUE, RuleDef

# Rules that read the LLD final_trenches layer from the LLD layer store.
_LLD_ROUTE_RULES = {'TRAFFIC_001', 'UTILITY_REUSE_001'}


def _latest_lld_layer_rows(project_id: str, layer_name: str, limit: int = 300):
    """Rows of an LLD output layer (geojson in ``business.ftth_lld_layers``)
    for the project's most recent LLD run."""
    sql = """
        SELECT f.value->'properties' AS props
        FROM business.ftth_lld_layers l,
             jsonb_array_elements(l.geojson->'features') f
        WHERE l.name = %s
          AND l.lld_run_id = (
              SELECT id FROM business.ftth_lld_runs
              WHERE ftth_project_id = %s
              ORDER BY run_date DESC LIMIT 1
          )
        LIMIT %s
    """
    rows = []
    with connection.cursor() as cur:
        cur.execute(sql, [layer_name, project_id, limit])
        for (props,) in cur.fetchall():
            if isinstance(props, str):
                props = json.loads(props)
            rows.append(props or {})
    return rows


def _ensure_rule(rule_def: RuleDef) -> tuple[PermitRule, int]:
    """Get-or-create the PermitRule row for a catalogue rule. Returns the
    rule and its current version (the version snapshot stored on matrix rows)."""
    authority = None
    if rule_def.authority_code:
        authority = PermitAuthority.objects.filter(code=rule_def.authority_code).first()
    rule, created = PermitRule.objects.get_or_create(
        rule_id=rule_def.rule_id,
        defaults={
            'name': rule_def.name,
            'description': rule_def.description,
            'layer_a': rule_def.layer_a,
            'layer_b': rule_def.layer_b,
            'operator': rule_def.operator,
            'required_level': rule_def.required_level,
            'authority': authority,
            'evidence_required': rule_def.evidence_required,
            'blocks_construction': rule_def.blocks_construction,
        },
    )
    if not created:
        # Sync catalogue drift into the DB snapshot (the registry is the
        # source of truth — e.g. the environmental split repointed layer_b
        # from the combined osm_environmental to per-category tables).
        changed = []
        for field, value in (
            ('name', rule_def.name),
            ('description', rule_def.description),
            ('layer_a', rule_def.layer_a),
            ('layer_b', rule_def.layer_b),
            ('operator', rule_def.operator),
            ('required_level', rule_def.required_level),
            ('evidence_required', rule_def.evidence_required),
            ('blocks_construction', rule_def.blocks_construction),
        ):
            if getattr(rule, field) != value:
                setattr(rule, field, value)
                changed.append(field)
        if authority and rule.authority_id != authority.id:
            rule.authority = authority
            changed.append('authority')
        if changed:
            rule.save(update_fields=changed + ['updated_at'])
    return rule, rule.version


def _upsert_permit(
    project_id: str,
    rule: PermitRule,
    route_section: str,
    layer: str,
    evidence: dict,
    notes: str = '',
    required: bool | None = None,
    authority: PermitAuthority | None = None,
) -> PermitMatrix:
    """Create (or update-in-place) a matrix row for one route section.

    ``authority`` overrides the rule-level authority per row (e.g. the
    road authority resolved from the segment's fclass).
    """
    required = rule.required_level == 'REQUIRED' if required is None else required
    row_authority = authority or rule.authority
    obj, created = PermitMatrix.objects.get_or_create(
        project_id=project_id,
        rule=rule,
        route_section=route_section[:128],
        defaults={
            'layer': layer,
            'authority': row_authority,
            'permit_type': rule.name,
            'rule_version': str(rule.version),
            'required': required,
            'blocks_construction': rule.blocks_construction,
            'evidence': evidence,
            'analysis_notes': notes,
            'status': PermitMatrix.STATUS_IDENTIFIED,
            'readiness_pct': 0,
        },
    )
    if not created:
        # Refresh the evidence/notes snapshot; keep review state untouched.
        obj.evidence = {**obj.evidence, **evidence}
        if notes:
            obj.analysis_notes = notes
        if authority:
            obj.authority = authority
            obj.save(update_fields=['evidence', 'analysis_notes', 'authority', 'updated_at'])
        else:
            obj.save(update_fields=['evidence', 'analysis_notes', 'updated_at'])
    PermitEvent.objects.get_or_create(
        permit=obj,
        event='IDENTIFIED',
        defaults={'detail': {'rule_id': rule.rule_id, 'created': bool(created)}},
    )
    return obj


def run_analysis(project_id: str, user=None) -> dict:
    """Run the full rule catalogue for a project. Returns a summary dict."""
    summary = {
        'project_id': project_id,
        'rules_fired': [],
        'rows_created': 0,
        'notes': [],
        'gaps': [],
    }

    # Municipality auto-derivation — Gemeinde from the OSM admin boundary
    # reference layer (best-effort: a missing layer records a gap, manual
    # overrides on the matrix are never overwritten).
    muni = attribute_municipality(project_id)
    if muni.get('layer_missing'):
        summary['gaps'].append(
            'osm_admin_boundary: reference layer gis.osm_admin_boundary not '
            'present — municipality stays manual until the layer is loaded '
            '(load_osm_reference_layers --layer osm_admin_boundary)'
        )
    elif muni.get('resolved'):
        summary['notes'].append(
            f"municipality resolved for {muni['resolved']} trenches "
            f"({muni['rows_updated']} matrix rows filled)"
        )

    # Street-level grouping (permit_group) — one permit per street per rule
    # (segment rows stay for map colouring / variation / traceability).
    grp = assign_groups(project_id)
    if grp.get('no_roads'):
        summary['gaps'].append('permit_group: no roads input file — street grouping skipped')
    elif grp['trench_rows'] or grp['lld_rows']:
        summary['notes'].append(
            f"street grouping: {grp['trench_rows']} trench rows, "
            f"{grp['lld_rows']} LLD rows assigned to streets"
        )

    for rule_def in RULE_CATALOGUE:
        rule, version = _ensure_rule(rule_def)

        # ── LLD attribute rules (traffic / utility reuse) ────────────────
        if rule_def.rule_id in _LLD_ROUTE_RULES:
            rows = _latest_lld_layer_rows(project_id, 'final_trenches')
            if not rows:
                summary['gaps'].append(
                    f'{rule_def.rule_id}: no final_trenches LLD output for project'
                )
                continue
            fired = 0
            for props in rows:
                if rule_def.rule_id == 'TRAFFIC_001':
                    surface = props.get('SURFACE') or ''
                    if surface not in ('Asphalt', 'Footpath'):
                        continue
                    evidence = {
                        'road_class': {
                            'present': bool(props.get('CONSTRUCT')),
                            'value': props.get('CONSTRUCT'),
                        },
                        'lane_impact': {'present': False, 'value': None},
                        'tmp_document': {'present': False, 'value': None},
                    }
                else:  # UTILITY_REUSE_001
                    reuse = props.get('REUSE_SOURCE') or ''
                    if not reuse:
                        continue
                    evidence = {
                        'reuse_source': {'present': True, 'value': reuse},
                        'capacity_check': {
                            'present': bool(props.get('INFRA_STATUS') == 'Reused'),
                            'value': props.get('INFRA_STATUS'),
                        },
                    }
                route_section = str(props.get('feature_id') or props.get('id') or 'unknown')
                _upsert_permit(
                    project_id,
                    rule,
                    route_section,
                    layer='final_trenches',
                    evidence=evidence,
                )
                fired += 1
            summary['rules_fired'].append({'rule_id': rule_def.rule_id, 'rows': fired})
            summary['rows_created'] += fired
            continue

        # ── Spatial intersection rules (railway / water / environmental) ──
        if rule_def.operator == 'INTERSECTS':
            if not gis_table_exists(rule_def.layer_b):
                summary['gaps'].append(
                    f'{rule_def.rule_id}: reference layer gis.{rule_def.layer_b} '
                    'not present — no permit identified until the layer exists'
                )
                continue
            # Route layers to evaluate: the rule's declared layer when it is a
            # real gis table (e.g. final_trenches once persisted), plus the
            # HLD output trench_layer so HLD-stage permits fire today.
            route_tables = []
            if gis_table_exists(rule_def.layer_a) and rule_def.layer_a != 'trench_layer':
                route_tables.append(rule_def.layer_a)
            if gis_table_exists('trench_layer'):
                route_tables.append('trench_layer')
            if not route_tables:
                summary['gaps'].append(
                    f'{rule_def.rule_id}: no route layer (gis.trench_layer / '
                    f'gis.{rule_def.layer_a}) present'
                )
                continue
            fired = 0
            for route_table in route_tables:
                hits = intersections_with(route_table, project_id, rule_def.layer_b)
                if not hits:
                    continue
                for hit in hits:
                    evidence = {
                        'crossing_coordinate': {
                            'present': hit.get('crossing_lng') is not None,
                            'value': (
                                [hit.get('crossing_lng'), hit.get('crossing_lat')]
                                if hit.get('crossing_lng') is not None
                                else None
                            ),
                        },
                    }
                    if rule_def.rule_id == 'RAILWAY_CROSSING_001':
                        evidence.update(
                            {
                                'hdd_design': {'present': False, 'value': None},
                                'profile_drawing': {'present': False, 'value': None},
                            }
                        )
                    elif rule_def.rule_id in ('ENVIRONMENTAL_001', 'ENVIRONMENTAL_002'):
                        # zone_type is known from the reference feature itself;
                        # impact_assessment stays open until a reviewer attaches it.
                        evidence.update(
                            {
                                'zone_type': {
                                    'present': bool(hit.get('ref_type')),
                                    'value': hit.get('ref_type'),
                                },
                                'impact_assessment': {'present': False, 'value': None},
                            }
                        )
                    elif rule_def.rule_id == 'ENVIRONMENTAL_003':
                        # tree_id from the OSM reference node; root protection
                        # measures are the reviewer's input.
                        evidence.update(
                            {
                                'tree_id': {
                                    'present': bool(hit.get('ref_id')),
                                    'value': hit.get('ref_id'),
                                },
                                'root_protection': {'present': False, 'value': None},
                            }
                        )
                    _upsert_permit(
                        project_id,
                        rule,
                        str(hit['route_id']),
                        layer=route_table,
                        evidence=evidence,
                    )
                    fired += 1
            if not fired:
                summary['notes'].append(f'{rule_def.rule_id}: no intersections found')
                continue
            summary['rules_fired'].append({'rule_id': rule_def.rule_id, 'rows': fired})
            summary['rows_created'] += fired
            continue

        # ── Attribute road-authority rule (fclass on trench sections) ────
        if rule_def.operator == 'ATTRIBUTE':
            if not gis_table_exists('trench_layer'):
                summary['gaps'].append(f'{rule_def.rule_id}: gis.trench_layer not present')
                continue
            # SECTION-LEVEL, street-wise. The trench layer publishes one
            # feature per construction sub-category (Open Cut / Garden / HDD),
            # so keying permits on the feature would give three rows for the
            # whole network and lose every street. Read the buildable civil
            # sections instead: each one carries the road it runs along, the
            # responsible authority follows from that road class, and the
            # street groups its sections into one permit per street.
            sections = [
                sec
                for sec in hld_layer_sections(project_id, 'trench_layer')
                if str(sec.get('fclass') or '').strip()
            ]
            if not sections:
                summary['gaps'].append(
                    f'{rule_def.rule_id}: no fclass persisted on trench sections — '
                    "run the road-class attribution (needs the project's roads "
                    'input) to enable authority mapping'
                )
                continue
            # One row per SECTION means thousands of rows on a real network, so
            # this rule writes in BATCHES (create / update / events) instead of
            # one round trip per section — the analysis runs inside the HLD
            # status poll, where a per-row upsert would stall the UI.
            required = rule.required_level == 'REQUIRED'
            authority_cache: dict[str, tuple[PermitAuthority | None, str]] = {}
            existing = {
                row.route_section: row
                for row in PermitMatrix.objects.filter(project_id=project_id, rule=rule)
            }
            to_create: list[PermitMatrix] = []
            to_update: list[PermitMatrix] = []
            for sec in sections:
                fclass = str(sec.get('fclass')).strip()
                code, owner_label = resolve_road_authority(fclass)
                if code not in authority_cache:
                    authority_cache[code] = (
                        PermitAuthority.objects.filter(code=code).first(),
                        owner_label,
                    )
                authority, label = authority_cache[code]
                street = str(sec.get('street_name') or '').strip()
                # Section identity is ``<gis_id>#<SECTION_ID>`` — unique per
                # section while still tracing back to the parent feature (the
                # map/joins match on the part before the ``#``).
                parent = str(sec.get('PARENT_FEATURE_ID') or '').strip()
                section_id = str(sec.get('SECTION_ID') or '').strip() or 'section'
                route_section = (f'{parent}#{section_id}' if parent else section_id)[:128]
                evidence = {
                    'road_class': {'present': True, 'value': fclass},
                    'road_owner': {
                        'present': authority is not None,
                        'value': label if authority is not None else None,
                    },
                }
                if street:
                    evidence['street_name'] = {'present': True, 'value': street}
                notes = f'Street: {street}' if street else 'Street: (unnamed road)'
                # One permit per street: group this section with the others on
                # the same road. Never overwrite a manual grouping.
                group = (street or f'{fclass} (unnamed)')[:128]
                row = existing.get(route_section)
                if row is None:
                    row = PermitMatrix(
                        permit_id=uuid.uuid4(),
                        project_id=project_id,
                        rule=rule,
                        route_section=route_section,
                        layer='trench_layer',
                        authority=authority,
                        permit_type=rule.name,
                        rule_version=str(rule.version),
                        required=required,
                        blocks_construction=rule.blocks_construction,
                        evidence=evidence,
                        analysis_notes=notes,
                        permit_group=group,
                        status=PermitMatrix.STATUS_IDENTIFIED,
                        readiness_pct=0,
                    )
                    to_create.append(row)
                else:
                    row.evidence = {**(row.evidence or {}), **evidence}
                    row.analysis_notes = notes
                    if authority is not None:
                        row.authority = authority
                    if not (row.permit_group or '').strip():
                        row.permit_group = group
                    to_update.append(row)
            if to_create:
                PermitMatrix.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
            if to_update:
                PermitMatrix.objects.bulk_update(
                    to_update,
                    ['evidence', 'analysis_notes', 'permit_group', 'authority', 'updated_at'],
                    batch_size=500,
                )
            # Traceability events — same contract as _upsert_permit, batched.
            known_events = set(
                PermitEvent.objects.filter(
                    event='IDENTIFIED',
                    permit_id__in=[r.permit_id for r in to_create],
                ).values_list('permit_id', flat=True)
            )
            PermitEvent.objects.bulk_create(
                [
                    PermitEvent(
                        permit_id=r.permit_id,
                        event='IDENTIFIED',
                        detail={'rule_id': rule.rule_id, 'created': True},
                    )
                    for r in to_create
                    if r.permit_id not in known_events
                ],
                batch_size=500,
                ignore_conflicts=True,
            )
            fired = len(to_create) + len(to_update)
            summary['rules_fired'].append({'rule_id': rule_def.rule_id, 'rows': fired})
            summary['rows_created'] += fired

    # Fill the municipality on the rows that were just created: the trench-layer
    # pass above runs before any row exists, so a first analysis would leave
    # Gemeinden blank until a second run. Cheap (a few SQL statements) and it
    # never overwrites a manual value.
    if summary['rows_created']:
        try:
            muni_late = attribute_municipality(project_id)
            if muni_late.get('rows_updated'):
                summary['notes'].append(
                    f"municipality filled on {muni_late['rows_updated']} new " f"matrix row(s)"
                )
        except Exception as exc:  # noqa: BLE001 - never fail the analysis
            summary['notes'].append(f'municipality backfill skipped: {exc}')

    # Refresh readiness on every touched row (evidence satisfaction check).
    _refresh_readiness(project_id)
    return summary


def _refresh_readiness(project_id: str) -> None:
    """Recompute readiness_pct for a project's permit rows and auto-manage
    the pre-submission statuses (identified → evidence_required → ready).

    A row becomes READY as soon as its rule's evidence checklist is
    satisfied and falls back to EVIDENCE_REQUIRED / IDENTIFIED when evidence
    is missing again (design status flow). Rows past the auto-managed stage
    (submitted / under_review / approved / rejected / closed) are never
    touched — the review flow owns them. Bulk-updates only the rows whose
    pct/status changed (avoids N individual UPDATEs over a remote DB — the
    permit matrix can be thousands of rows).
    """
    rows = list(PermitMatrix.objects.filter(project_id=project_id).select_related('rule'))
    changed: list[PermitMatrix] = []
    events: list[PermitEvent] = []
    for pm in rows:
        rule = pm.rule
        if not rule:
            continue
        required_keys = rule.evidence_required or []
        if not required_keys:
            pct = 100
        else:
            present = sum(1 for k in required_keys if (pm.evidence.get(k) or {}).get('present'))
            pct = round(present / len(required_keys) * 100)
        new_status = pm.status_for_readiness(pct)
        if pct == pm.readiness_pct and new_status == pm.status:
            continue
        pm.readiness_pct = pct
        if new_status and new_status != pm.status:
            events.append(
                PermitEvent(
                    permit=pm,
                    event='STATUS_UPDATE',
                    detail={
                        'auto': True,
                        'from': pm.status,
                        'to': new_status,
                        'readiness_pct': pct,
                        'by': 'readiness_refresh',
                    },
                )
            )
            pm.status = new_status
        changed.append(pm)
    if changed:
        PermitMatrix.objects.bulk_update(changed, ['readiness_pct', 'status'], batch_size=500)
    if events:
        PermitEvent.objects.bulk_create(events, batch_size=500)


def project_summary(project_id: str) -> dict:
    """Aggregate the permit matrix for one project (status counts + readiness).

    ``total`` counts segment rows; ``total_groups`` counts street-level permit
    groups (rule × ``permit_group``, falling back to the route section for
    ungrouped rows) — the clubbed number the UI surfaces as "permits".
    """
    rows = list(
        PermitMatrix.objects.filter(project_id=project_id).select_related('authority', 'rule')
    )
    counts: dict[str, int] = {}
    groups: set[tuple] = set()
    for pm in rows:
        counts[pm.status] = counts.get(pm.status, 0) + 1
        groups.add((pm.rule.rule_id if pm.rule else '', pm.permit_group or pm.route_section))
    avg = round(sum(pm.readiness_pct for pm in rows) / len(rows)) if rows else 0
    return {
        'project_id': project_id,
        'total': len(rows),
        'total_groups': len(groups),
        'by_status': counts,
        'readiness_pct': avg,
        'blocks_construction': any(pm.blocks_construction and pm.required for pm in rows),
    }
