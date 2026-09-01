"""LLD final permit summary generator.

One final, authority-ready HTML document built from the completed LLD design
layers (``final_trenches``) and survey references. This is the **main LLD
permit document**:

* every section (trench) is listed with its **trench type, construction
  method, length, width, depth, surface, sidewalk side, road class and reuse
  source** — mirroring the per-street form in ``docs/permit_road.docx``;
* it **drops features the survey purge marked ``lld_purged``** so a rerouted
  corridor never includes the old path traders that were removed;
* it embeds an **inline street map image** (OSM tile at the corridor centroid)
  plus a Google Street View anchor and any survey photo references;
* it closes with an overall trench-type summary (open-cut / HDD / micro /
  reuse / garden) and the permit-road form fields for the authority.

Street grouping uses a real ``street_name`` when the features carry one,
otherwise each distinct (trench-type × surface × sidewalk-side) design case is
its own block so the document stays useful even when the engine has not stamped
street names onto the trench layer.
"""

from __future__ import annotations

import html
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from . import data
from .hld_summary import _permit_road_fields, _street_name, _value


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _first_vertex(feature: dict[str, Any]) -> tuple[float, float] | None:
    """First [lng, lat] vertex of a line/point feature geometry."""
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") or []
    if geom.get("type") == "Point" and len(coords) >= 2:
        return (float(coords[0]), float(coords[1]))
    if geom.get("type") in ("LineString", "MultiLineString"):
        c0 = coords[0] if coords else []
        if c0 and len(c0) >= 2 and isinstance(c0[0], (int, float)):
            return (float(c0[0]), float(c0[1]))
        if c0 and isinstance(c0[0], list) and c0[0] and len(c0[0]) >= 2:
            return (float(c0[0][0]), float(c0[0][1]))
    return None


def _centroid(feature: dict[str, Any]) -> tuple[float, float] | None:
    """Mean of all vertices — better image/anchor point than the first vertex."""
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") or []
    pts: list[tuple[float, float]] = []
    def gather(node: Any) -> None:
        if isinstance(node, list) and node and isinstance(node[0], (int, float)) and len(node) >= 2:
            pts.append((float(node[0]), float(node[1])))
        elif isinstance(node, list):
            for n in node:
                gather(n)
    gather(coords)
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _street_view_link(lat: float, lng: float) -> str:
    """Google Street View pano link at a viewpoint (street imagery anchor)."""
    return f"https://www.google.com/maps?api=1&map_action=pano&viewpoint={lat:.6f},{lng:.6f}"


def _osm_tile_xy(lat: float, lng: float, zoom: int) -> tuple[int, int]:
    """OSM slippy-map tile (x, y) for a lng/lat at a given zoom."""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = int((lng + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    x = max(0, min(n - 1, x))
    y = max(0, min(n - 1, y))
    return (x, y)


def _method_label(props: dict[str, Any], trench_type: str) -> str:
    return str(_value(props, "CONSTRUCT", "construction_method", "METHOD", default=trench_type))


def _reuse_label(props: dict[str, Any]) -> str:
    src = _value(props, "REUSE_SOURCE", "reuse_source", default="")
    return str(src) if src else "—"


# Traffic tier thresholds mirror traffic_plan._TIERS (length-based).
_TIERS = [
    (2000, "Major", "Full lane closure with diversion", [
        "Lane closure with advance warning signs (500 m, 200 m, 100 m)",
        "Signed diversion route with temporary road markings",
        "Pedestrian diversion with barriers and tactile guidance",
        "Traffic light control (or banksman) at the work zone",
        "Work zone protected by Type-2 barriers and delineators",
        "Emergency access maintained at all times",
    ]),
    (500, "Moderate", "Partial lane closure (lane shift)", [
        "Partial lane closure with taper and temporary markings",
        "Pedestrian crossing maintained with temporary crossing point",
        "Work zone barriers with reflective delineators",
        "Advance warning signs (200 m, 100 m)",
    ]),
    (0, "Minor", "No lane closure — verge/footway works", [
        "Coned-off work zone with pedestrian diversion on footway",
        "Advance warning signs (100 m)",
        "Works under permit hours only",
    ]),
]


# Normalise the engine's USAGE_TYPE / trench_type into the four authority
# categories (open-cut / HDD / micro-trench / reuse) used by the permit forms.
def _construction_category(trench_type: str, method: str, surface: str, reuse: str) -> str:
    ttype = (trench_type or "").lower()
    meth = (method or "").lower()
    if reuse not in ("", "—", "none", "null", "None"):
        return "Existing Duct Reuse"
    hints = f"{ttype} {meth}".lower()
    if "hdd" in hints or "directional" in hints or "micro" in hints and "trench" in hints:
        if "micro" in hints:
            return "Micro Trench"
        return "HDD"
    if "micro" in hints:
        return "Micro Trench"
    if "aerial" in hints:
        return "Aerial"
    if "garden" in hints:
        return "Garden"
    return "Open Cut"


def lld_street_summary(project_id: str, project_name: str) -> dict[str, Any]:
    """One final, case-wise HTML summary from the LLD final_trenches layer."""
    trenches = data.lld_layer_features(project_id, "final_trenches")

    # ── Drop features the survey purge marked as removed ──────────────────
    # `lld_purged` trenches are the old HLD path pieces an engineer rerouted
    # away from; keeping them would show the stale corridor alongside the new
    # approved route. They must never appear in an authority-ready permit.
    live_trenches = [
        f for f in trenches
        if not (data._props(f).get("lld_purged") in (True, "true", "True", "1", 1))
    ]

    if not live_trenches:
        return {
            "name": "LLD street-wise permit summary",
            "kind": "REPORT",
            "filename": "lld/lld_street_permit_summary.html",
            "content": "<html><body><p>No final trench features available for this LLD run.</p></body></html>",
            "description": "Street-wise LLD permit summary (empty)",
        }

    # Group sections into blocks. When a street name exists it is preferred;
    # otherwise each distinct (type × surface × sidewalk) design case gets its
    # own block so the document is genuinely section-wise, never one bucket.
    streets: dict[str, dict[str, Any]] = {}
    totals: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "length": 0.0})

    for feature in live_trenches:
        props = data._props(feature)
        raw_street = _street_name(props)
        trench_type = str(_value(props, "trench_type", "TRENCH_TYPE", default="Unknown"))
        method = _method_label(props, trench_type)
        length = data._as_float(_value(props, "length_m", "LENGTH_M", "distance_m", default=0)) or 0.0
        depth = str(_value(props, "DEPTH_MM", "depth_mm", "depth", default="—")) or "—"
        width = str(_value(props, "WIDTH_MM", "width_mm", "width", default="—")) or "—"
        surface = str(_value(props, "SURFACE", "surface", default="—")) or "—"
        sidewalk = str(_value(props, "sidewalk", "SIDEWALK", "side", "Side", default="—")) or "—"
        road_class = str(_value(props, "fclass", "road_class", "ROAD_CLASS", default="Not classified"))
        reinst = str(_value(props, "REINSTATE", "reinstatement", default="—")) or "—"
        section_id = str(_value(props, "feature_id", "FEATURE_ID", "fid", "id", default="—")) or "—"
        image = str(_value(props, "image_url", "IMAGE_URL", "photo_url", "PHOTO_URL", default="")) or ""
        reuse = _reuse_label(props)
        centroid = _centroid(feature)

        if raw_street not in ("", "Unnamed street", "—"):
            block_name = raw_street
        else:
            # Synthetic but meaningful case label.
            block_name = f"{trench_type} · {surface} · {sidewalk}".strip(" ·")

        if block_name not in streets:
            streets[block_name] = {
                "sections": [], "types": defaultdict(lambda: {"count": 0, "length": 0.0}),
                "traffic": set(), "images": [], "centroids": [], "total": 0.0,
            }
        row = streets[block_name]
        row["sections"].append({
            "id": section_id, "type": trench_type, "method": method, "length": length,
            "width": width, "depth": depth, "surface": surface, "road_class": road_class,
            "sidewalk": sidewalk, "reinst": reinst, "reuse": reuse,
        })
        row["types"][trench_type]["count"] += 1
        row["types"][trench_type]["length"] += length
        row["traffic"].add(road_class)
        row["total"] += length
        if image:
            row["images"].append(image)
        if centroid:
            row["centroids"].append(centroid)
        totals[trench_type]["count"] += 1
        totals[trench_type]["length"] += length

    def _tier(length_m: float) -> tuple[str, str, list[str]]:
        for t in _TIERS:
            if length_m >= t[0]:
                return (t[1], t[2], t[3])
        return (_TIERS[-1][1], _TIERS[-1][2], _TIERS[-1][3])

    def _inline_tile_image(centroid: tuple[float, float], span_km: float) -> str:
        """A self-contained inline street map image (OSM tile) for a block."""
        lng, lat = centroid
        zoom = 17
        if span_km > 1.5:
            zoom = 15
        elif span_km > 0.6:
            zoom = 16
        x, y = _osm_tile_xy(lat, lng, zoom)
        src = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
        return (
            f'<img src="{src}" alt="Street map" '
            f'style="max-width:100%;width:420px;border:1px solid #d1d5db;border-radius:6px;'
            f'aspect-ratio:1;object-fit:cover;background:#eef2f7;">'
            f'<div class="small" style="margin-top:4px;">Street map around '
            f'{lat:.5f}, {lng:.5f} · <a href="{_street_view_link(lat, lng)}" target="_blank">'
            f'Open in Google Street View</a></div>'
        )

    def _span_km(centroids: list[tuple[float, float]]) -> float:
        if len(centroids) < 2:
            return 0.0
        lats = [c[0] for c in centroids]
        lngs = [c[1] for c in centroids]
        dlat = (max(lats) - min(lats)) * 110.54
        dlon = (max(lngs) - min(lngs)) * 111.32 * math.cos(math.radians(sum(lats) / len(lats)))
        return math.hypot(dlat, dlon)

    street_blocks = []
    for block_name, info in sorted(streets.items()):
        type_summary = "; ".join(
            f"{kind}: {bucket['length']:.1f} m ({bucket['count']} sections)"
            for kind, bucket in sorted(info["types"].items())
        ) or "No trench sections"
        tier_label, tier_impact, measures = _tier(info["total"])
        measures_html = "".join(f"<li>{_e(m)}</li>" for m in measures)

        # Inline street map image + Street View anchor at the block centroid.
        image_html = ""
        if info["centroids"]:
            lat = sum(p[0] for p in info["centroids"]) / len(info["centroids"])
            lng = sum(p[1] for p in info["centroids"]) / len(info["centroids"])
            image_html = _inline_tile_image((lat, lng), _span_km(info["centroids"]))
        else:
            image_html = "<p><em>No coordinates for street imagery — attach survey photos.</em></p>"
        if info["images"]:
            refs = "".join(
                f'<a href="{_e(i)}" target="_blank">{_e(i)}</a>; ' for i in info["images"][:6]
            )
            image_html += f'<p class="small"><strong>Survey photo references:</strong> {refs}</p>'

        rows = "".join(
            "<tr>"
            f"<td>{_e(s['id'])}</td>"
            f"<td>{_e(s['type'])}</td>"
            f"<td>{_e(s['method'])}</td>"
            f"<td>{s['length']:.1f} m</td>"
            f"<td>{_e(s['width'])} mm</td>"
            f"<td>{_e(s['depth'])} mm</td>"
            f"<td>{_e(s['surface'])}</td>"
            f"<td>{_e(s['road_class'])}</td>"
            f"<td>{_e(s['sidewalk'])}</td>"
            f"<td>{_e(s['reuse'])}</td>"
            "</tr>"
            for s in info["sections"]
        )
        street_blocks.append(f"""<section class="street">
<h2>{_e(block_name)}</h2>
<p><strong>Trench summary:</strong> {_e(type_summary)}<br>
<strong>Total length:</strong> {info['total']:,.1f} m<br>
<strong>Traffic / road classes:</strong> {_e(', '.join(sorted(info['traffic'])))}<br>
<strong>Traffic management:</strong> {_e(tier_label)} — {_e(tier_impact)}</p>
{image_html}
<h3>Traffic management measures (street-wise)</h3>
<ul style="font-size:12px;line-height:1.6;">{measures_html}</ul>
<h3>Sections</h3>
<table><thead><tr>
<th>Section</th><th>Trench type</th><th>Method</th><th>Length</th><th>Width</th>
<th>Depth</th><th>Surface</th><th>Road class</th><th>Sidewalk</th><th>Reuse src</th>
</tr></thead><tbody>{rows}</tbody></table>
</section>""")

    # Overall trench-type summary — split into the four construction categories
    # the permit forms ask for (open-cut / HDD / micro / reuse) in addition to
    # the raw engine types.
    cat_totals: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "length": 0.0})
    for feature in live_trenches:
        props = data._props(feature)
        trench_type = str(_value(props, "trench_type", "TRENCH_TYPE", default="Unknown"))
        method = _method_label(props, trench_type)
        surface = str(_value(props, "SURFACE", default=""))
        reuse = _reuse_label(props)
        length = data._as_float(_value(props, "length_m", "LENGTH_M", "distance_m", default=0)) or 0.0
        cat = _construction_category(trench_type, method, surface, reuse)
        cat_totals[cat]["count"] += 1
        cat_totals[cat]["length"] += length

    raw_rows = "".join(
        f"<tr><td>{_e(kind)}</td><td>{bucket['count']}</td><td>{bucket['length']:.1f} m</td></tr>"
        for kind, bucket in sorted(totals.items())
    ) or '<tr><td colspan="3">No trench data available.</td></tr>'
    cat_rows = "".join(
        f"<tr><td>{_e(cat)}</td><td>{bucket['count']}</td><td>{bucket['length']:.1f} m</td></tr>"
        for cat, bucket in sorted(cat_totals.items())
    )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    content = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>LLD Permit Summary — {_e(project_name)}</title>
<style>body{{font-family:Arial,sans-serif;color:#111827;margin:28px}}h1{{font-size:22px}}h2{{font-size:17px;margin-bottom:6px}}.notice{{padding:12px;background:#ecfdf5;border:2px solid #10b981;border-radius:7px}}.street{{page-break-inside:avoid;margin-top:28px}}table{{border-collapse:collapse;width:100%;font-size:11px}}th,td{{border:1px solid #d1d5db;padding:5px;text-align:left}}th{{background:#f3f4f6}}.small{{font-size:11px;color:#6b7280}}</style></head><body>
<h1>Final LLD Permit Summary</h1><p class="small">Project: {_e(project_name)} · ID: {_e(project_id)} · Generated: {generated} · Purged/routed-away sections excluded</p>
<div class="notice"><strong>FINAL DESIGN — SURVEY-CONFIRMED. The engineer-approved (rerouted) route is authoritative.</strong><br>
Features marked <code>lld_purged</code> by the field survey (old rerouted-away corridor) are excluded from every row below. Section lengths, trench types, dimensions, street imagery and traffic management are computed from the completed LLD design layers and approved survey input.</div>
<h2>Overall trench-type summary</h2>
<table><thead><tr><th>Construction method</th><th>Sections</th><th>Total length</th></tr></thead><tbody>{cat_rows}</tbody></table>
<h3>By design type</h3>
<table><thead><tr><th>Design type</th><th>Sections</th><th>Total length</th></tr></thead><tbody>{raw_rows}</tbody></table>
<h2>Street-wise design and permit information</h2>{''.join(street_blocks) or '<p>No trench features were available in the LLD output.</p>'}
<h2>Permit-road form fields</h2><p class="small">The following fields from <strong>docs/permit_road.docx</strong> are represented by the LLD data where available; remaining values are completed during application.</p>
<table><thead><tr><th>Field</th><th>Status / value</th></tr></thead><tbody>{''.join(f'<tr><td>{_e(field)}</td><td>Completed in LLD design data / application form</td></tr>' for field in _permit_road_fields())}</tbody></table>
<p class="small">Reroute handling: where the survey approved a diverted path, the old corridor is excluded (see <code>lld_purged</code>) and only the approved route is carried into the permit.</p>
</body></html>"""
    return {
        "name": "LLD street-wise permit summary",
        "kind": "REPORT",
        "filename": "lld/lld_street_permit_summary.html",
        "content": content,
        "description": "Final LLD permit summary — per-section trench type, length, dimensions, street imagery and traffic management",
    }