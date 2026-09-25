"""HDD crossing profile drawings for the permit package (P16).

Generates one dimensioned SVG per **crossing group** (railway / waterway)
only where such crossings exist — i.e. where the permit matrix contains
RAILWAY_CROSSING_001 or WATERWAY_CROSSING_001 rows. No crossings → no files,
no readiness side-effects, no empty placeholders.

Each SVG is a schematic HDD profile (entry / bore / exit) with depth under
the corridor, bend radius and a traceability footer. The drawings satisfy the
crossing evidence keys (``hdd_design`` / ``profile_drawing`` /
``crossing_drawing``) via ``package._promote_evidence`` — but only when they
were actually generated (the promotion guard checks for the HDD file prefix,
not just any DRAWING).

Deterministic: no LLM, no GIS query beyond the permit rows. Geometry is
schematic — annotated "not to scale, to be confirmed by HDD contractor".
"""

from __future__ import annotations

import html as _html
from typing import Any

from ..models import PermitMatrix

# Crossing rules that trigger HDD drawings.
CROSSING_RULE_IDS = frozenset({"RAILWAY_CROSSING_001", "WATERWAY_CROSSING_001"})

# Human labels per rule.
_RULE_LABEL = {
    "RAILWAY_CROSSING_001": "Railway crossing — HDD profile",
    "WATERWAY_CROSSING_001": "Waterway crossing — HDD profile",
}
_RULE_SHORT = {
    "RAILWAY_CROSSING_001": "railway",
    "WATERWAY_CROSSING_001": "waterway",
}


def _e(v: Any) -> str:
    return _html.escape("" if v is None else str(v))


def _svg_hdd_profile(
    title: str,
    subtitle: str,
    street: str,
    municipality: str,
    authority: str,
    route_section: str,
    rule_id: str,
    crossing_type: str,
    *,
    bore_length_m: int = 32,
    depth_m: float = 2.5,
    radius_m: int = 80,
) -> str:
    """Dimensioned HDD profile SVG.

    Schematic side view: entry pit → bore arc → exit pit, with the
    railway/waterway corridor marked above the bore vertex.
    """
    # Canvas
    W, H = 820, 360
    margin = 40
    ground_y = 110
    bore_vertex_y = ground_y + int(depth_m * 28)  # scale for visibility
    entry_x = margin + 60
    exit_x = W - margin - 60
    vertex_x = (entry_x + exit_x) // 2
    # Control points for a smooth HDD arc (entry 10°, exit 10°)
    # Use quadratic bezier entry→vertex→exit.
    is_water = crossing_type == "waterway"
    corridor_color = "#3B82F6" if is_water else "#6B7280"
    corridor_label = "Waterway corridor" if is_water else "Railway corridor"
    corridor_y = ground_y - 18
    corridor_w = 140
    corridor_x0 = vertex_x - corridor_w // 2

    esc_title = _e(title)
    esc_sub = _e(subtitle)
    esc_street = _e(street or "Unnamed section")
    esc_muni = _e(municipality or "—")
    esc_auth = _e(authority or "—")
    esc_route = _e(route_section)
    esc_rule = _e(rule_id)

    svg = []
    svg.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="Segoe UI, Arial, sans-serif">'
    )
    # Background
    svg.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="#FFFFFF"/>')
    # Ground line
    svg.append(
        f'<line x1="{margin}" y1="{ground_y}" x2="{W - margin}" y2="{ground_y}" '
        f'stroke="#111827" stroke-width="2"/>'
    )
    svg.append(
        f'<text x="{margin}" y="{ground_y - 8}" font-size="9" fill="#6B7280">Ground level (GOK)</text>'
    )
    # Corridor band
    svg.append(
        f'<rect x="{corridor_x0}" y="{corridor_y}" width="{corridor_w}" height="18" '
        f'fill="{corridor_color}" opacity="0.18" stroke="{corridor_color}" stroke-width="1.2"/>'
    )
    # Rails / water line inside corridor
    if is_water:
        svg.append(
            f'<path d="M {corridor_x0 + 8} {corridor_y + 9} '
            f'Q {vertex_x - 20} {corridor_y + 2} {vertex_x} {corridor_y + 9} '
            f'Q {vertex_x + 20} {corridor_y + 16} {corridor_x0 + corridor_w - 8} {corridor_y + 9}" '
            f'stroke="{corridor_color}" stroke-width="1.8" fill="none"/>'
        )
    else:
        svg.append(
            f'<line x1="{corridor_x0 + 10}" y1="{corridor_y + 6}" x2="{corridor_x0 + corridor_w - 10}" y2="{corridor_y + 6}" '
            f'stroke="{corridor_color}" stroke-width="2"/>'
        )
        svg.append(
            f'<line x1="{corridor_x0 + 10}" y1="{corridor_y + 12}" x2="{corridor_x0 + corridor_w - 10}" y2="{corridor_y + 12}" '
            f'stroke="{corridor_color}" stroke-width="2"/>'
        )
        svg.append(
            f'<text x="{vertex_x}" y="{corridor_y - 4}" text-anchor="middle" font-size="8" fill="{corridor_color}">{_e(corridor_label)}</text>'
        )
    if is_water:
        svg.append(
            f'<text x="{vertex_x}" y="{corridor_y - 4}" text-anchor="middle" font-size="8" fill="{corridor_color}">{_e(corridor_label)}</text>'
        )
    # Entry / exit pits
    pit_w, pit_h = 18, 22
    svg.append(f'<rect x="{entry_x - pit_w}" y="{ground_y}" width="{pit_w}" height="{pit_h}" fill="#FEF3C7" stroke="#D97706" stroke-width="1.2"/>')
    svg.append(f'<rect x="{exit_x}" y="{ground_y}" width="{pit_w}" height="{pit_h}" fill="#FEF3C7" stroke="#D97706" stroke-width="1.2"/>')
    svg.append(f'<text x="{entry_x - pit_w/2}" y="{ground_y + pit_h + 10}" text-anchor="middle" font-size="8" fill="#92400E">Entry</text>')
    svg.append(f'<text x="{exit_x + pit_w/2}" y="{ground_y + pit_h + 10}" text-anchor="middle" font-size="8" fill="#92400E">Exit</text>')
    # HDD bore path (quadratic bezier via vertex)
    svg.append(
        f'<path d="M {entry_x} {ground_y} Q {vertex_x} {bore_vertex_y + 18} {exit_x} {ground_y}" '
        f'stroke="#DC2626" stroke-width="3" fill="none" stroke-linecap="round"/>'
    )
    # Depth dimension
    svg.append(
        f'<line x1="{vertex_x + 86}" y1="{ground_y}" x2="{vertex_x + 86}" y2="{bore_vertex_y}" '
        f'stroke="#6B7280" stroke-width="1" stroke-dasharray="4 3"/>'
    )
    svg.append(
        f'<text x="{vertex_x + 90}" y="{(ground_y + bore_vertex_y)//2 + 3}" font-size="9" fill="#111827">D {depth_m:.1f} m</text>'
    )
    # Length dimension
    svg.append(
        f'<line x1="{entry_x}" y1="{bore_vertex_y + 26}" x2="{exit_x}" y2="{bore_vertex_y + 26}" '
        f'stroke="#6B7280" stroke-width="1" marker-start="url(#arrow)" marker-end="url(#arrow)"/>'
    )
    svg.append(
        f'<text x="{vertex_x}" y="{bore_vertex_y + 38}" text-anchor="middle" font-size="9" fill="#111827">L ~{bore_length_m} m</text>'
    )
    # Radius note
    svg.append(
        f'<text x="{vertex_x}" y="{bore_vertex_y + 52}" text-anchor="middle" font-size="8" fill="#6B7280">R ≥ {radius_m} m (min. bending radius)</text>'
    )
    # Arrow markers
    svg.append(
        '<defs><marker id="arrow" viewBox="0 0 10 6" refX="10" refY="3" markerWidth="8" markerHeight="6" orient="auto">'
        '<path d="M 0 0 L 10 3 L 0 6 z" fill="#6B7280"/></marker></defs>'
    )
    # Title block
    svg.append(f'<text x="{margin}" y="{H - 86}" font-size="13" font-weight="600" fill="#111827">{esc_title}</text>')
    svg.append(f'<text x="{margin}" y="{H - 70}" font-size="10" fill="#374151">{esc_sub} — {esc_street} · {_e(municipality or "municipality pending")}</text>')
    svg.append(f'<text x="{margin}" y="{H - 56}" font-size="9" fill="#6B7280">Route section: {esc_route} · Authority: {esc_auth} · Rule: {esc_rule}</text>')
    svg.append(f'<text x="{margin}" y="{H - 42}" font-size="8" fill="#9CA3AF">Schematic profile — not to scale. HDD design to be confirmed by contractor (entry/exit angles 8–12°, depth and radius per DB/Wasserbehörde approval).</text>')
    svg.append(f'<text x="{margin}" y="{H - 28}" font-size="8" fill="#9CA3AF">Generated by FTTH Permit Engine — deterministic, no LLM.</text>')
    svg.append(f'<text x="{W - margin}" y="{H - 14}" text-anchor="end" font-size="7" fill="#D1D5DB">FTTH permit package · HDD crossing drawing</text>')
    svg.append("</svg>")
    return "\n".join(svg)


def hdd_crossing_drawings(project_id: str, project_name: str = "") -> list[dict[str, Any]]:
    """One HDD profile SVG per crossing group.

    Groups by (rule, permit_group) so a street with multiple intersecting
    segments yields one drawing, not one per segment. Returns [] when no
    crossing rows exist — this is the P16 guard.

    The grouping keeps the package small and matches the application-form
    granularity (one form per street). Each drawing lists the member sections
    in its subtitle so traceability is preserved.
    """
    rows = list(
        PermitMatrix.objects.filter(project_id=project_id)
        .select_related("authority", "rule")
        .order_by("permit_group", "route_section")
    )
    # Keep only crossing rules.
    crossing_rows = [pm for pm in rows if pm.rule and pm.rule.rule_id in CROSSING_RULE_IDS]
    if not crossing_rows:
        return []

    # Group by (rule_id, permit_group or route_section) — same clustering as
    # the application forms.
    groups: dict[tuple[str, str], list[PermitMatrix]] = {}
    for pm in crossing_rows:
        key = (pm.rule.rule_id, (pm.permit_group or pm.route_section or "").strip() or pm.route_section)
        groups.setdefault(key, []).append(pm)

    out: list[dict[str, Any]] = []
    for (rule_id, group), members in sorted(groups.items()):
        head = members[0]
        kind_label = _RULE_LABEL.get(rule_id, "HDD crossing profile")
        crossing_type = _RULE_SHORT.get(rule_id, "railway")
        # Street/area label
        street = (head.permit_group or group or "Unnamed section").strip()
        # Pick a bore length/depth that is plausible but clearly schematic.
        # Waterway crossings are typically longer/shallower than railway.
        if crossing_type == "waterway":
            bore_len, depth, radius = 42, 3.0, 100
        else:
            bore_len, depth, radius = 32, 2.5, 80
        # Traceability: list up to 3 member sections in subtitle
        sections_note = ", ".join(m.route_section for m in members[:3])
        if len(members) > 3:
            sections_note += f" +{len(members) - 3} more"
        subtitle = f"{kind_label} · {len(members)} section(s): {sections_note}"
        title = f"HDD Profile — {street}"

        slug_street = "".join(c if c.isalnum() else "_" for c in street.lower())[:40].strip("_") or "unnamed"
        slug_rule = crossing_type
        filename = f"drawings/hdd_{slug_rule}_{slug_street}.svg"
        # Ensure filename uniqueness per group (two streets can slug identically)
        # by appending a short hash of the group key when needed.
        if any(g["filename"] == filename for g in out):
            filename = f"drawings/hdd_{slug_rule}_{slug_street}_{abs(hash((rule_id, group))) % 1000:03d}.svg"

        svg = _svg_hdd_profile(
            title=title,
            subtitle=subtitle,
            street=street,
            municipality=head.municipality or "",
            authority=head.authority.name if head.authority else "",
            route_section=head.route_section,
            rule_id=rule_id,
            crossing_type=crossing_type,
            bore_length_m=bore_len,
            depth_m=depth,
            radius_m=radius,
        )
        out.append(
            {
                "name": f"hdd_{crossing_type}_{slug_street}",
                "kind": "DRAWING",
                "filename": filename,
                "content": svg,
                "description": f"{kind_label} — {street} ({len(members)} sections, {crossing_type} crossing)",
            }
        )
    return out
