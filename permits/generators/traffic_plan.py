"""Traffic management plan (TMP) generator — rules-driven.

A TMP is produced per trench type / surface combination (the granularity
the construction phase works at), derived deterministically from:

* road class / surface (from the final_trenches SURFACE + fclass)
* construction method (CONSTRUCT) and reinstatement type (REINSTATE)
* aggregate trench length and segment count for the combination

The rule table mirrors the phase-1.5 traffic rule (TRAFFIC_001): surfaced
routes (asphalt/footpath/concrete) need a TMP; grass/private routes get a
lighter "works only" note.
"""

from __future__ import annotations

import html as _html
from datetime import UTC, datetime
from typing import Any

from . import data


def _e(v: Any) -> str:
    return _html.escape('' if v is None else str(v))


# Severity tiers: (min_total_length_m, label, lane_impact, measures)
_TIERS = [
    (
        2000,
        'Major',
        'Full lane closure with diversion',
        [
            'Lane closure with advance warning signs (500 m, 200 m, 100 m)',
            'Signed diversion route with temporary road markings',
            'Pedestrian diversion with barriers and tactile guidance',
            'Traffic light control (or banksman) at the work zone',
            'Work zone protected by Type-2 barriers and delineators',
            'Emergency access maintained at all times',
        ],
    ),
    (
        500,
        'Moderate',
        'Partial lane closure (lane shift)',
        [
            'Partial lane closure with taper and temporary markings',
            'Pedestrian crossing maintained with temporary crossing point',
            'Work zone barriers with reflective delineators',
            'Advance warning signs (200 m, 100 m)',
        ],
    ),
    (
        0,
        'Minor',
        'No lane closure — verge/footway works',
        [
            'Coned-off work zone with pedestrian diversion on footway',
            'Advance warning signs (100 m)',
            'Works under permit hours only',
        ],
    ),
]

# Reinstatement by surface type.
_REINSTATEMENT = {
    'Asphalt': 'Hot-rolled asphalt reinstatement in two layers, flush with existing surface',
    'Footpath': 'Concrete paving slab / block reinstatement matching adjacent surface',
    'Concrete': 'Concrete reinstatement with expansion joints',
    'Grass': 'Topsoil replacement and grass seeding (approved seed mix)',
    'Paving': 'Paving slab reinstatement matching existing pattern',
}

# Construction method -> typical equipment / note.
_METHODS = {
    'Open Cut': 'Trenching machine + breaker, excavated material in temporary stockpile',
    'Micro Trenching': 'Micro-trenching saw + vacuum excavation, surface reinstatement immediate',
    'HDD': 'Horizontal directional drilling rig — no open excavation at crossings',
    'Mole Plough': 'Mole plough / impact moling — minimal surface disturbance',
    'Trenchless': 'Trenchless installation with launch/reception pits',
    'Aerial': 'No excavation — pole works only',
}


def _tmp_page(
    title: str,
    surface: str,
    ttype: str,
    length_m: float,
    seg_count: int,
    method: str,
    depth_mm: int | None,
    width_mm: int | None,
) -> str:
    tier = _TIERS[0]
    for t in _TIERS:
        if length_m >= t[0]:
            tier = t
            break
    now = datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')
    reinstatement = _REINSTATEMENT.get(surface, 'Reinstate to original condition')
    method_note = _METHODS.get(method, 'Standard open-cut construction')
    measures = ''.join(f'<li>{_e(m)}</li>' for m in tier[2])
    specs = ''
    if depth_mm:
        specs += f'<li>Typical depth: <strong>{int(depth_mm)} mm</strong></li>'
    if width_mm:
        specs += f'<li>Typical width: <strong>{int(width_mm)} mm</strong></li>'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Traffic Management Plan — {_e(title)}</title></head>
<body style="font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#111827;">
  <h2 style="margin:0 0 4px;">Traffic Management Plan (TMP)</h2>
  <p style="margin:0 0 16px;color:#6B7280;font-size:12px;">{_e(title)} — rules-derived from the LLD design.</p>
  <table style="border-collapse:collapse;width:100%;font-size:12px;">
    <tr><th style="text-align:left;width:220px;padding:6px 8px;border:1px solid #E5E7EB;background:#F9FAFB;">Trench Type</th>
        <td style="padding:6px 8px;border:1px solid #E5E7EB;">{_e(ttype)}</td></tr>
    <tr><th style="text-align:left;padding:6px 8px;border:1px solid #E5E7EB;background:#F9FAFB;">Surface</th>
        <td style="padding:6px 8px;border:1px solid #E5E7EB;">{_e(surface)}</td></tr>
    <tr><th style="text-align:left;padding:6px 8px;border:1px solid #E5E7EB;background:#F9FAFB;">Total Length</th>
        <td style="padding:6px 8px;border:1px solid #E5E7EB;">{length_m:,.0f} m across {seg_count} segments</td></tr>
    <tr><th style="text-align:left;padding:6px 8px;border:1px solid #E5E7EB;background:#F9FAFB;">Construction Method</th>
        <td style="padding:6px 8px;border:1px solid #E5E7EB;">{_e(method)}</td></tr>
    <tr><th style="text-align:left;padding:6px 8px;border:1px solid #E5E7EB;background:#F9FAFB;">Impact Level</th>
        <td style="padding:6px 8px;border:1px solid #E5E7EB;">{_e(tier[1])} — {_e(tier[2])}</td></tr>
    <tr><th style="text-align:left;padding:6px 8px;border:1px solid #E5E7EB;background:#F9FAFB;">Reinstatement</th>
        <td style="padding:6px 8px;border:1px solid #E5E7EB;">{_e(reinstatement)}</td></tr>
  </table>
  <h3 style="margin:18px 0 8px;font-size:14px;">Construction details</h3>
  <ul style="font-size:12px;line-height:1.6;">{specs}<li>{_e(method_note)}</li></ul>
  <h3 style="margin:18px 0 8px;font-size:14px;">Traffic management measures</h3>
  <ul style="font-size:12px;line-height:1.6;">{measures}</ul>
  <h3 style="margin:18px 0 8px;font-size:14px;">Permit conditions</h3>
  <ul style="font-size:12px;line-height:1.6;">
    <li>Works permitted within the approved corridor only.</li>
    <li>Street furniture and utilities to be protected; discovery of unknown utilities — stop work and re-assess.</li>
    <li>Signing and guarding to comply with the road authority's requirements.</li>
    <li>Night/weekend working only where the authority permit allows.</li>
    <li>Photographic record of pre-works and reinstatement condition.</li>
  </ul>
  <p style="margin-top:24px;font-size:10px;color:#9CA3AF;">Generated {now} by the FTTH permit engine.</p>
</body></html>
"""


def traffic_plans(project_id: str) -> list[dict[str, Any]]:
    """One TMP per trench type/surface combination in final_trenches."""
    combos: dict[tuple[str, str], dict[str, Any]] = {}
    for f in data.lld_layer_features(project_id, 'final_trenches'):
        p = data._props(f)
        ttype = (p.get('trench_type') or 'Unknown').strip() or 'Unknown'
        surf = (p.get('SURFACE') or 'Unknown').strip() or 'Unknown'
        key = (ttype, surf)
        c = combos.setdefault(key, {'length_m': 0.0, 'count': 0})
        c['length_m'] += (
            data._as_float(p.get('length_m')) or data._as_float(p.get('distance_m')) or 0.0
        )
        c['count'] += 1
        c['method'] = p.get('CONSTRUCT') or 'Open Cut'
        c['depth_mm'] = data._as_float(p.get('DEPTH_MM'))
        c['width_mm'] = data._as_float(p.get('WIDTH_MM'))
    out = []
    for (ttype, surf), c in sorted(combos.items()):
        slug = f"{ttype.lower().replace(' ', '_')}_{surf.lower().replace(' ', '_')}"
        title = f'TMP — {ttype} trench on {surf}'
        out.append(
            {
                'name': f'tmp_{slug}',
                'kind': 'TMP',
                'filename': f'traffic/tmp_{slug}.html',
                'content': _tmp_page(
                    title,
                    surf,
                    ttype,
                    c['length_m'],
                    c['count'],
                    c['method'],
                    c['depth_mm'],
                    c['width_mm'],
                ),
                'description': title,
            }
        )
    return out
