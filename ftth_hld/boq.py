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
from typing import Any, Dict, List, Optional

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

# Trenching (section 2) — from the single Final_Trenches layer, keyed by the
# construction class (trench_type / CONSTRUCT / USAGE_TYPE all carry it):
# Open Cut (feeder + distribution routes combined), Garden (drop legs), HDD.
# Legacy sublayer values are mapped too so HLD outputs from before the
# single-trench change still quantify.
_TRENCH_RULES = [
    ("Open Cut", "2.1"),
    ("Feeder_Trench", "2.1"),      # legacy per-tier sublayer tag
    ("Distribution_Trench", "2.1"),  # legacy per-tier sublayer tag
    ("Garden", "2.5"),
    ("Garden_Trench", "2.5"),      # legacy per-tier sublayer tag
    ("HDD", "2.6"),
    ("Drill_Trench", "2.6"),       # legacy per-tier sublayer tag
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

# OTB distribution (section 7) — premises per polygon decides the tier.
_OTB_TIERS = [
    ("7.1", 1, 4),   # OTB 1–4 HH
    ("7.2", 5, 8),   # OTB 5–8 HH
    ("7.3", 9, 24),  # OTB 9–24 HH
    ("7.4", 25, 48), # OTB 25–48 HH
]


# BOM material list — mirrors the reference BOM.xlsx sheet (Ducts / Cables /
# Plant Elements / OTB-Termination groups). Each row consumes the quantity of
# the BOQ item it maps to; sub-ducts ride their parent ducts (a feeder duct
# carries the 14/10 feeder sub-duct, the property 1×7/4 sub-duct is the
# distribution sub-duct), exactly as the reference template does.
_BOM_ITEMS = [
    # (group, material description, source BOQ code, unit)
    ("Ducts", "HDPE duct 50/40mm", "3.1", "m"),
    ("Ducts", "HDPE duct 40mm", "3.2", "m"),
    ("Ducts", "HDPE duct 32mm", "3.7", "m"),
    ("Ducts", "Sub-duct 14/10 (feeder)", "3.1", "m"),
    ("Ducts", "Sub-duct 7/4 (distribution)", "3.11", "m"),
    ("Cables", "Fiber cable 288 FO", "4.1", "m"),
    ("Cables", "Fiber cable 144 FO", "4.3", "m"),
    ("Cables", "Fiber cable 96 FO", "4.4", "m"),
    ("Cables", "Fiber cable 48 FO", "4.5", "m"),
    ("Cables", "Fiber cable 24 FO", "4.6", "m"),
    ("Cables", "Fiber cable 12 FO", "4.7", "m"),
    ("Cables", "Fiber cable 4 FO", "4.8", "m"),
    ("Plant Elements", "DP48 distribution point", "6.1", "ea"),
    ("Plant Elements", "MFG / Mini-PoP enclosure", "6.3", "ea"),
    ("Plant Elements", "Handhole B125", "6.4", "ea"),
    ("Plant Elements", "Splice closure", "6.6", "ea"),
    ("Plant Elements", "Optical coupler (drop ↔ distribution)", "6.13", "ea"),
    ("Plant Elements", "1:32 Splitter", "6.7", "ea"),
    ("Plant Elements", "1:8 Splitter", "6.8", "ea"),
    ("Plant Elements", "1:16 Splitter", "6.11", "ea"),
    ("Plant Elements", "1:64 Splitter", "6.12", "ea"),
    ("Plant Elements", "Patchpanel 1HU", "6.9", "ea"),
    ("OTB / Termination", "OTB-4 (1–4 HH)", "7.1", "ea"),
    ("OTB / Termination", "OTB-8 (5–8 HH)", "7.2", "ea"),
    ("OTB / Termination", "OTB-24 (9–24 HH)", "7.3", "ea"),
    ("OTB / Termination", "OTB-48 (25–48 HH)", "7.4", "ea"),
]


def _section_order(code: str) -> tuple[int, float]:
    """Sort key placing items in template order (section, then item)."""
    parts = str(code).split(".")
    try:
        section = int(parts[0])
        item = float(parts[1]) if len(parts) > 1 else 0.0
    except (TypeError, ValueError):
        return (99, 0.0)
    return (section, item)


# Reuse-aware BOQ: features stamped as reused/existing infrastructure (from
# the brownfield classification pass — INFRA_STATUS=Reused when a new trench
# follows an existing duct/trench/fibre corridor, Existing when present but
# unused) are NOT new material. They are excluded from the trench/duct/cable
# quantities and their metres are surfaced separately as a reuse summary so
# the BOQ documents the savings instead of billing for them.
_REUSE_STATUSES = {"reused", "existing"}


def _is_reused(props) -> bool:
    """True when a feature rides existing infrastructure (not new material)."""
    status = str(props.get("INFRA_STATUS") or props.get("infra_status") or "").lower()
    if status in _REUSE_STATUSES:
        return True
    # Survey-layer reuse signals (Mode A approved features keep these):
    # construction_type=existing_* or duct existing/reuse flags.
    ct = str(props.get("construction_type") or props.get("CONSTRUCTION_TYPE") or "").lower()
    if ct.startswith("existing"):
        return True
    if props.get("existing") in (True, "true", "1", 1) or props.get("reuse") in (True, "true", "1", 1):
        return True
    return False


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

# HLD layer name → LLD layer name(s) mapping.  When LLD layers exist the
# BOQ reads from them instead of the HLD originals so quantities reflect
# the final (survey-corrected) design.
_HLD_TO_LLD = {
    "objects": ["objects"],
    "trenches": ["final_trenches"],          # LLD merges all trench sublayers
    "ducts": ["feeder_ducts", "distribution_ducts", "drop_ducts"],
    "cables": ["feeder_cable", "distribution_cable"],
}

# Cache: project_id → latest LldRun (or None)
_lld_cache: Dict[str, Optional[Any]] = {}


def clear_lld_cache(project_id: str = "") -> None:
    """Clear the LLD layer cache so the next BOQ read picks up fresh layers.
    Call with a project_id to clear one project, or empty to clear all."""
    if project_id:
        _lld_cache.pop(project_id, None)
    else:
        _lld_cache.clear()


def _latest_lld_run(project_id: str):
    """Return the latest completed LldRun for a project, or None."""
    if project_id in _lld_cache:
        return _lld_cache[project_id]
    try:
        from ftth_lld.models import LldRun
        run = (
            LldRun.objects
            .filter(ftth_project__project_id=project_id, status=LldRun.STATUS_COMPLETED)
            .order_by("-run_date")
            .first()
        )
        _lld_cache[project_id] = run
        return run
    except Exception:
        _lld_cache[project_id] = None
        return None


class _CombinedLayer:
    """Virtual layer that merges features from multiple LLD layers.

    When one HLD name maps to multiple LLD layers (e.g. 'ducts' →
    feeder_ducts + distribution_ducts + drop_ducts), this merges them
    into a single FeatureCollection so the BOQ can iterate all features.
    """
    def __init__(self, layers):
        features = []
        for layer in layers:
            fc = layer.geojson or {}
            features.extend(fc.get("features", []) if isinstance(fc, dict) else [])
        self.geojson = {"type": "FeatureCollection", "features": features}
        self.name = "+".join(l.name for l in layers)
        self.feature_count = len(features)


def _get_layer(project_id: str, name: str):
    """Get a layer by name, preferring the fresher of LLD output / HLD input.

    When one HLD name maps to multiple LLD layers (e.g. 'ducts' →
    feeder_ducts + distribution_ducts + drop_ducts), returns a combined
    virtual layer containing all features.

    Returns an object with a `.geojson` attribute (FtthLayer, LldLayer,
    or _CombinedLayer — all expose the same interface for _iter_features).
    """
    hld_layer = FtthLayer.objects.filter(
        ftth_project__project_id=project_id, name=name
    ).first()

    # Prefer the LLD only when it is at least as fresh as the HLD layer.
    # Preferring LLD unconditionally priced a re-run HLD with an older LLD
    # snapshot: after a trench-layer fix the BOQ still billed the previous
    # geometry. When the HLD layer is newer (or no LLD exists), the HLD wins.
    lld_run = _latest_lld_run(project_id)
    if lld_run is not None:
        from ftth_lld.models import LldLayer
        lld_names = _HLD_TO_LLD.get(name, [name])
        matched = []
        for lld_name in lld_names:
            try:
                lld_layer = LldLayer.objects.filter(
                    lld_run=lld_run, name=lld_name
                ).first()
                if lld_layer and lld_layer.geojson:
                    matched.append(lld_layer)
            except Exception:
                pass
        if matched:
            lld_times = [t for t in (getattr(l, "updated_at", None) for l in matched) if t]
            newest_lld = max(lld_times) if lld_times else None
            hld_time = getattr(hld_layer, "updated_at", None) if hld_layer is not None else None
            if hld_layer is None or newest_lld is None or hld_time is None or newest_lld >= hld_time:
                if len(matched) == 1:
                    return matched[0]
                return _CombinedLayer(matched)

    return hld_layer


def _iter_features(layer):
    if layer is None:
        return
    fc = layer.geojson or {}
    for f in fc.get("features", []) if isinstance(fc, dict) else []:
        yield f


# ---------------------------------------------------------------------------
# Quantity computation
# ---------------------------------------------------------------------------

def _reuse_len_m(props) -> float:
    """Metres of a feature riding existing infrastructure, when known.

    The pipeline writes this per grouped feature (REUSE_LEN_M) because one
    published trench feature can hold a whole construction sub-category —
    part of it reusing existing ducts and part of it new.
    """
    try:
        v = props.get("REUSE_LEN_M")
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _new_build_length(props, feature) -> float:
    """Length to BILL: the feature's length minus the part that is reused.

    A grouped HLD trench feature (one per Open Cut / Garden / HDD) carries
    many runs, so stamping the whole feature "Reused" billed 0 m of trench and
    wrote the entire network off as reuse. With REUSE_LEN_M only the reused
    metres are excluded; a wholly-reused feature still contributes 0 and a
    wholly-new one its full length.
    """
    raw = props.get("length_m")
    try:
        total = float(raw) if raw is not None else _geometry_length(feature.get("geometry"))
    except (TypeError, ValueError):
        total = _geometry_length(feature.get("geometry"))
    reuse = _reuse_len_m(props)
    if reuse <= 0:
        return 0.0 if _is_reused(props) else total
    return max(0.0, total - reuse)


def _sum_by_attr(project_id: str, layer_name: str, attr: str,
                 match: Dict[str, str], length_attr: Optional[str] = None,
                 count_all: bool = False,
                 skip_reused: bool = False) -> Dict[str, float]:
    """Sum length (or count) of features grouped by matched item code.

    ``match`` maps attribute value → item code. Features whose attribute
    value is not in ``match`` are skipped (unless ``count_all``, in which
    case unmatched features roll into ``"*"``). When ``skip_reused`` is set,
    features stamped as reused/existing infrastructure are excluded — they
    are not new material (tracked separately via ``reused_metres``).
    """
    totals: Dict[str, float] = {}
    layer = _get_layer(project_id, layer_name)
    for f in _iter_features(layer):
        props = f.get("properties", {}) or {}
        if skip_reused and _is_reused(props):
            continue
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


def _sum_by_trench_class(project_id: str) -> Dict[str, float]:
    """Trench metres grouped by construction class → BOQ item code.

    Reads the single Final_Trenches publication. The class lives in
    trench_type / CONSTRUCT / USAGE_TYPE (all carry the same value now);
    the legacy ``sublayer`` tag is checked first so HLD outputs from
    before the single-trench change still quantify. Reused corridors are
    excluded (tracked separately via reused_metres).
    """
    match = dict(_TRENCH_RULES)
    totals: Dict[str, float] = {}
    layer = _get_layer(project_id, "trenches")
    for f in _iter_features(layer):
        props = f.get("properties", {}) or {}
        if _is_reused(props):
            continue
        key = None
        for attr in ("sublayer", "trench_type", "CONSTRUCT", "USAGE_TYPE"):
            val = props.get(attr)
            if val is None:
                continue
            key = match.get(str(val))
            if key is not None:
                break
        if key is None:
            continue
        # Bill only the NEW part: a grouped trench feature carries a whole
        # construction sub-category, so it can be partly reused, partly new.
        qty = _new_build_length(props, f)
        totals[key] = totals.get(key, 0.0) + qty
    return totals


def reused_metres(project_id: str) -> Dict[str, float]:
    """Metres of trench/duct/cable riding existing infrastructure.

    Mirrors the quantity rules (same layer + attribute keys) but counts only
    features classified as reused/existing — the amount the BOQ does NOT bill
    for. Used for reporting the reuse savings, not for quantities.
    """
    totals: Dict[str, float] = {}
    for layer_name, attr, match, length_attr in (
        ("trenches", "construct", dict(_TRENCH_RULES), "length_m"),
        ("ducts", "DUCT_TYPE", dict(_DUCT_RULES), "length_m"),
        ("cables", "CABLE_TYPE", None, None),
    ):
        layer = _get_layer(project_id, layer_name)
        for f in _iter_features(layer):
            props = f.get("properties", {}) or {}
            # A grouped feature is "Mixed": part of it rides existing
            # infrastructure and part is new. It reports those metres in
            # REUSE_LEN_M, so the whole-feature reuse test must not skip it —
            # otherwise the reuse saving disappears from the report entirely.
            if not _is_reused(props) and _reuse_len_m(props) <= 0:
                continue
            if attr == "CABLE_TYPE":
                key = "cable"
            else:
                val = props.get(attr)
                if val is None:
                    val = props.get("trench_type") or props.get("USAGE_TYPE")
                key = match.get(str(val))
                if key is None:
                    continue
            # Prefer the pipeline's per-run reused metres for grouped
            # features (REUSE_LEN_M); fall back to the whole feature.
            qty = _reuse_len_m(props)
            if qty <= 0:
                raw = props.get(length_attr) if length_attr else None
                try:
                    qty = float(raw) if raw is not None else _geometry_length(f.get("geometry"))
                except (TypeError, ValueError):
                    qty = _geometry_length(f.get("geometry"))
            totals[key] = totals.get(key, 0.0) + qty
    return {k: round(v, 2) for k, v in totals.items() if v}


def compute_quantities(project_id: str) -> Dict[str, float]:
    """Compute raw quantities (metres / counts) for every BOQ item code.

    Every item in the reference catalogue that is deterministically derivable
    from the persisted HLD layers is filled; items with no data source stay
    absent (the row builders surface them as 0 rather than dropping them).
    Derived conventions (mirroring the reference template): road restoration
    = the open-cut asphalt trench length, garden duct = garden trench length,
    PoP termination = feeder fibre count.
    """
    qty: Dict[str, float] = {}

    # 1. SUMMARY — home passes (access network) = Σ HH over the premises;
    #    backhaul design (1.2) has no design input and stays 0.
    objects_layer = _get_layer(project_id, "objects")
    object_count = sum(1 for _ in _iter_features(objects_layer))
    home_passes = 0
    for f in _iter_features(objects_layer):
        try:
            home_passes += int((f.get("properties") or {}).get("HH") or 0)
        except (TypeError, ValueError):
            pass
    if home_passes:
        qty["1.1"] = qty.get("1.1", 0.0) + home_passes

    # 2. Trenching — construction-class breakdown from the single trenches
    #    layer (Open Cut / Garden / HDD). ``Final_Trenches`` is the only
    #    trench publication now; the legacy per-tier sublayers still match
    #    via the rule aliases. Features riding reused existing corridors are
    #    excluded — not new trenching.
    trench_totals = _sum_by_trench_class(project_id)
    for code, total in trench_totals.items():
        qty[code] = qty.get(code, 0.0) + total

    # 2.11 Road restoration (asphalt) = the open-cut trench metres under the
    #     road; 2.12 brick/paving has no data source.
    road_cut = qty.get("2.1", 0.0)
    if road_cut:
        qty["2.11"] = qty.get("2.11", 0.0) + road_cut

    # 3. Ducts — by DUCT_TYPE (reused existing ducts excluded — no new HDPE)
    duct_totals = _sum_by_attr(project_id, "ducts", "DUCT_TYPE",
                               dict(_DUCT_RULES), length_attr="length_m",
                               skip_reused=True)
    for code, total in duct_totals.items():
        qty[code] = qty.get(code, 0.0) + total

    # Duct surplus (+2%) → 3.12
    duct_sum = sum(duct_totals.values())
    if duct_sum:
        qty["3.12"] = qty.get("3.12", 0.0) + round(duct_sum * 0.02, 2)

    # 4. Cables — by CABLE_TYPE + FIBER_COUNT; also capture the feeder fibre
    #    count for the PoP termination item (6.10).
    cable_totals: Dict[str, float] = {}
    feeder_fo = 0
    cable_layer = _get_layer(project_id, "cables")
    for f in _iter_features(cable_layer):
        props = f.get("properties", {}) or {}
        if _is_reused(props):
            continue  # reused fibre — not new cable material
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
        if ctype.lower().startswith("feeder"):
            try:
                feeder_fo = max(feeder_fo, int(props.get("FIBER_COUNT") or 0))
            except (TypeError, ValueError):
                pass
    for code, total in cable_totals.items():
        qty[code] = qty.get(code, 0.0) + total

    # Cable surplus (+2%) → 4.9
    cable_sum = sum(cable_totals.values())
    if cable_sum:
        qty["4.9"] = qty.get("4.9", 0.0) + round(cable_sum * 0.02, 2)

    # 6. Plant elements — point counts from the equipment layers.
    pdps = list(_iter_features(_get_layer(project_id, "pdps")))
    mfg = list(_iter_features(_get_layer(project_id, "mfg")))
    chambers = list(_iter_features(_get_layer(project_id, "chambers")))
    coupleurs = list(_iter_features(_get_layer(project_id, "coupleurs")))

    def _int_prop(f, key):
        try:
            return int((f.get("properties") or {}).get(key) or 0)
        except (TypeError, ValueError):
            return 0

    # DP48 with vs without splicing — the design stamps SPLIT_CNT per PDP.
    pdp_with = sum(1 for f in pdps if _int_prop(f, "SPLIT_CNT") > 0)
    pdp_without = max(0, len(pdps) - pdp_with)
    if pdp_with:
        qty["6.1"] = qty.get("6.1", 0.0) + pdp_with
    if pdp_without:
        qty["6.2"] = qty.get("6.2", 0.0) + pdp_without
    if mfg:
        qty["6.3"] = qty.get("6.3", 0.0) + len(mfg)

    # Standard chamber catalogue: HH (6.4), DHH+MH count as their
    # equivalents; splice closures = DHH chambers hosting PDP splitters.
    _ct = lambda f: (f.get("properties") or {}).get("CHAMBER_TYPE")
    n_hh = sum(1 for f in chambers if _ct(f) in ("HH", "Handhole"))
    n_dhh = sum(1 for f in chambers if _ct(f) in ("DHH", "Distribution Handhole"))
    n_mh = sum(1 for f in chambers if _ct(f) in ("MH", "Manhole"))
    if n_hh:
        qty["6.4"] = qty.get("6.4", 0.0) + n_hh
    if n_mh:
        qty["6.5"] = qty.get("6.5", 0.0) + n_mh
    if n_dhh:
        # DHH hosts splitter install + distribution splicing → closure line.
        qty["6.6"] = qty.get("6.6", 0.0) + n_dhh

    # Couplers at pseudo → object duct connections (own Coupleurs layer;
    # fall back to chambers carrying coupler equipment for legacy runs).
    n_cpl = sum(1 for f in coupleurs)
    if not n_cpl:
        n_cpl = sum(
            1 for f in chambers
            if "coupler" in str((f.get("properties") or {}).get("EQUIPMENT") or "").lower()
        )
    if n_cpl:
        qty["6.13"] = qty.get("6.13", 0.0) + n_cpl

    # Splitters — the design stamps per-ratio counts (SPL_32 / SPL_8 are the
    # template items; SPL_16 / SPL_64 are the ratios this design actually
    # deploys, surfaced as 6.11 / 6.12).
    for attr, code in (("SPL_32", "6.7"), ("SPL_8", "6.8"),
                       ("SPL_16", "6.11"), ("SPL_64", "6.12")):
        n = sum(_int_prop(f, attr) for f in pdps)
        if n:
            qty[code] = qty.get(code, 0.0) + n

    # PoP termination = every fibre of the feeder cable terminated at the PoP.
    if feeder_fo:
        qty["6.10"] = qty.get("6.10", 0.0) + feeder_fo

    # 7. OTB distribution — group premises by polygon
    otb = _count_otb_tiers(project_id)
    for code, tier_qty in otb.items():
        qty[code] = qty.get(code, 0.0) + tier_qty

    # 8. Installation — one OTB→ONT hook-up per premise (1st HC); 2nd/3rd HC
    #    have no per-premise data source.
    if object_count:
        qty["8.1"] = qty.get("8.1", 0.0) + object_count

    # 9. FTTF — access per property = premises; garden trenching + garden
    #    duct follow the garden trench length (the 1×7/4 sub-duct rides it).
    if object_count:
        qty["9.1"] = qty.get("9.1", 0.0) + object_count
    garden = qty.get("2.5", 0.0)
    if garden:
        qty["9.2"] = qty.get("9.2", 0.0) + garden
        qty["9.3"] = qty.get("9.3", 0.0) + garden

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
    """Build BOQ rows — the full rate-card catalogue in template order.

    Every active rate-card item becomes a row (uncomputed items show a 0
    quantity instead of disappearing), so the workbook matches the reference
    BOQ.xlsx layout. Quantities not backed by a rate-card entry (shouldn't
    happen) are appended with a fallback name so nothing is lost.
    """
    rates = _rate_map()
    rows: List[Dict] = []
    for rate in sorted(BoqRate.objects.filter(active=True),
                       key=lambda r: _section_order(r.item_code)):
        code = rate.item_code
        qty = quantities.get(code, 0.0)
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
    # Safety net: computed quantities without a rate-card row.
    known = {r["item_code"] for r in rows}
    for code in sorted(quantities, key=_section_order):
        if code in known:
            continue
        rows.append({
            "section": _section_for(code),
            "item_code": code,
            "item_name": _fallback_name(code),
            "unit": "m" if _is_length(code) else "ea",
            "quantity": quantities[code],
            "material_rate": 0.0, "labour_rate": 0.0, "rent_rate": 0.0,
            "material_total": 0.0, "labour_total": 0.0, "amount": 0.0,
            "notes": "No rate card entry",
        })
    return rows


def build_bom_rows(quantities: Dict[str, float]) -> List[Dict]:
    """Build BOM rows — the reference material list, priced with the rate card.

    Mirrors the BOM sheet structure (Ducts / Cables / Plant Elements /
    OTB-Termination groups) with the material price (material_rate ×
    quantity) per row.
    """
    rates = _rate_map()
    rows: List[Dict] = []
    for group, name, src_code, unit in _BOM_ITEMS:
        qty = quantities.get(src_code, 0.0)
        rate = rates.get(src_code)
        material_rate = rate.material_rate if rate else 0.0
        rows.append({
            "section": group,
            "item_code": src_code,
            "item_name": name,
            "unit": unit,
            "quantity": qty,
            "material_rate": material_rate,
            "material_total": round(qty * material_rate, 2),
            "notes": "",
        })
    return rows


def _section_for(code: str) -> str:
    section_map = {
        "1": "SUMMARY",
        "2": "CIVIL WORKS — TRENCHING",
        "3": "CIVIL WORKS — DUCTS",
        "4": "LINES & CABLES",
        "5": "SPLICING",
        "6": "PLANT ELEMENTS",
        "7": "OTB DISTRIBUTION",
        "8": "INSTALLATION",
        "9": "FTTF (FIBER TO THE FENCE)",
    }
    return section_map.get(code.split(".")[0], "Other")


def _is_length(code: str) -> bool:
    return code.split(".")[0] in ("2", "3", "4")


def _fallback_name(code: str) -> str:
    names = {
        "2.1": "Trench — open cut (feeder + distribution)", "2.3": "Trench — open cut (feeder + distribution)",
        "2.5": "Garden trench — micro-trenching", "2.6": "Drill / HDD crossings",
        "3.1": "Feeder duct HDPE 50/40", "3.7": "Distribution duct HDPE 32",
        "3.11": "Distribution sub-duct 1×7/4 (property)", "3.12": "Duct surplus (+2%)",
        "4.1": "Feeder cable 288 FO", "4.6": "Distribution cable 24 FO",
        "4.9": "Cable surplus (+2%)",
        "6.1": "DP48 (PDP with splicing)", "6.3": "MFG / Mini-PoP",
        "6.4": "Handhole HH 300x300/450x450 (B125)", "6.5": "Manhole MH 1200x1200",
        "6.6": "Distribution Handhole DHH 600x600 (splicing)", "6.13": "Optical coupler (drop ↔ distribution)",
        "7.1": "OTB 1–4 HH", "7.2": "OTB 5–8 HH",
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
        # Reuse summary — metres NOT billed because they ride existing
        # infrastructure (brownfield reuse). Exposed as boq_totals["reuse"]
        # so the UI/report can show the savings.
        try:
            snapshot.boq_totals["reuse"] = reused_metres(project_id)
        except Exception:
            pass
        snapshot.regenerated_at = timezone.now()
        snapshot.save()

    return snapshot


# ---------------------------------------------------------------------------
# XLSX export (openpyxl — the backend's available xlsx writer)
# ---------------------------------------------------------------------------

_BOQ_HEADERS = [
    "#", "Description", "Unit", "Quantity",
    "Material\n€/unit", "Labour\n€/unit", "Rent\n€/unit",
    "Material\n€ Total", "Labour\n€ Total", "Total Cost €", "Remark",
]

_BOM_HEADERS = ["#", "Material Description", "Unit", "Quantity",
                "Unit Price €", "Total €", "Notes"]


def _style_header_row(ws, row_idx: int, fill: str = "E6F2FF") -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    for cell in ws[row_idx]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor=fill)
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)


def _style_bold_row(ws, row_idx: int, col: int = 0) -> None:
    from openpyxl.styles import Font

    ws.cell(row=row_idx, column=1).font = Font(bold=True)
    if col:
        ws.cell(row=row_idx, column=col).font = Font(bold=True)


def _write_boq_sheet(ws, project: Optional[FtthProject], rows: List[Dict]) -> None:
    """Write the BoQ sheet in the reference BOQ.xlsx layout.

    Title + project/date/revision header block, then section header rows,
    all catalogue items, per-section TOTAL rows and a GRAND TOTAL.
    """
    from openpyxl.styles import Font

    today = timezone.localdate().isoformat()
    ws.append(["BILL OF QUANTITIES (BOQ)"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["Project:", (project.name if project else "") or "", "",
               "Date:", today, "", "Revision:"])
    ws.append(["Dataset:", "", "", "Planner:", "", "",
               "Run ID:", project.project_id if project else ""])
    ws.append([])  # blank spacer row

    header_row = ws.max_row + 1
    ws.append(_BOQ_HEADERS)
    _style_header_row(ws, header_row)

    current_section = None
    for r in rows:
        section = str(r.get("section", "")).strip() or "Other"
        if section != current_section:
            num = _section_order(r.get("item_code", ""))[0]
            ws.append([f"{num}. {section}", "", "", "", "", "", "", "", "", "", ""])
            _style_bold_row(ws, ws.max_row)
            current_section = section
        ws.append([
            r.get("item_code", ""),
            r.get("item_name", ""),
            r.get("unit", ""),
            r.get("quantity", 0),
            r.get("material_rate", 0.0),
            r.get("labour_rate", 0.0),
            r.get("rent_rate", 0.0),
            r.get("material_total", 0.0),
            r.get("labour_total", 0.0),
            r.get("amount", 0.0),
            r.get("notes", "") or "",
        ])

    # Per-section totals + grand total (reference layout).
    totals: Dict[str, float] = {}
    for r in rows:
        try:
            key = str(r.get("section") or "Other")
            totals[key] = totals.get(key, 0.0) + float(r.get("amount") or 0.0)
        except (TypeError, ValueError):
            pass
    # Keep template order: sections appear in the same order as their rows.
    ordered = []
    for r in rows:
        s = str(r.get("section") or "Other")
        if s not in ordered:
            ordered.append(s)
    for section in ordered:
        ws.append([f"TOTAL {section}", "", "", "", "", "", "", "", "",
                   round(totals.get(section, 0.0), 2), ""])
        _style_bold_row(ws, ws.max_row, col=10)
    ws.append(["GRAND TOTAL", "", "", "", "", "", "", "", "",
               round(sum(totals.values()), 2), ""])
    _style_bold_row(ws, ws.max_row, col=10)

    widths = [9, 42, 10, 12, 12, 12, 12, 12, 12, 12, 26]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w


def _write_bom_sheet(ws, project: Optional[FtthProject], rows: List[Dict]) -> None:
    """Write the BoM sheet in the reference BOM.xlsx layout."""
    from openpyxl.styles import Font

    today = timezone.localdate().isoformat()
    ws.append(["BILL OF MATERIALS (BOM)"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["Project:", (project.name if project else "") or "", "",
               "Date:", today])
    ws.append([])

    header_row = ws.max_row + 1
    ws.append(_BOM_HEADERS)
    _style_header_row(ws, header_row)

    idx = 0
    current_group = None
    for r in rows:
        group = str(r.get("section", "")).strip() or "Other"
        if group != current_group:
            ws.append([group, "", "", "", "", "", ""])
            _style_bold_row(ws, ws.max_row)
            current_group = group
        idx += 1
        ws.append([
            idx,
            r.get("item_name", ""),
            r.get("unit", ""),
            r.get("quantity", 0),
            r.get("material_rate", 0.0),
            r.get("material_total", 0.0),
            r.get("notes", "") or "",
        ])

    total = 0.0
    for r in rows:
        try:
            total += float(r.get("material_total") or 0.0)
        except (TypeError, ValueError):
            pass
    ws.append(["TOTAL MATERIALS", "", "", "", "", round(total, 2), ""])
    _style_bold_row(ws, ws.max_row, col=6)

    widths = [8, 42, 10, 12, 12, 12, 22]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w


def render_boq_xlsx(project_id: str, snapshot: Optional[BoqSnapshot] = None,
                    sheets: str = "both") -> bytes:
    """Render BOQ (+ optional BOM) sheet(s) into an XLSX workbook.

    ``sheets``: "both" (default), "boq" or "bom" — lets callers produce
    separate BOQ.xlsx and BOM.xlsx deliverables. The sheets replicate the
    reference template layout: title + header block (project/date/run id),
    the full rate-card catalogue with section headers, per-section totals
    and a grand total.
    """
    import io
    from openpyxl import Workbook

    if snapshot is None:
        snapshot = generate_snapshot(project_id)

    project = FtthProject.objects.filter(pk=project_id).first()

    wb = Workbook()
    if sheets in ("both", "boq"):
        ws_boq = wb.active
        ws_boq.title = "BoQ"
        _write_boq_sheet(ws_boq, project, snapshot.boq_json or [])

    if sheets in ("both", "bom"):
        ws_bom = wb.create_sheet("BoM") if sheets == "both" else wb.active
        ws_bom.title = "BoM"
        _write_bom_sheet(ws_bom, project, snapshot.bom_json or [])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
