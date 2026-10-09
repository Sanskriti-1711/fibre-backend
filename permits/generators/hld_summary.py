"""HLD preliminary permit summary generator.

The HLD has no field photographs yet, so image cells are explicit placeholders
and later survey image references can be inserted without changing the layout.
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from . import data


def _e(value: Any) -> str:
    return html.escape('' if value is None else str(value))


def _value(props: dict[str, Any], *keys: str, default: Any = '') -> Any:
    for key in keys:
        if props.get(key) not in (None, ''):
            return props[key]
    return default


def _street_name(props: dict[str, Any]) -> str:
    return str(
        _value(
            props, 'street_name', 'STREET_NAME', 'road_name', 'ROAD_NAME', default='Unnamed street'
        )
    )


def _permit_road_fields() -> list[str]:
    """Fields mirrored from docs/permit_road.docx's main/street form."""
    return [
        'Permit Reference No',
        'Project Area',
        'Exchange / CO',
        'Permit Authority',
        'Traffic Authority',
        'Utility Authority',
        'Applicant Company',
        'Prime Contractor',
        'Permit Coordinator',
        'Mobile No',
        'Email',
        'Submission Date',
        'Planned Start Date',
        'Planned End Date',
        'Permit Valid Until',
        'Version',
        'Street ID',
        'Ward No',
        'Start Location',
        'End Location',
        'GPS Start Coordinate',
        'GPS End Coordinate',
        'Road Category',
        'Road Width',
        'Land Use Type',
        'UG Length',
        'Aerial Length',
        'Open Trench Length',
        'Micro Trench Length',
        'HDD Length',
        'Existing Duct Reuse Length',
        'Trench Width',
        'Trench Depth',
        'Duct Configuration',
        'Road Crossing HDD',
        'Footpath Crossing',
        'Junction Crossing',
        'Bridge Crossing',
        'Rail Crossing',
        'Utility Crossing Count',
        'Pole Owner',
        'Pole Count',
        'Span Count',
        'Average Span',
        'Maximum Span',
        'Attachment Height',
        'Cable Type',
        'Messenger Required',
        'Pole Replacement Required',
        'Civil Contractor',
        'Fiber Contractor',
        'Traffic Management Contractor',
        'Crew Size',
        'Equipment Used',
        'Working Window',
        'Weekend Work',
        'Night Work',
        'Traffic Control Required',
        'Expected Duration',
        'Water Pipeline Crossing',
        'Water Utility Owner',
        'Existing Power Cable',
        'Telecom Duct Present',
        'Gas Pipeline Present',
        'Sewer Line Present',
        'Conflict Risk',
        'Protection Method',
        'Daily Traffic Volume',
        'Lane Closure',
        'Detour Required',
        'Barricades',
        'Traffic Cones',
        'Warning Boards',
        'Flash Lights',
        'Traffic Marshals',
        'Pedestrian Access Maintained',
        'Emergency Vehicle Access',
        'TMP Drawing Reference',
        'Restoration Method',
        'Restoration Width',
        'Restoration Length',
        'Inspection Required',
        'Defect Liability Period',
        'Telecom Design Approval',
        'Municipality Approval',
        'Traffic Police Approval',
        'Utility Clearance',
        'Safety Approval',
        'Permit Status',
    ]


def generate_hld_summary(project_id: str, project_name: str) -> dict[str, Any]:
    """Build one street-wise, planning-only HTML summary from HLD layers."""
    # HLD is the sole source for this preliminary document. Use the persisted
    # HLD attribute table, never the latest LLD run.
    # One row per continuous civil section, expanded from the grouped
    # multipart trench geometry (see data.hld_layer_sections). Each physical
    # trench appears exactly once — the pipeline's union de-duplicates it and
    # the helper drops any repeated part — so the section table carries no
    # duplicate line items. Sub-metre noding slivers are held back from the
    # quantities and reported separately, because a trench shorter than 1 m is
    # not buildable and would otherwise inflate every number in this document.
    trenches = data.hld_layer_sections(project_id, 'trenches')

    streets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            'sections': [],
            'types': defaultdict(lambda: {'count': 0, 'length': 0.0}),
            'traffic': set(),
            'images': [],
        }
    )
    totals: dict[str, dict[str, Any]] = defaultdict(lambda: {'count': 0, 'length': 0.0})
    slivers: dict[str, dict[str, Any]] = defaultdict(lambda: {'count': 0, 'length': 0.0})

    for props in trenches:
        street = _street_name(props)
        trench_type = str(
            _value(
                props,
                'trench_type',
                'TRENCH_TYPE',
                'construction_method',
                'CONSTRUCT',
                default='Unknown',
            )
        )
        method = str(_value(props, 'CONSTRUCT', 'construction_method', default=trench_type))
        length = (
            data._as_float(
                _value(props, 'SECTION_LEN_M', 'length_m', 'LENGTH_M', 'distance_m', default=0)
            )
            or 0.0
        )
        depth = _value(props, 'DEPTH_MM', 'depth_mm', 'depth', default='—')
        width = _value(props, 'WIDTH_MM', 'width_mm', 'width', default='—')
        surface = _value(props, 'SURFACE', 'surface', default='—')
        section_id = _value(
            props, 'SECTION_ID', 'feature_id', 'FEATURE_ID', 'fid', 'id', default='—'
        )
        traffic = _value(
            props,
            'fclass',
            'road_class',
            'ROAD_CLASS',
            'street',
            'STREET',
            default='Not classified',
        )
        image = _value(
            props,
            'image_url',
            'IMAGE_URL',
            'photo_url',
            'PHOTO_URL',
            default='No field image — survey pending',
        )
        if data._as_float(_value(props, 'SLIVER', default=0)):
            slivers[trench_type]['count'] += 1
            slivers[trench_type]['length'] += length
            continue
        row = streets[street]
        row['sections'].append(
            (section_id, trench_type, method, length, width, depth, surface, str(image))
        )
        row['types'][trench_type]['count'] += 1
        row['types'][trench_type]['length'] += length
        row['traffic'].add(str(traffic))
        row['images'].append(str(image))
        totals[trench_type]['count'] += 1
        totals[trench_type]['length'] += length

    street_blocks = []
    for street, info in sorted(streets.items()):
        type_summary = (
            '; '.join(
                f"{kind}: {bucket['length']:.1f} m ({bucket['count']} sections)"
                for kind, bucket in sorted(info['types'].items())
            )
            or 'No trench sections'
        )
        rows = ''.join(
            f'<tr><td>{_e(section)}</td><td>{_e(kind)}</td><td>{_e(method)}</td>'
            f'<td>{length:.1f} m</td><td>{_e(width)} mm</td><td>{_e(depth)} mm</td>'
            f'<td>{_e(surface)}</td><td>{_e(image)}</td></tr>'
            for section, kind, method, length, width, depth, surface, image in info['sections']
        )
        street_blocks.append(
            f"""
<section class="street">
<h2>{_e(street)}</h2>
<p><strong>Trench summary:</strong> {_e(type_summary)}<br>
<strong>Traffic / road classes:</strong> {_e(', '.join(sorted(info['traffic'])))}<br>
<strong>Traffic management:</strong> Preliminary street plan based on HLD attributes; confirm lane impact, work window, signing, guarding, pedestrian and emergency access during Survey/LLD.</p>
<table><thead><tr><th>Section</th><th>Trench type</th><th>Method</th><th>Length</th><th>Width</th><th>Depth</th><th>Surface</th><th>Image / survey reference</th></tr></thead><tbody>{rows}</tbody></table>
</section>"""
        )

    total_rows = (
        ''.join(
            f"<tr><td>{_e(kind)}</td><td>{bucket['count']}</td><td>{bucket['length']:.1f} m</td></tr>"
            for kind, bucket in sorted(totals.items())
        )
        or '<tr><td colspan="3">No trench data available.</td></tr>'
    )

    sliver_note = ''
    if slivers:
        detail = ', '.join(
            f"{kind}: {b['count']} sections / {b['length']:.1f} m"
            for kind, b in sorted(slivers.items())
        )
        sliver_note = (
            '<p class="small">Excluded from the quantities above: sub-metre noding slivers — '
            f'{_e(detail)}. These are union/noding artefacts below the 1 m buildable threshold '
            '(still present in the trench layer, flagged SLIVER=1).</p>'
        )

    # ── Chamber-to-chamber duct sections ─────────────────────────────────
    # Ducts are CONTINUOUS corridors (geometry never splits); the chamber-
    # bounded civil sections are recorded per feature in SECTIONS_JSON
    # [{start, end, length_m}, …] with the ordered chain in SECTION_CHAIN.
    # Sections longer than REVIEW_RUN_M need an intermediate pull point —
    # flag them REVIEW so the planner checks chamber spacing.
    REVIEW_RUN_M = 150.0
    ducts = data.hld_layer_features(project_id, 'ducts')
    run_rows: list[str] = []
    review_items: list[tuple[str, str, str, float]] = []  # (duct_type, id, section, len)
    corridor_totals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            'sections': 0,
            'length': 0.0,
            'min_len': float('inf'),
            'max_len': 0.0,
            'review': 0,
            'bundle': 0.0,
            'runs': 0,
            'clubs': 0,
        }
    )
    for feature in ducts:
        props = data._props(feature)
        duct_type = str(_value(props, 'DUCT_TYPE', 'duct_type', default='Duct'))
        length = data._as_float(_value(props, 'LENGTH_M', 'length_m', default=0)) or 0.0
        # The published corridor is CLUBBED — every duct on the same route is
        # dissolved into one line per street — while BUNDLE_LEN_M carries the
        # duct material actually laid (the parallel runs), N_DUCTS how many
        # parallel ducts the corridor needs and CLUBS how many routes it merged.
        bundle = data._as_float(_value(props, 'BUNDLE_LEN_M', 'bundle_len_m', default=0)) or 0.0
        n_runs = _value(props, 'N_DUCTS', 'n_ducts', default=0)
        n_clubs = _value(props, 'CLUBS', 'clubs', default=0)
        ways = _value(props, 'WAYS', 'ways', default='—')
        occupancy = _value(props, 'OCCUPANCY_PCT', 'occupancy_pct', default='—')
        corridor_id = _value(props, 'feature_id', 'FEATURE_ID', 'fid', 'id', default='—')
        sections_raw = _value(props, 'SECTIONS_JSON', 'sections_json', default='')
        sections: list[dict] = []
        if sections_raw:
            try:
                parsed = json.loads(sections_raw) if isinstance(sections_raw, str) else sections_raw
                if isinstance(parsed, list):
                    sections = [s for s in parsed if isinstance(s, dict)]
            except Exception:
                sections = []
        if not sections:
            # Corridor with no chamber splices — one whole-length section.
            sections = [{'start': None, 'end': None, 'length_m': length}]
        for idx, sec in enumerate(sections, 1):
            sec_len = data._as_float(sec.get('length_m')) or 0.0
            s_id = sec.get('start') or 'Project entry'
            e_id = sec.get('end') or 'Network edge'
            is_review = sec_len > REVIEW_RUN_M
            row_cls = ' class="review"' if is_review else ''
            run_rows.append(
                f"<tr{row_cls}><td>{_e(duct_type)}</td><td>{_e(corridor_id)}·{idx}</td>"
                f"<td>{_e(s_id)} → {_e(e_id)}</td><td>{sec_len:.1f} m</td>"
                f"<td>{_e(ways)}</td><td>{_e(occupancy)}%</td>"
                f"<td>{'REVIEW — pull point check' if is_review else ''}</td></tr>"
            )
            if is_review:
                review_items.append(
                    (duct_type, f'{corridor_id}·{idx}', f'{s_id} → {e_id}', sec_len)
                )
            bucket = corridor_totals[duct_type]
            bucket['sections'] += 1
            bucket['length'] += sec_len
            bucket['min_len'] = min(bucket['min_len'], sec_len)
            bucket['max_len'] = max(bucket['max_len'], sec_len)
            if is_review:
                bucket['review'] += 1
        bucket = corridor_totals[duct_type]
        bucket['bundle'] += bundle
        bucket['runs'] = max(bucket['runs'], int(n_runs or 0))
        bucket['clubs'] = max(bucket['clubs'], int(n_clubs or 0))

    corridor_rows = (
        ''.join(
            f"<tr><td>{_e(kind)}</td><td>{bucket['clubs']}</td><td>{bucket['sections']}</td>"
            f"<td>{bucket['length']:.1f} m</td>"
            f"<td>{bucket['min_len']:.1f} m</td><td>{bucket['max_len']:.1f} m</td>"
            f"<td>{bucket['runs']}</td><td>{bucket['bundle']:.1f} m</td>"
            f"<td>{bucket['review'] or ''}</td></tr>"
            for kind, bucket in sorted(corridor_totals.items())
        )
        or '<tr><td colspan="9">No duct data available.</td></tr>'
    )

    review_rows = (
        ''.join(
            f'<tr><td>{_e(dt)}</td><td>{_e(sid)}</td><td>{_e(run)}</td><td>{ln:.1f} m</td>'
            f'<td>Add/verify intermediate pull chamber (duct max pull distance ≈ 150 m for HDPE 32/63)</td></tr>'
            for dt, sid, run, ln in sorted(review_items, key=lambda r: -r[3])
        )
        or '<tr><td colspan="5">No runs exceed the 150 m pull threshold — no review items.</td></tr>'
    )

    generated = datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')
    content = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>HLD Permit Summary — {_e(project_name)}</title>
<style>body{{font-family:Arial,sans-serif;color:#111827;margin:28px}}h1{{font-size:22px}}h2{{font-size:17px;margin-bottom:6px}}.notice{{padding:12px;background:#fff7ed;border:2px solid #fb923c;border-radius:7px}}.street{{page-break-inside:avoid;margin-top:28px}}table{{border-collapse:collapse;width:100%;font-size:11px}}th,td{{border:1px solid #d1d5db;padding:5px;text-align:left}}th{{background:#f3f4f6}}.small{{font-size:11px;color:#6b7280}}tr.review td{{background:#fef2f2;color:#991b1b;font-weight:600}}</style></head><body>
<h1>Preliminary HLD Permit Summary</h1><p class="small">Project: {_e(project_name)} · ID: {_e(project_id)} · Generated: {generated}</p>
<div class="notice"><strong>PLANNING ONLY — NOT FOR CONSTRUCTION OR AUTHORITY SUBMISSION.</strong><br>Dimensions, street conditions, traffic controls and photographs must be verified and replaced/confirmed during the field survey and LLD.</div>
<h2>Overall trench summary</h2><table><thead><tr><th>Trench type / method</th><th>Sections</th><th>Total length</th></tr></thead><tbody>{total_rows}</tbody></table>
{sliver_note}
<h2>Duct sections between chambers</h2><p class="small">Ducts are continuous corridors that pass through chambers; the chamber-bounded civil sections within each corridor are listed here (from the corridor's section chain) — the build units a contractor constructs and prices. Sections ending at “Network edge” terminate at a PDP / polygon entry rather than an intermediate chamber. Sections longer than 150 m are flagged <strong>REVIEW</strong>: verify an intermediate pull chamber exists before construction.</p>
<p class="small">Ducts on similar routes are <strong>clubbed</strong>: all the parallel ducts laid along one street are dissolved into a single corridor line, so each street appears once. <em>Corridor length</em> is the street footage; <em>parallel runs</em> is how many {ways}-way ducts that corridor needs; <em>duct material</em> is the duct length actually laid (the parallel runs summed) and is what the BOQ bills. REVIEW marks corridors still needing a pull-point check.</p>
<table><thead><tr><th>Duct type</th><th>Clubbed routes</th><th>Sections</th><th>Corridor length</th><th>Shortest section</th><th>Longest section</th><th>Parallel runs</th><th>Duct material</th><th>REVIEW (&gt;150 m)</th></tr></thead><tbody>{corridor_rows}</tbody></table>
<h2>REVIEW — sections exceeding 150 m pull threshold</h2><p class="small">Standard HDPE 32/63 sub-duct pulling distance is ≈ 150 m between chambers. Each section below needs an intermediate pull point added (or a verified existing one) before construction. Sorted longest first.</p>
<table><thead><tr><th>Duct type</th><th>Section</th><th>Run (start → end chamber)</th><th>Length</th><th>Action</th></tr></thead><tbody>{review_rows}</tbody></table>
<details><summary style="cursor:pointer;font-size:13px;margin:10px 0 4px"><strong>All duct sections (section-wise)</strong> — click to expand {_e(len(run_rows))} rows</summary>
<table><thead><tr><th>Duct type</th><th>Section</th><th>Run (start → end chamber)</th><th>Length</th><th>Ways</th><th>Occupancy</th></tr></thead><tbody>{''.join(run_rows) or '<tr><td colspan="6">No duct runs available.</td></tr>'}</tbody></table>
</details>
<h2>Street-wise design and permit information</h2>{''.join(street_blocks) or '<p>No trench features were available in the HLD output.</p>'}
<h2>Permit-road form fields</h2><p class="small">The following fields from <strong>docs/permit_road.docx</strong> are represented by the HLD data where available. Values not present in HLD are marked “To be completed during Survey/LLD”; no sample values are copied into the project.</p>
<table><thead><tr><th>Field</th><th>HLD value / status</th></tr></thead><tbody>{''.join(f'<tr><td>{_e(field)}</td><td>To be completed during Survey/LLD</td></tr>' for field in _permit_road_fields())}</tbody></table>
<p class="small">HDD/road-crossing sections should be confirmed from the generated trench attributes. Survey photographs are intentionally shown as pending until field evidence is attached.</p>
</body></html>"""
    return {
        'name': 'HLD street-wise permit summary',
        'kind': 'REPORT',
        'filename': 'hld_preliminary/hld_street_permit_summary.html',
        'content': content,
        'description': 'Street-wise HLD permit summary with trench dimensions, traffic and image references',
    }
