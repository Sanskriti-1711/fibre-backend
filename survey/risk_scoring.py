"""
Survey change risk scoring (Tier-1 A5).

Scores each survey change by how much LLD re-planning work it will trigger
and how confidently it was captured:

    risk = severity x likelihood x lld_impact

All three are small integer scales combined into a 1-25 score, banded into
low / medium / high / critical. Rule-based — no ML.

Severity    — what changed (geometry moves a trench; attributes rarely do)
Likelihood  — how confident the capture is (GPS quality, evidence, photos)
LLD impact  — how far the change propagates (feeder > distribution > drop)
"""

from __future__ import annotations

import math
from typing import Dict, List

# ── Severity: how much of the feature changed ──────────────────────────────
GEOMETRY_SEVERITY = {
    "moved": 4,        # reroute / vertex move — the change that matters most
    "reshaped": 4,     # multi-vertex edit
    "added": 3,        # brand-new feature (unplanned work)
    "removed": 3,      # removal needs LLD to drop dependents
    "attribute_only": 1,
    "none": 0,
}

# ── LLD impact: how far the change propagates down the hierarchy ──────────
LAYER_IMPACT = {
    "feeder": 5,
    "distribution": 3,
    "drop": 2,
    "garden": 2,
    "hdd": 2,
    "object": 1,
    "pdp": 1,
    "polygon": 1,
    "chamber": 1,
    "premise": 1,
    "coupleur": 1,
}

# Attribute keys whose change actually alters LLD planning decisions.
HIGH_IMPACT_ATTRS = {
    "trench_type", "construction_method", "depth_mm", "width_mm",
    "road_crossing", "footpath_crossing", "rail_crossing", "river_crossing",
    "private_property", "traffic_sensitive", "permit_required",
    "aerial_required", "surface_type",
}

# GPS quality multipliers for capture confidence (likelihood).
GPS_LIKELIHOOD = {
    "ok": 5,        # trustworthy capture
    "unknown": 3,
    "warn": 2,
    "reject": 1,    # override-grade fix — least trustworthy
}

# Evidence bonus: photos/backing make the capture more reviewable.
EVIDENCE_BONUS = {
    0: 0.6,   # nothing attached
    1: 0.8,   # one item
    2: 1.0,   # photos + notes — treat as fully evidenced
}

RISK_BANDS = (
    (16, "critical"),
    (9, "high"),
    (4, "medium"),
    (0, "low"),
)


def _band(raw: int) -> str:
    for threshold, name in RISK_BANDS:
        if raw >= threshold:
            return name
    return "low"

# Severity 0 (nothing material changed) always scores low.


def score_change(sf) -> Dict:
    """Score one SurveyFeature instance. Returns a risk payload dict."""
    severity, sev_key = _severity(sf)
    likelihood = _likelihood(sf)
    impact = _lld_impact(sf)

    raw = severity * likelihood * impact
    # Evidence scales the capture-confidence part (likelihood), bounded 1-5.
    likelihood = max(1, min(5, round(likelihood * _evidence_factor(sf))))
    raw = severity * likelihood * impact
    band = _band(raw)

    factors = _factor_notes(sev_key, likelihood, impact, sf)
    return {
        "score": int(raw),
        "band": band,
        "severity": sev_key,
        "likelihood": int(likelihood),
        "lld_impact": impact,
        "factors": factors,
    }


def score_changes(survey_features) -> Dict[str, Dict]:
    """Bulk-score an iterable of SurveyFeature. Returns {sf_id: risk}."""
    out: Dict[str, Dict] = {}
    for sf in survey_features:
        out[str(sf.id)] = score_change(sf)
    return out


# ── Internals ───────────────────────────────────────────────────────────────

def _severity(sf):
    """4 = geometry moved, 1 = attribute-only, 0 = nothing material."""
    same_geom = _geoms_equal(sf.original_geometry, sf.survey_geometry)
    orig_attrs = sf.original_attributes or {}
    surv_attrs = sf.survey_attributes or {}

    changed_keys = set()
    for k in set(orig_attrs) | set(surv_attrs):
        if (orig_attrs.get(k) or None) != (surv_attrs.get(k) or None):
            changed_keys.add(k)
    high_impact_attr_change = bool(changed_keys & HIGH_IMPACT_ATTRS)

    if sf.is_removal:
        return GEOMETRY_SEVERITY["removed"], "removed"
    if sf.original_hld_feature_id is None:
        return GEOMETRY_SEVERITY["added"], "added"
    if not same_geom:
        # Multiple changed attrs + geometry = reshape-level severity.
        return GEOMETRY_SEVERITY["reshaped"], "reshaped"
    if high_impact_attr_change:
        return GEOMETRY_SEVERITY["attribute_only"], "attribute_only"
    if changed_keys:
        return 1, "cosmetic_only"
    return 0, "none"


def _likelihood(sf) -> int:
    """Base capture confidence from GPS quality (1-5)."""
    q = (sf.gps_quality or "").strip().lower()
    return GPS_LIKELIHOOD.get(q, GPS_LIKELIHOOD["unknown"])


def _evidence_factor(sf) -> float:
    count = (1 if sf.photo else 0) + (1 if (sf.change_reason or "").strip() else 0)
    return EVIDENCE_BONUS.get(min(count, 2), 0.8)


def _lld_impact(sf) -> int:
    """How far down the hierarchy this change propagates (1-5)."""
    layer_key = _layer_key(sf)
    impact = LAYER_IMPACT.get(layer_key)
    if impact:
        return impact
    # Unknown layer — infer from attribute hints.
    attrs = sf.survey_attributes or {}
    usage = str(attrs.get("USAGE_TYPE") or attrs.get("trench_type") or "").lower()
    if "feeder" in usage:
        return 5
    if "distribution" in usage:
        return 3
    if usage in ("garden", "drop") or "garden" in usage:
        return 2
    return 2  # conservative default


def _factor_notes(sev_key, likelihood, impact, sf) -> List[str]:
    notes = []
    if sev_key in ("moved", "reshaped"):
        notes.append("Geometry rerouted — LLD must re-trace this corridor")
    elif sev_key == "added":
        notes.append("New engineer-created feature — LLD must absorb unplanned work")
    elif sev_key == "removed":
        notes.append("Feature removal — LLD must drop dependents")
    elif sev_key == "attribute_only":
        notes.append("Planning-relevant attributes changed (trench type/crossings)")
    if sf.gps_quality == "reject":
        notes.append("Captured on a reject-grade GPS fix (engineer override)")
    elif sf.gps_quality == "warn":
        notes.append("Captured on a degraded GPS fix")
    if not sf.photo and not (sf.change_reason or "").strip():
        notes.append("No photo or reason attached")
    if impact >= 4:
        notes.append("Feeder-level change — expect wide LLD propagation")
    return notes


def _layer_key(sf) -> str:
    raw = (sf.layer_name or sf.layer_id or "").lower()
    for key in LAYER_IMPACT:
        if key in raw:
            return key
    return raw


def _geoms_equal(a, b) -> bool:
    if a is None or b is None:
        return a is b
    if a == b:
        return True
    # Numeric jitter tolerance: compare rounded coordinate lists.
    try:
        ra = json_round_coords(a)
        rb = json_round_coords(b)
        return ra == rb
    except Exception:
        return False


def json_round_coords(geom):
    """Round all coordinates to 6 dp for jitter-insensitive equality."""
    import copy

    def _round(x):
        if isinstance(x, float):
            return round(x, 6)
        if isinstance(x, (list, tuple)):
            return [_round(i) for i in x]
        return x

    g = copy.deepcopy(geom)
    if isinstance(g, dict) and "coordinates" in g:
        g["coordinates"] = _round(g["coordinates"])
    return g
