"""Data access for permit package generators.

LLD generators consume ``business.ftth_lld_layers``. HLD preliminary
packaging consumes the persisted ``ftth_hld_layers`` attribute tables through
``hld_layer_features`` so it is never accidentally based on LLD output.
"""

from __future__ import annotations

import json
import math
from typing import Any

from django.db import connection

from ..analysis.sections import (
    SLIVER_M,
    coord_key,
    feature_parts,
    line_length_m,
    part_fingerprint,
    part_midpoint,
)
from ..models import PermitMatrix

# Section table produced by ``permits.analysis.trench_sections`` (one row per
# buildable civil section, with the street it runs along).
SECTION_LAYER = 'trench_sections'

# Layers the package generator consumes (final design layers).
PACKAGE_LAYERS = [
    'final_trenches',
    'distribution_ducts',
    'drop_ducts',
    'feeder_ducts',
    'chambers',
    'pdps',
    'distribution_cable',
    'feeder_cable',
    'objects',
    'polygons',
    'existing_infrastructure',
    'existing_infrastructure_points',
]


def latest_lld_run_id(project_id: str) -> str | None:
    """Id of the project's most recent LLD run, or None."""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM business.ftth_lld_runs
            WHERE ftth_project_id = %s AND status = 'completed'
            ORDER BY run_date DESC LIMIT 1
            """,
            [project_id],
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


def hld_layer_features(project_id: str, layer_name: str) -> list[dict[str, Any]]:
    """Return persisted HLD features and their original attribute columns."""
    from ftth_hld.models import FtthLayer

    row = FtthLayer.objects.filter(ftth_project__project_id=project_id, name=layer_name).first()
    if not row:
        return []
    return list((row.geojson or {}).get('features', []))


def _line_length_m(coords: list) -> float:
    """Geodesic length of a stored GeoJSON line (the HLD layer store is 4326)."""
    return line_length_m(coords)


def _section_street_map(props: dict[str, Any]) -> dict[str, Any]:
    """Per-section street attribution stamped by the section builder."""
    raw = props.get('SECTION_STREETS')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _apply_street(section: dict[str, Any], props: dict[str, Any], part: list) -> None:
    """Attach the street/road class of the road this section runs along.

    The builder keys the street per section midpoint, so the lookup is robust
    to part ordering. Absent attribution leaves the keys blank rather than
    inventing a street — the summary then reports "Unnamed street".
    """
    info = _section_street_map(props).get(coord_key(*part_midpoint(part)) or '') or {}
    if not isinstance(info, dict):
        info = {}
    section['street_name'] = info.get('street_name') or props.get('street_name') or ''
    section['fclass'] = info.get('fclass') or props.get('fclass') or ''
    section['highway'] = info.get('highway') or props.get('highway') or ''


def hld_layer_sections(project_id: str, layer_name: str) -> list[dict[str, Any]]:
    """One row per continuous geometry PART (a buildable civil section).

    Prefers the persisted ``trench_sections`` table (built by
    ``permits.analysis.trench_sections``), which already carries one row per
    section together with the street the section runs along.  Projects built
    before that table existed fall back to expanding the grouped geometry
    here, so the section tables never come back empty.

    No duplicate line items: the grouped geometry is de-duplicated by the
    pipeline's union, and identical parts are dropped here too (vertices
    rounded to 1e-6 deg ≈ 0.1 m) so a repeated part can never be billed twice.
    Sub-metre noding slivers are returned (geometry stays complete) but carry
    ``SLIVER = 1`` so callers can exclude them from quantities.
    """
    if layer_name in ('trenches', 'trench_layer', SECTION_LAYER):
        persisted = _persisted_sections(project_id)
        if persisted:
            return persisted
    return _sections_from_geometry(project_id, layer_name)


def _persisted_sections(project_id: str) -> list[dict[str, Any]]:
    """Section rows from the persisted ``trench_sections`` layer, if any."""
    rows: list[dict[str, Any]] = []
    for feature in hld_layer_features(project_id, SECTION_LAYER):
        props = dict(_props(feature))
        length = _as_float(props.get('SECTION_LEN_M')) or _as_float(props.get('length_m'))
        if not length or length <= 0:
            length = line_length_m(((feature.get('geometry') or {}).get('coordinates')) or [])
        if length <= 0:
            continue
        props.setdefault('SECTION_ID', props.get('feature_id') or '')
        props['SECTION_LEN_M'] = round(length, 2)
        props['length_m'] = round(length, 2)
        props['SLIVER'] = 1 if length < SLIVER_M else 0
        rows.append(props)
    return rows


def _sections_from_geometry(project_id: str, layer_name: str) -> list[dict[str, Any]]:
    """Fallback: expand the grouped multipart trench geometry into sections."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for feature in hld_layer_features(project_id, layer_name):
        props = _props(feature)
        for idx, part in enumerate(feature_parts(feature), 1):
            key = part_fingerprint(part)
            if len(key) < 2 or key in seen:
                continue
            seen.add(key)
            length = line_length_m(part)
            if length <= 0:
                continue
            section = dict(props)
            kind = str(props.get('trench_type') or layer_name)
            section['SECTION_ID'] = f'{kind}-{len(rows) + 1:04d}'
            section['PART_INDEX'] = idx
            section['PARTS_IN_FEATURE'] = props.get('PARTS')
            section['SECTION_LEN_M'] = round(length, 2)
            # Per-section length replaces the group total so quantities are
            # section-wise and can never be counted twice.
            section['length_m'] = round(length, 2)
            section['SLIVER'] = 1 if length < SLIVER_M else 0
            if not section.get('street_name'):
                _apply_street(section, props, part)
            rows.append(section)
    return rows


def lld_layer_features(
    project_id: str, layer_name: str, lld_run_id: str | None = None
) -> list[dict[str, Any]]:
    """Full GeoJSON features of one LLD layer for the project's latest run."""
    run_id = lld_run_id or latest_lld_run_id(project_id)
    if not run_id:
        return []
    sql = """
        SELECT f.value
        FROM business.ftth_lld_layers l,
             jsonb_array_elements(l.geojson->'features') f
        WHERE l.name = %s AND l.lld_run_id = %s
    """
    rows = []
    with connection.cursor() as cur:
        cur.execute(sql, [layer_name, run_id])
        for (feat,) in cur.fetchall():
            if isinstance(feat, str):
                feat = json.loads(feat)
            rows.append(feat or {})
    return rows


def _props(feature: dict[str, Any]) -> dict[str, Any]:
    p = feature.get('properties') or {}
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except Exception:
            p = {}
    return p


def _as_float(value: Any) -> float | None:
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def trench_stats(project_id: str) -> dict[str, Any]:
    """Aggregates over final_trenches: counts, length by type/surface,
    max depth/width for cross-sections."""
    feats = lld_layer_features(project_id, 'final_trenches')
    by_type: dict[str, dict[str, float]] = {}
    by_surface: dict[str, float] = {}
    total_m = 0.0
    max_depth = max_width = 0.0
    for f in feats:
        p = _props(f)
        ttype = (p.get('trench_type') or 'Unknown').strip() or 'Unknown'
        surf = (p.get('SURFACE') or 'Unknown').strip() or 'Unknown'
        length = _as_float(p.get('length_m')) or _as_float(p.get('distance_m')) or 0.0
        total_m += length
        bucket = by_type.setdefault(ttype, {'count': 0, 'length_m': 0.0})
        bucket['count'] += 1
        bucket['length_m'] += length
        by_surface[surf] = by_surface.get(surf, 0.0) + length
        depth = _as_float(p.get('DEPTH_MM')) or 0.0
        width = _as_float(p.get('WIDTH_MM')) or 0.0
        max_depth = max(max_depth, depth)
        max_width = max(max_width, width)
    return {
        'count': len(feats),
        'total_length_m': round(total_m, 1),
        'by_type': {
            k: {'count': v['count'], 'length_m': round(v['length_m'], 1)}
            for k, v in by_type.items()
        },
        'by_surface': {k: round(v, 1) for k, v in by_surface.items()},
        'max_depth_mm': round(max_depth),
        'max_width_mm': round(max_width),
    }


def chamber_schedule(project_id: str) -> list[dict[str, Any]]:
    feats = lld_layer_features(project_id, 'chambers')
    rows = []
    for f in feats:
        p = _props(f)
        rows.append(
            {
                'id': p.get('STRUCT_ID') or p.get('feature_id') or '',
                'chamber_type': p.get('CHAMBER_TYPE') or '',
                'size': p.get('SIZE') or '',
                'capacity_total': p.get('CAPACITY_TOTAL') or '',
                'capacity_used': p.get('CAPACITY_USED') or '',
                'equipment': p.get('EQUIPMENT') or '',
                'parent_trench': p.get('PARENT_TRENCH') or '',
                'connected_ducts': p.get('CONN_DUCTS') or '',
            }
        )
    rows.sort(key=lambda r: str(r['id']))
    return rows


def pdp_schedule(project_id: str) -> list[dict[str, Any]]:
    feats = lld_layer_features(project_id, 'pdps')
    rows = []
    for f in feats:
        p = _props(f)
        rows.append(
            {
                'id': p.get('PDP_ID') or p.get('feature_id') or '',
                'node_type': p.get('NODE_TYPE') or '',
                'equip_type': p.get('EQUIP_TYPE') or '',
                'equip_name': p.get('EQUIP_NAME') or '',
                'equip_capacity': p.get('EQUIP_CAPACITY') or '',
                'split_ratio': p.get('SPLIT_RATIO') or '',
                'split_ports': p.get('SPL_PORTS') or '',
                'hh': p.get('households') or p.get('HH') or '',
                'power_required': p.get('POWER_REQ') or '',
                'location': p.get('LOCATION') or '',
            }
        )
    rows.sort(key=lambda r: str(r['id']))
    return rows


def duct_stats(project_id: str) -> dict[str, Any]:
    dist = lld_layer_features(project_id, 'distribution_ducts')
    drop = lld_layer_features(project_id, 'drop_ducts')
    out = {}
    for label, feats in (('distribution', dist), ('drop', drop)):
        total_m = 0.0
        ways = set()
        diameters = set()
        occupancy = 0.0
        n_occ = 0
        for f in feats:
            p = _props(f)
            total_m += _as_float(p.get('length_m')) or 0.0
            if p.get('WAYS'):
                ways.add(str(p['WAYS']))
            if p.get('DIAMETER_MM'):
                diameters.add(str(p['DIAMETER_MM']))
            occ = _as_float(p.get('OCCUPANCY_PCT'))
            if occ is not None:
                occupancy += occ
                n_occ += 1
        out[label] = {
            'count': len(feats),
            'length_m': round(total_m, 1),
            'ways': sorted(ways),
            'diameters_mm': sorted(diameters),
            'avg_occupancy_pct': round(occupancy / n_occ, 1) if n_occ else 0,
        }
    return out


def permit_rows(project_id: str) -> list[PermitMatrix]:
    return list(
        PermitMatrix.objects.filter(project_id=project_id)
        .select_related('authority', 'rule')
        .order_by('permit_type', 'route_section')
    )
