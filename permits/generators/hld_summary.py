"""HLD preliminary permit summary generator.

The HLD has no field photographs yet, so image cells are explicit placeholders
and later survey image references can be inserted without changing the layout.
"""
from __future__ import annotations

import html
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from . import data


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _value(props: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if props.get(key) not in (None, ""):
            return props[key]
    return default


def _street_name(props: dict[str, Any]) -> str:
    return str(_value(props, "street_name", "STREET_NAME", "road_name", "ROAD_NAME", default="Unnamed street"))


def _permit_road_fields() -> list[str]:
    """Fields mirrored from docs/permit_road.docx's main/street form."""
    return [
        "Permit Reference No", "Project Area", "Exchange / CO", "Permit Authority",
        "Traffic Authority", "Utility Authority", "Applicant Company", "Prime Contractor",
        "Permit Coordinator", "Mobile No", "Email", "Submission Date", "Planned Start Date",
        "Planned End Date", "Permit Valid Until", "Version", "Street ID", "Ward No",
        "Start Location", "End Location", "GPS Start Coordinate", "GPS End Coordinate",
        "Road Category", "Road Width", "Land Use Type", "UG Length", "Aerial Length",
        "Open Trench Length", "Micro Trench Length", "HDD Length", "Existing Duct Reuse Length",
        "Trench Width", "Trench Depth", "Duct Configuration", "Road Crossing HDD",
        "Footpath Crossing", "Junction Crossing", "Bridge Crossing", "Rail Crossing",
        "Utility Crossing Count", "Pole Owner", "Pole Count", "Span Count", "Average Span",
        "Maximum Span", "Attachment Height", "Cable Type", "Messenger Required",
        "Pole Replacement Required", "Civil Contractor", "Fiber Contractor",
        "Traffic Management Contractor", "Crew Size", "Equipment Used", "Working Window",
        "Weekend Work", "Night Work", "Traffic Control Required", "Expected Duration",
        "Water Pipeline Crossing", "Water Utility Owner", "Existing Power Cable",
        "Telecom Duct Present", "Gas Pipeline Present", "Sewer Line Present", "Conflict Risk",
        "Protection Method", "Daily Traffic Volume", "Lane Closure", "Detour Required",
        "Barricades", "Traffic Cones", "Warning Boards", "Flash Lights", "Traffic Marshals",
        "Pedestrian Access Maintained", "Emergency Vehicle Access", "TMP Drawing Reference",
        "Restoration Method", "Restoration Width", "Restoration Length", "Inspection Required",
        "Defect Liability Period", "Telecom Design Approval", "Municipality Approval",
        "Traffic Police Approval", "Utility Clearance", "Safety Approval", "Permit Status",
    ]


def generate_hld_summary(project_id: str, project_name: str) -> dict[str, Any]:
    """Build one street-wise, planning-only HTML summary from HLD layers."""
    # HLD is the sole source for this preliminary document. Use the persisted
    # HLD attribute table, never the latest LLD run.
    trenches = data.hld_layer_features(project_id, "trenches")

    streets: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "sections": [], "types": defaultdict(lambda: {"count": 0, "length": 0.0}),
        "traffic": set(), "images": [],
    })
    totals: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "length": 0.0})

    for feature in trenches:
        props = data._props(feature)
        street = _street_name(props)
        trench_type = str(_value(props, "trench_type", "TRENCH_TYPE", "construction_method", "CONSTRUCT", default="Unknown"))
        method = str(_value(props, "CONSTRUCT", "construction_method", default=trench_type))
        length = data._as_float(_value(props, "length_m", "LENGTH_M", "distance_m", default=0)) or 0.0
        depth = _value(props, "DEPTH_MM", "depth_mm", "depth", default="—")
        width = _value(props, "WIDTH_MM", "width_mm", "width", default="—")
        surface = _value(props, "SURFACE", "surface", default="—")
        section_id = _value(props, "feature_id", "FEATURE_ID", "fid", "id", default="—")
        traffic = _value(props, "fclass", "road_class", "ROAD_CLASS", "street", "STREET", default="Not classified")
        image = _value(props, "image_url", "IMAGE_URL", "photo_url", "PHOTO_URL", default="No field image — survey pending")
        row = streets[street]
        row["sections"].append((section_id, trench_type, method, length, width, depth, surface, str(image)))
        row["types"][trench_type]["count"] += 1
        row["types"][trench_type]["length"] += length
        row["traffic"].add(str(traffic))
        row["images"].append(str(image))
        totals[trench_type]["count"] += 1
        totals[trench_type]["length"] += length

    street_blocks = []
    for street, info in sorted(streets.items()):
        type_summary = "; ".join(
            f"{kind}: {bucket['length']:.1f} m ({bucket['count']} sections)"
            for kind, bucket in sorted(info["types"].items())
        ) or "No trench sections"
        rows = "".join(
            f"<tr><td>{_e(section)}</td><td>{_e(kind)}</td><td>{_e(method)}</td>"
            f"<td>{length:.1f} m</td><td>{_e(width)} mm</td><td>{_e(depth)} mm</td>"
            f"<td>{_e(surface)}</td><td>{_e(image)}</td></tr>"
            for section, kind, method, length, width, depth, surface, image in info["sections"]
        )
        street_blocks.append(f"""
<section class="street">
<h2>{_e(street)}</h2>
<p><strong>Trench summary:</strong> {_e(type_summary)}<br>
<strong>Traffic / road classes:</strong> {_e(', '.join(sorted(info['traffic'])))}<br>
<strong>Traffic management:</strong> Preliminary street plan based on HLD attributes; confirm lane impact, work window, signing, guarding, pedestrian and emergency access during Survey/LLD.</p>
<table><thead><tr><th>Section</th><th>Trench type</th><th>Method</th><th>Length</th><th>Width</th><th>Depth</th><th>Surface</th><th>Image / survey reference</th></tr></thead><tbody>{rows}</tbody></table>
</section>""")

    total_rows = "".join(
        f"<tr><td>{_e(kind)}</td><td>{bucket['count']}</td><td>{bucket['length']:.1f} m</td></tr>"
        for kind, bucket in sorted(totals.items())
    ) or '<tr><td colspan="3">No trench data available.</td></tr>'
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    content = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>HLD Permit Summary — {_e(project_name)}</title>
<style>body{{font-family:Arial,sans-serif;color:#111827;margin:28px}}h1{{font-size:22px}}h2{{font-size:17px;margin-bottom:6px}}.notice{{padding:12px;background:#fff7ed;border:2px solid #fb923c;border-radius:7px}}.street{{page-break-inside:avoid;margin-top:28px}}table{{border-collapse:collapse;width:100%;font-size:11px}}th,td{{border:1px solid #d1d5db;padding:5px;text-align:left}}th{{background:#f3f4f6}}.small{{font-size:11px;color:#6b7280}}</style></head><body>
<h1>Preliminary HLD Permit Summary</h1><p class="small">Project: {_e(project_name)} · ID: {_e(project_id)} · Generated: {generated}</p>
<div class="notice"><strong>PLANNING ONLY — NOT FOR CONSTRUCTION OR AUTHORITY SUBMISSION.</strong><br>Dimensions, street conditions, traffic controls and photographs must be verified and replaced/confirmed during the field survey and LLD.</div>
<h2>Overall trench summary</h2><table><thead><tr><th>Trench type / method</th><th>Sections</th><th>Total length</th></tr></thead><tbody>{total_rows}</tbody></table>
<h2>Street-wise design and permit information</h2>{''.join(street_blocks) or '<p>No trench features were available in the HLD output.</p>'}
<h2>Permit-road form fields</h2><p class="small">The following fields from <strong>docs/permit_road.docx</strong> are represented by the HLD data where available. Values not present in HLD are marked “To be completed during Survey/LLD”; no sample values are copied into the project.</p>
<table><thead><tr><th>Field</th><th>HLD value / status</th></tr></thead><tbody>{''.join(f'<tr><td>{_e(field)}</td><td>To be completed during Survey/LLD</td></tr>' for field in _permit_road_fields())}</tbody></table>
<p class="small">HDD/road-crossing sections should be confirmed from the generated trench attributes. Survey photographs are intentionally shown as pending until field evidence is attached.</p>
</body></html>"""
    return {"name": "HLD street-wise permit summary", "kind": "REPORT", "filename": "hld_preliminary/hld_street_permit_summary.html", "content": content, "description": "Street-wise HLD permit summary with trench dimensions, traffic and image references"}
