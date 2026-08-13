"""
BOQ / BOM quantity engine.

Computes bill-of-quantities (BOQ) and bill-of-materials (BOM) rows for a
completed HLD run from the **persisted output layers** (``FtthLayer``
GeoJSON in the database), then prices them with the rate card
(``BoqRate``) and stores the result as an immutable ``BoqSnapshot``.

Replaces the old hardcoded template-copy behaviour (the engine used to
drop static ``Drafts/BOQ.xlsx`` / ``Drafts/BOM.xlsx`` files into the
output folder with hand-typed quantities). Everything here is computed
from the actual design data, so quantities stay correct when the design
changes.

Section numbering mirrors the original BOQ.xlsx template:

    2  Civil works — trenching
    3  Civil works — ducts
    4  Lines & cables
    6  Plant elements
    7  OTB distribution
    9  FTTF (fiber to the fence)
"""

from __future__ import annotations

import math
from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional

from django.db import transaction
from django.utils import timezone

try:
    from pyproj import Geod

    _GEOD = Geod(ellps="WGS84")
except Exception:  # pragma: no cover
    _GEOD = None

from .models import BoqRate, BoqSnapshot, FtthLayer, FtthProject

# ---------------------------------------------------------------------------
# Quantity rules: layer name → (attribute key, value → BOQ item code)
# ---------------------------------------------------------------------------
# Each rule yields a quantity for a BOQ item code:
#   layer  : persisted layer name (FtthLayer.name)
#   key    : attribute to inspect (missing/None → feature is counted once)
#   match  : dict value → item code, or special "*" catch-all
#   unit   : "m" (length from geometry/attrs) or "ea" (feature count)
#   attr_len: attribute carrying an explicit length in metres (optional)
# ---------------------------------------------------------------------------

# Trenching (section 2) — from the combined trenches layer, keyed by
# the ``sublayer`` attribute the engine stamps on each feature.
_TRENCH_RULES = [
    ("Feeder_Trench", "2.1"),
    ("Distribution_Trench", "2.3"),
    ("Garden_Trench", "2.5"),
    ("Drill_Trench", "2.6"),
]

# Ducts (section 3) — from the ducts layer, keyed by DUCT_TYPE.
_DUCT_RULES = [
    ("4-Way HDPE", "3.1"),   # Feeder duct HDPE 50/40
    ("2-Way HDPE", "3.7"),   # Distribution duct HDPE 32
    ("1-Way HDPE", "3.11"),  # Distribution sub-duct 1×7/4 (property)
]

# Cables (section 4) — from the cables layer, keyed by CABLE_TYPE +
# FIBER_COUNT. The template has per-FO-count items; map common counts,
# fall back to a generic feeder/distribution item when unknown.
_CABLE_RULES = [
    (("Feeder", "288"), "4.1"),
    (("Feeder", "192"), "4.2"),
    (("Feeder", "144"), "4.3"),
    (("Feeder", "96"), "4.4"),
    (("Feeder", "48"), "4.5"),
    (("Distribution", "24"), "4.6"),
    (("Distribution", "12"), "4.7"),
    (("Distribution", "4"), "4.8"),
]

# Plant elements (section 6) — point counts.
_PLANT_RULES = [
    # (layer, attr_key, attr_value, item_code)
    ("pdps", "EQUIP_TYPE", "PDP", "6.1"),          # DP48 (PDP with splicing)
    ("mfg", "EQUIP_TYPE", "MFG", "6.3"),           # MFG / Mini-PoP
    ("chambers", "CHAMBER_TYPE", "Handhole", "6.4"),  # Handhole open cut (B125)
    ("chambers", "CHAMBER_TYPE", "Manhole", "6.5"),   # Handhole trench cut (B125)
    ("chambers", "CHAMBER_TYPE", "Chamber", "6.6"),   # Closure (splice)
]

# OTB distribution (section 7) — premises per polygon decides the tier.
_OTB_TIERS = [
    ("7.1", 1, 4),   # OTB 1–4 HH
    ("7.2", 5, 8),   # OTB 5–8 HH
    ("7.3", 9, 24),  # OTB 9–24 HH
    ("7.4", 25, 48), # OTB 25–48 HH
]


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _ring_length(coords: List) -> float:
    """Length in metres of a coordinate ring (lon/lat degrees)."""
    if len(coords) < 2:
        return 0.0
    if _GEOD is not None:
        try:
            _, _, dist = _GEOD.geometry_length(
                {"type": "LineString", "coordinates": coords}
            )
            return float(dist)
        except Exception:
            pass
    # Fallback: equirectangular approximation at mean latitude.
    lat0 = math.radians(sum(c[1] for c in coords) / len(coords))
    total = 0.0
    for a, b in zip(coords, coords[1:]):
        dy = (b[1] - a[1]) * 111132.0
        dx = (b[0] - a[0]) * 111320.0 * math.cos(lat0)
        total += math.hypot(dx, dy)
    return total


def _geometry_length(geom: Optional[dict]) -> float:
    """Length in metres of a GeoJSON geometry (line-ish)."""
    if not geom or not geom.get("coordinates"):
        return 0.0
    gtype = geom.get("type", "")
    coords = geom.get("coordinates")
    if gtype == "LineString":
        return _ring_length(coords)
    if gtype == "MultiLineString":
        return sum(_ring_length(c) for c in coords)
    return 0.0


# ---------------------------------------------------------------------------
# Layer access helpers
# ---------------------------------------------------------------------------

def _get_layer(project_id: str, name: str) -> Optional[FtthLayer]:
    return FtthLayer.objects.filter(ftth_project__project_id=project_id, name=name).first()


def _iter_features(layer: Optional[FtthLayer]):
    if layer is None:
        return
    fc = layer.geojson or {}
    for f in fc.get("features", []) if isinstance(fc, dict) else []:
        yield f


# ---------------------------------------------------------------------------
# Quantity computation
# ---------------------------------------------------------------------------

def _sum_by_attr(project_id: str, layer_name: str, attr: str,
                 match: Dict[str, str], length_attr: Optional[str] = None,
                 count_all: bool = False) -> Dict[str, float]:
    """Sum length (or count) of features grouped by matched item code.

    ``match`` maps attribute value → item code. Features whose attribute
    value is not in ``match`` are skipped (unless ``count_all``, in which
    case unmatched features roll into ``"*"``).
    """
    totals: Dict[str, float] = {}
    layer = _get_layer(project_id, layer_name)
    for f in _iter_features(layer):
        props = f.get("properties", {}) or {}
        val = props.get(attr)
        key = match.get(str(val)) if not count_all else match.get(str(val), "*")
        if key is None:
            continue
        if length_attr:
            raw = props.get(length_attr)
            try:
                qty = float(raw) if raw is not None else _geometry_length(f.get("geometry"))
            except (TypeError, ValueError):
                qty = _geometry_length(f.get("geometry"))
        else:
            qty = 1.0
        totals[key] = totals.get(key, 0.0) + qty
    return totals


def compute_quantities(project_id: str) -> Dict[str, float]:
    """Compute raw quantities (metres / counts) for every BOQ item code."""
    qty: Dict[str, float] = {}

    # 2. Trenching — sublayer breakdown
    trench_totals = _sum_by_attr(project_id, "trenches", "sublayer",
                                 dict(_TRENCH_RULES), length_attr="length_m")
    for code, total in trench_totals.items():
        qty[code] = qty.get(code, 0.0) + total

    # 3. Ducts — by DUCT_TYPE
    duct_totals = _sum_by_attr(project_id, "ducts", "DUCT_TYPE",
                               dict(_DUCT_RULES), length_attr="length_m")
    for code, total in duct_totals.items():
        qty[code] = qty.get(code, 0.0) + total

    # Duct surplus (+2%) → 3.12
    duct_sum = sum(duct_totals.values())
    if duct_sum:
        qty["3.12"] = qty.get("3.12", 0.0) + round(duct_sum * 0.02, 2)

    # 4. Cables — by CABLE_TYPE + FIBER_COUNT
    cable_totals: Dict[str, float] = {}
    cable_layer = _get_layer(project_id, "cables")
    for f in _iter_features(cable_layer):
        props = f.get("properties", {}) or {}
        ctype = str(props.get("CABLE_TYPE") or props.get("cable_type") or "")
        fibers = str(props.get("FIBER_COUNT") or props.get("fiber_count") or "")
        key = (ctype, fibers)
        code = dict(_CABLE_RULES).get(key)
        if code is None:
            code = "4.1" if ctype.lower().startswith("feeder") else "4.6"
        raw = props.get("LENGTH_M") or props.get("length_m")
        try:
            length = float(raw) if raw is not None else _geometry_length(f.get("geometry"))
        except (TypeError, ValueError):
            length = _geometry_length(f.get("geometry"))
        cable_totals[code] = cable_totals.get(code, 0.0) + length
    for code, total in cable_totals.items():
        qty[code] = qty.get(code, 0.0) + total

    # Cable surplus (+2%) → 4.9
    cable_sum = sum(cable_totals.values())
    if cable_sum:
        qty["4.9"] = qty.get("4.9", 0.0) + round(cable_sum * 0.02, 2)

    # 6. Plant elements — point counts
    for layer, attr, val, code in _PLANT_RULES:
        counts = _sum_by_attr(project_id, layer, attr, {val: code})
        qty[code] = qty.get(code, 0.0) + counts.get(code, 0.0)

    # 7. OTB distribution — group premises by polygon
    otb = _count_otb_tiers(project_id)
    for code, tier_qty in otb.items():
        qty[code] = qty.get(code, 0.0) + tier_qty

    # 9. FTTF — access per property = premises count
    objects_layer = _get_layer(project_id, "objects")
    object_count = sum(1 for _ in _iter_features(objects_layer))
    if object_count:
        qty["9.1"] = qty.get("9.1", 0.0) + object_count

    return {k: round(v, 2) for k, v in qty.items() if v}


def _count_otb_tiers(project_id: str) -> Dict[str, int]:
    """Count how many polygons fall into each OTB tier by premises inside."""
    from collections import Counter

    objects_layer = _get_layer(project_id, "objects")
    polygons_layer = _get_layer(project_id, "polygons")
    if objects_layer is None or polygons_layer is None:
        return {}

    try:
        from shapely.geometry import shape, Point
    except Exception:
        return {}

    polys = []
    for f in _iter_features(polygons_layer):
        geom = f.get("geometry")
        if geom:
            try:
                polys.append((f.get("properties", {}), shape(geom)))
            except Exception:
                pass

    # Match each premise point to its containing polygon.
    counts = Counter()
    for f in _iter_features(objects_layer):
        geom = f.get("geometry")
        if not geom or geom.get("type") != "Point":
            continue
        try:
            pt = Point(geom["coordinates"])
        except Exception:
            continue
        for _, poly in polys:
            if poly.contains(pt):
                counts[id(poly)] += 1
                break

    tiers: Dict[str, int] = {}
    for hh in counts.values():
        for code, lo, hi in _OTB_TIERS:
            if lo <= hh <= hi:
                tiers[code] = tiers.get(code, 0) + 1
                break
    return tiers


# ---------------------------------------------------------------------------
# BOQ / BOM row assembly
# ---------------------------------------------------------------------------

def _rate_map() -> Dict[str, BoqRate]:
    return {r.item_code: r for r in BoqRate.objects.filter(active=True)}


def build_boq_rows(quantities: Dict[str, float]) -> List[Dict]:
    """Build BOQ rows priced with the rate card."""
    rates = _rate_map()
    rows: List[Dict] = []
    for code in sorted(quantities, key=lambda c: (c.split(".")[0], float(c.split(".")[1]))):
        rate = rates.get(code)
        qty = quantities[code]
        if rate is None:
            rows.append({
                "section": _section_for(code),
                "item_code": code,
                "item_name": _fallback_name(code),
                "unit": "m" if _is_length(code) else "ea",
                "quantity": qty,
                "material_rate": 0.0, "labour_rate": 0.0, "rent_rate": 0.0,
                "material_total": 0.0, "labour_total": 0.0, "amount": 0.0,
                "notes": "No rate card entry",
            })
            continue
        material_total = round(qty * rate.material_rate, 2)
        labour_total = round(qty * rate.labour_rate, 2)
        rent_total = round(qty * rate.rent_rate, 2)
        rows.append({
            "section": rate.section,
            "item_code": code,
            "item_name": rate.item_name,
            "unit": rate.unit,
            "quantity": qty,
            "material_rate": rate.material_rate,
            "labour_rate": rate.labour_rate,
            "rent_rate": rate.rent_rate,
            "material_total": material_total,
            "labour_total": labour_total,
            "amount": round(material_total + labour_total + rent_total, 2),
            "notes": "",
        })
    return rows


def build_bom_rows(quantities: Dict[str, float]) -> List[Dict]:
    """Build BOM rows — a materials-oriented view of the same quantities.

    Mirrors the BOM sheet structure: Ducts / Cables / Plant grouped rows
    with the material cost (material_rate × quantity).
    """
    rates = _rate_map()
    rows: List[Dict] = []
    for code in sorted(quantities, key=lambda c: (c.split(".")[0], float(c.split(".")[1]))):
        rate = rates.get(code)
        qty = quantities[code]
        material_rate = rate.material_rate if rate else 0.0
        material_total = round(qty * material_rate, 2)
        rows.append({
            "section": _bom_section(code),
            "item_code": code,
            "item_name": rate.item_name if rate else _fallback_name(code),
            "unit": rate.unit if rate else ("m" if _is_length(code) else "ea"),
            "quantity": qty,
            "material_rate": material_rate,
            "material_total": material_total,
            "notes": "",
        })
    return rows


def _section_for(code: str) -> str:
    section_map = {
        "2": "Civil works — trenching",
        "3": "Civil works — ducts",
        "4": "Lines & cables",
        "6": "Plant elements",
        "7": "OTB distribution",
        "9": "FTTF (fiber to the fence)",
    }
    return section_map.get(code.split(".")[0], "Other")


def _bom_section(code: str) -> str:
    section_map = {
        "3": "Ducts",
        "4": "Cables",
        "6": "Plant elements",
        "7": "OTB distribution",
    }
    return section_map.get(code.split(".")[0], "Other")


def _is_length(code: str) -> bool:
    return code.split(".")[0] in ("2", "3", "4")


def _fallback_name(code: str) -> str:
    names = {
        "2.1": "Feeder trench — open cut", "2.3": "Distribution trench — open cut",
        "2.5": "Garden trench — ploughing", "2.6": "Drill / HDD crossings",
        "3.1": "Feeder duct HDPE 50/40", "3.7": "Distribution duct HDPE 32",
        "3.11": "Distribution sub-duct 1×7/4 (property)", "3.12": "Duct surplus (+2%)",
        "4.1": "Feeder cable 288 FO", "4.6": "Distribution cable 24 FO",
        "4.9": "Cable surplus (+2%)",
        "6.1": "DP48 (PDP with splicing)", "6.3": "MFG / Mini-PoP",
        "6.4": "Handhole open cut (B125)", "6.5": "Handhole trench cut (B125)",
        "6.6": "Closure (splice)", "7.1": "OTB 1–4 HH", "7.2": "OTB 5–8 HH",
        "7.3": "OTB 9–24 HH", "7.4": "OTB 25–48 HH", "9.1": "Access per property",
    }
    return names.get(code, code)


# ---------------------------------------------------------------------------
# Snapshot lifecycle
# ---------------------------------------------------------------------------

def totals_for(rows: List[Dict]) -> Dict:
    per_section: Dict[str, float] = {}
    grand = 0.0
    for r in rows:
        try:
            amt = float(r.get("amount") or 0.0)
        except (TypeError, ValueError):
            amt = 0.0
        sect = str(r.get("section", "")).strip() or "Other"
        per_section[sect] = per_section.get(sect, 0.0) + amt
        grand += amt
    return {"per_section": per_section, "grand_total": round(grand, 2)}


def generate_snapshot(project_id: str, force: bool = False) -> BoqSnapshot:
    """Compute (or refresh) the BOQ/BOM snapshot for a completed HLD run."""
    try:
        project = FtthProject.objects.get(pk=project_id)
    except FtthProject.DoesNotExist:
        raise ValueError(f"Project '{project_id}' not found.")

    snapshot = BoqSnapshot.objects.filter(ftth_project=project).first()
    if snapshot is not None and not force:
        return snapshot

    quantities = compute_quantities(project_id)
    boq_rows = build_boq_rows(quantities)
    bom_rows = build_bom_rows(quantities)

    with transaction.atomic():
        if snapshot is None:
            snapshot = BoqSnapshot(ftth_project=project)
        snapshot.boq_json = boq_rows
        snapshot.bom_json = bom_rows
        snapshot.boq_totals = totals_for(boq_rows)
        snapshot.bom_totals = totals_for(bom_rows)
        snapshot.regenerated_at = timezone.now()
        snapshot.save()

    return snapshot


# ---------------------------------------------------------------------------
# XLSX export (openpyxl — the backend's available xlsx writer)
# ---------------------------------------------------------------------------

def _write_sheet(ws, title: str, rows: List[Dict], money_cols: int = 6) -> None:
    """Write a BOQ/BOM sheet with headers + section subtotals + grand total."""
    from openpyxl.styles import Alignment, Font, PatternFill

    header_fill = PatternFill("solid", fgColor="E6F2FF")
    bold = Font(bold=True)
    header = ["Section", "Code", "Item", "Unit", "Quantity",
              "Material €", "Labour €", "Rent €", "Total €", "Notes"]
    ws.append(header)
    for cell in ws[1]:
        cell.font = bold
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="left")

    total_col = header.index("Total €") + 1  # 1-based
    last_section = None
    section_total = 0.0
    grand = 0.0

    for r in rows:
        section = str(r.get("section", "")).strip() or "Other"
        if last_section is not None and section != last_section:
            ws.append([f"Subtotal — {last_section}", "", "", "", "", "", "", "",
                       round(section_total, 2), ""])
            for cell in ws[ws.max_row]:
                cell.font = bold
            section_total = 0.0
        last_section = section

        ws.append([
            section,
            r.get("item_code", ""),
            r.get("item_name", ""),
            r.get("unit", ""),
            r.get("quantity", 0),
            r.get("material_rate", 0.0),
            r.get("labour_rate", 0.0),
            r.get("rent_rate", 0.0),
            r.get("amount", 0.0),
            r.get("notes", "") or "",
        ])
        try:
            section_total += float(r.get("amount") or 0.0)
            grand += float(r.get("amount") or 0.0)
        except (TypeError, ValueError):
            pass

    if last_section is not None:
        ws.append([f"Subtotal — {last_section}", "", "", "", "", "", "", "",
                   round(section_total, 2), ""])
        for cell in ws[ws.max_row]:
            cell.font = bold

    ws.append(["Grand Total", "", "", "", "", "", "", "", round(grand, 2), ""])
    for cell in ws[ws.max_row]:
        cell.font = bold

    widths = [22, 10, 42, 8, 12, 12, 12, 12, 12, 26]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w


def render_boq_xlsx(project_id: str, snapshot: Optional[BoqSnapshot] = None,
                    sheets: str = "both") -> bytes:
    """Render BOQ (+ optional BOM) sheet(s) into an XLSX workbook.

    ``sheets``: "both" (default), "boq" or "bom" — lets callers produce
    separate BOQ.xlsx and BOM.xlsx deliverables.
    """
    import io
    from openpyxl import Workbook

    if snapshot is None:
        snapshot = generate_snapshot(project_id)

    wb = Workbook()
    if sheets in ("both", "boq"):
        ws_boq = wb.active
        ws_boq.title = "BoQ"
        _write_sheet(ws_boq, "BoQ", snapshot.boq_json or [])

    if sheets in ("both", "bom"):
        ws_bom = wb.create_sheet("BoM") if sheets == "both" else wb.active
        ws_bom.title = "BoM"
        bom_rows = []
        for r in snapshot.bom_json or []:
            row = dict(r)
            row["material_rate"] = r.get("material_rate", 0.0)
            row["labour_rate"] = 0.0
            row["rent_rate"] = 0.0
            row["amount"] = r.get("material_total", 0.0)
            bom_rows.append(row)
        _write_sheet(ws_bom, "BoM", bom_rows)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
