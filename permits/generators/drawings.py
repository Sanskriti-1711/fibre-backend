"""Route drawings + trench cross-sections for the permit package.

Route drawings are GeoJSON extracts of the final design layers (usable in
QGIS / MapLibre / print) with a human-readable index. Cross-sections are
self-contained SVGs per trench type / surface, dimensioned from the
persisted layer attributes (DEPTH_MM / WIDTH_MM / trench_type / SURFACE).
"""

from __future__ import annotations

import json
from typing import Any

from . import data

# Default construction spec per trench type (mm) — used only when the layer
# carries no DEPTH_MM / WIDTH_MM. Mirrors the HLD/LLD engine's construction
# specs (see docs/stages/LLD.md).
TRENCH_SPECS = {
    'Feeder': {'depth_mm': 900, 'width_mm': 450},
    'Distribution': {'depth_mm': 600, 'width_mm': 300},
    'Garden': {'depth_mm': 450, 'width_mm': 150},
    'Duct': {'depth_mm': 600, 'width_mm': 300},
    'Unknown': {'depth_mm': 600, 'width_mm': 300},
}

_SVG_FILL = {
    'Asphalt': '#374151',
    'Footpath': '#D1D5DB',
    'Concrete': '#9CA3AF',
    'Grass': '#86B87C',
    'Paving': '#D1D5DB',
}


def route_drawings(project_id: str, lld_run_id: str | None = None) -> list[dict[str, Any]]:
    """GeoJSON FeatureCollection extracts of the final design layers."""
    drawings = []
    for layer in data.PACKAGE_LAYERS:
        feats = data.lld_layer_features(project_id, layer, lld_run_id)
        if not feats:
            continue
        fc = {'type': 'FeatureCollection', 'features': feats}
        drawings.append(
            {
                'name': f'drawing_{layer}',
                'kind': 'DRAWING',
                'filename': f'drawings/{layer}.geojson',
                'content': json.dumps(fc, indent=1),
                'description': f'Route drawing — {layer}',
            }
        )
    return drawings


def _svg_cross_section(trench_type: str, surface: str, depth_mm: int, width_mm: int) -> str:
    """Dimensioned cross-section SVG for one trench type/surface combo."""
    # Scale: 1 mm -> 0.5 px, capped so the SVG stays readable.
    w = max(120, min(int(width_mm * 0.5), 480))
    h = max(120, min(int(depth_mm * 0.5), 420))
    fill = _SVG_FILL.get(surface, '#D1D5DB')
    margin = 46
    x0, y0 = margin, margin  # top-left of the trench opening

    def esc(v: str) -> str:
        return v.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    svg = []
    svg.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{x0 + w + 70}" '
        f'height="{y0 + h + 60}" viewBox="0 0 {x0 + w + 70} {y0 + h + 60}" '
        f'font-family="Segoe UI, Arial, sans-serif">'
    )
    # Ground line above the trench
    svg.append(
        f'<line x1="{x0 - 20}" y1="{y0}" x2="{x0 + w + 20}" y2="{y0}" '
        f'stroke="#111827" stroke-width="2"/>'
    )
    # Surface layer (top band)
    band = max(14, min(int(h * 0.18), 60))
    svg.append(
        f'<rect x="{x0}" y="{y0}" width="{w}" height="{band}" fill="{fill}" '
        f'stroke="#111827" stroke-width="1.5"/>'
    )
    # Trench body
    body_y = y0 + band
    body_h = h - band
    svg.append(
        f'<rect x="{x0}" y="{body_y}" width="{w}" height="{body_h}" '
        f'fill="#F3F4F6" stroke="#111827" stroke-width="1.5" stroke-dasharray="6 3"/>'
    )
    # Duct / conduit inside the body
    duct_d = 20
    duct_x = x0 + w / 2 - duct_d / 2
    duct_y = body_y + body_h - duct_d - 8
    svg.append(
        f'<rect x="{duct_x}" y="{duct_y}" width="{duct_d}" height="{duct_d}" '
        f'rx="4" fill="#EF4444" stroke="#7F1D1D" stroke-width="1.5"/>'
    )
    svg.append(
        f'<text x="{duct_x + duct_d / 2}" y="{duct_y + duct_d / 2 + 4}" '
        f'text-anchor="middle" font-size="9" fill="#FFFFFF">D</text>'
    )
    # Dimension labels
    svg.append(
        f'<text x="{x0 + w / 2}" y="{y0 - 10}" text-anchor="middle" '
        f'font-size="11" fill="#111827">W {width_mm} mm</text>'
    )
    svg.append(
        f'<text x="{x0 - 8}" y="{y0 + h / 2}" text-anchor="end" '
        f'font-size="11" fill="#111827" transform="rotate(-90 {x0 - 8} {y0 + h / 2})">'
        f'D {depth_mm} mm</text>'
    )
    # Header
    svg.append(
        f'<text x="{x0}" y="{y0 + h + 28}" font-size="13" font-weight="600" '
        f'fill="#111827">{esc(trench_type)} trench — {esc(surface)}</text>'
    )
    svg.append(
        f'<text x="{x0}" y="{y0 + h + 44}" font-size="10" fill="#6B7280">'
        f'Typical cross-section (not to scale)</text>'
    )
    svg.append('</svg>')
    return '\n'.join(svg)


def cross_sections(project_id: str) -> list[dict[str, Any]]:
    """One SVG cross-section per trench type/surface combination actually
    present in the project's final_trenches."""
    stats = data.trench_stats(project_id)
    combos: set[tuple[str, str]] = set()
    for f in data.lld_layer_features(project_id, 'final_trenches'):
        p = data._props(f)
        ttype = (p.get('trench_type') or 'Unknown').strip() or 'Unknown'
        surf = (p.get('SURFACE') or 'Unknown').strip() or 'Unknown'
        combos.add((ttype, surf))
    out = []
    for ttype, surf in sorted(combos):
        depth = int(
            stats.get('max_depth_mm')
            or TRENCH_SPECS.get(ttype, TRENCH_SPECS['Unknown'])['depth_mm']
        )
        width = int(
            stats.get('max_width_mm')
            or TRENCH_SPECS.get(ttype, TRENCH_SPECS['Unknown'])['width_mm']
        )
        slug = f"{ttype.lower().replace(' ', '_')}_{surf.lower().replace(' ', '_')}"
        out.append(
            {
                'name': f'cross_section_{slug}',
                'kind': 'CROSS_SECTION',
                'filename': f'drawings/cross_section_{slug}.svg',
                'content': _svg_cross_section(ttype, surf, depth, width),
                'description': f'Trench cross-section — {ttype} / {surf}',
            }
        )
    return out
