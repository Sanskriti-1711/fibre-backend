"""
Auto-classify permit type from trench attributes (P22b) — heuristic now + label collection.

P22b is 7 days + 500 labeled rows if done as ML. We ship a deterministic
heuristic today that is useful offline and collects labels as you correct it:
every call to `classify(...)` returns {permit_type, confidence, rule_id,
reasons, applied_rules}. When you POST /classify/feedback with the true
permit type, the correction is stored (label file) and contributes to the
future training set. When 500+ labels accumulate, a sklearn pipeline can be
trained without changing the API shape.

Features: fclass, SURFACE, trench_type, CONSTRUCT/REINSTATE, VERIFY_STATUS,
suggested env flags (protected_area/tree/rail/water). No DB mutation except
the optional label file (and an optional PermitAiDraft for audit when a
permit_id is supplied).

Deterministic: no LLM — the boundary is permits/rules + this module. AI is
only an optional note via advisory when PERMITS_LLM_* is configured.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

# Label store — JSONL file next to the module, not a DB table (so it works
# before any migration and is easy to export for training).
_LABEL_PATH = Path(__file__).with_name("classify_labels.jsonl")

FCLASS_ROAD = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified", "service", "living_street", "road"}
FCLASS_FOOTWAY = {"footway", "cycleway", "pedestrian", "path", "steps", "track", "bridleway"}

TRAFFIC_SURFACES = {"Asphalt", "Footpath", "Concrete", "Paving"}

RULE_ORDER = ["ENVIRONMENTAL_002", "ENVIRONMENTAL_001", "ENVIRONMENTAL_003", "RAILWAY_CROSSING_001", "WATERWAY_CROSSING_001", "ROAD_AUTHORITY_001", "TRAFFIC_001", "UTILITY_REUSE_001"]

PERMIT_TYPE_FOR_RULE: dict[str, str] = {
    "ROAD_AUTHORITY_001": "Road Opening",
    "RAILWAY_CROSSING_001": "Railway Crossing",
    "WATERWAY_CROSSING_001": "Waterway Crossing",
    "ENVIRONMENTAL_001": "Environmental Review",
    "ENVIRONMENTAL_002": "Environmental Review",
    "ENVIRONMENTAL_003": "Tree Protection",
    "TRAFFIC_001": "Traffic Management",
    "UTILITY_REUSE_001": "Utility Coexistence",
}


def _norm(s: Any) -> str:
    return str(s or "").strip()


def _lower(s: Any) -> str:
    return str(s or "").strip().lower()


def _surface_is_traffic(surface: str) -> bool:
    return surface in TRAFFIC_SURFACES or surface.lower() in {x.lower() for x in TRAFFIC_SURFACES}


def classify(features: dict[str, Any]) -> dict[str, Any]:
    """Heuristic permit-type classifier.

    Input keys (all optional): fclass, highway, SURFACE, trench_type,
    CONSTRUCT, REINSTATE, VERIFY_STATUS, street_name, REUSE_SOURCE,
    INFRA_STATUS, env_flags: {railway, waterway, protected_area, landuse,
    tree, private_land}, municipality.

    Returns dict: permit_type, rule_id, confidence (0.0-1.0), reasons: [str],
    candidates: [{permit_type, rule_id, score, reasons}], applied_rules: [str].
    The candidates list is sorted best-first, so the UI can show the runner-up.
    """
    fclass = _lower(features.get("fclass") or features.get("highway") or "")
    surface = _norm(features.get("SURFACE") or features.get("surface") or "")
    trench_type = _norm(features.get("trench_type") or features.get("TRENCH_TYPE") or "")
    reuse_source = _norm(features.get("REUSE_SOURCE") or features.get("reuse_source") or "")
    infra_status = _norm(features.get("INFRA_STATUS") or features.get("infra_status") or "")
    verify = _norm(features.get("VERIFY_STATUS") or features.get("verify_status") or "")
    env_flags = features.get("env_flags") or features.get("env") or {}
    if not isinstance(env_flags, dict):
        env_flags = {}

    raw_candidates: list[dict[str, Any]] = []

    def add(rule_id: str, score: float, reasons: list[str]):
        pt = PERMIT_TYPE_FOR_RULE.get(rule_id) or rule_id
        raw_candidates.append({"permit_type": pt, "rule_id": rule_id, "score": score, "reasons": [r for r in reasons if r]})

    # Environmental split — check before road/traffic so a tree/protected-area
    # hit is not swallowed by a generic footway label.
    if env_flags.get("protected_area") or env_flags.get("protected"):
        add("ENVIRONMENTAL_002", 0.92, ["protected_area flag set (nature reserve / protected boundary)", "blocks_construction=True — highest priority"])
    elif env_flags.get("landuse") or env_flags.get("habitat") or _lower(features.get("zone_type")).strip():
        add("ENVIRONMENTAL_001", 0.72, ["landuse/habitat flag or zone_type present"])

    if env_flags.get("tree") or env_flags.get("tree_id"):
        add("ENVIRONMENTAL_003", 0.78, ["tree flag / tree_id present"])

    if env_flags.get("railway") or env_flags.get("rail") or _lower(features.get("rail_crossing")) in ("true", "1", "yes"):
        add("RAILWAY_CROSSING_001", 0.95, ["railway flag set — RAILWAY_CROSSING_001 fires on ST_Intersects", "blocks_construction=True"])

    if env_flags.get("waterway") or env_flags.get("water") or _lower(features.get("river_crossing")) in ("true", "1", "yes"):
        add("WATERWAY_CROSSING_001", 0.90, ["waterway flag set — WATERWAY_CROSSING_001 fires on ST_Intersects"])

    # Road authority — from persisted fclass on trench sections (ROAD_AUTHORITY_001 is attribute, not spatial)
    if fclass:
        if fclass in FCLASS_ROAD or fclass in FCLASS_FOOTWAY:
            # Strong signal for Road Opening; score higher for road classes vs footway
            score = 0.82 if fclass in FCLASS_ROAD else 0.62
            reasons = [f"fclass={fclass} maps to a Straßenbaulastträger (ROAD_AUTHORITY_001)"]
            if fclass in ("motorway", "trunk", "primary"):
                reasons.append("Bund-level road — BUND authority")
            elif fclass in ("secondary", "tertiary"):
                reasons.append("Landes-level road — SenMVKU")
            else:
                reasons.append("Bezirk / private-level road")
            add("ROAD_AUTHORITY_001", score, reasons)

    # Traffic — surfaced route needs TMP (TRAFFIC_001, attribute on SURFACE)
    if _surface_is_traffic(surface) or surface.lower() in ("asphalt", "footpath", "concrete"):
        add("TRAFFIC_001", 0.70, [f"SURFACE={surface or 'surfaced'} implies TRAFFIC_001 (TMP required)"])
    elif trench_type.lower() in ("feeder", "distribution") and not surface:
        # Weak hint when surface is missing but trench type suggests carriageway
        add("TRAFFIC_001", 0.42, ["trench_type suggests carriageway but SURFACE is blank — check surface before submitting"])

    # Utility reuse — informational coexistence (not a real permit, but useful for the package)
    if reuse_source or infra_status.lower() == "reused":
        add("UTILITY_REUSE_001", 0.66, [f"REUSE_SOURCE={reuse_source or 'present'} / INFRA_STATUS={infra_status or '—'} — informational coexistence"])

    # Deduplicate by rule_id, keep highest score per rule
    best: dict[str, dict[str, Any]] = {}
    for c in raw_candidates:
        cur = best.get(c["rule_id"])
        if not cur or c["score"] > cur["score"]:
            best[c["rule_id"]] = c
    candidates = sorted(best.values(), key=lambda c: (-c["score"], RULE_ORDER.index(c["rule_id"]) if c["rule_id"] in RULE_ORDER else 99))

    if not candidates:
        return {
            "permit_type": "Unknown",
            "rule_id": None,
            "confidence": 0.0,
            "reasons": ["No trench attributes match any rule — check fclass, SURFACE, and env flags."],
            "candidates": [],
            "applied_rules": [],
            "is_heuristic": True,
        }

    top = candidates[0]
    # Calibrate confidence: sole strong hit >0.85, two competing hits ~0.55, weak hint ~0.42
    conf = float(top["score"])
    if len(candidates) >= 2 and candidates[1]["score"] >= 0.65 and abs(candidates[0]["score"] - candidates[1]["score"]) < 0.15:
        conf = max(0.45, conf - 0.18)
        top["reasons"] = top["reasons"] + [f"Runner-up {candidates[1]['permit_type']} ({candidates[1]['rule_id']}) is close — verify env flags / fclass."]

    return {
        "permit_type": top["permit_type"],
        "rule_id": top["rule_id"],
        "confidence": round(conf, 2),
        "reasons": top["reasons"],
        "candidates": candidates,
        "applied_rules": [c["rule_id"] for c in candidates],
        "is_heuristic": True,
    }


def store_feedback(
    features: dict[str, Any],
    predicted: dict[str, Any],
    true_rule_id: str | None,
    true_permit_type: str | None,
    *,
    project_id: str | None = None,
    permit_id: str | None = None,
    user_email: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Append one labeled row to the label store and return the stored record.

    Validates true_rule_id / true_permit_type against known values; caller
    should map unknown strings to None rather than inventing a permit type.
    """
    # Normalize true label
    true_rule = _norm(true_rule_id) or None
    true_type = _norm(true_permit_type) or None
    if true_rule and true_rule not in PERMIT_TYPE_FOR_RULE and true_rule not in set(PERMIT_TYPE_FOR_RULE.values()):
        # Allow permit_type strings as true_rule convenience
        for rid, pt in PERMIT_TYPE_FOR_RULE.items():
            if _lower(pt) == _lower(true_rule) or _lower(rid) == _lower(true_rule):
                true_rule = rid
                true_type = pt
                break
    if true_type and true_type not in set(PERMIT_TYPE_FOR_RULE.values()):
        # Try to map back
        for rid, pt in PERMIT_TYPE_FOR_RULE.items():
            if _lower(pt) == _lower(true_type):
                true_rule = true_rule or rid
                true_type = pt
                break

    record = {
        "ts": int(time.time()),
        "project_id": project_id,
        "permit_id": permit_id,
        "features": {k: str(v)[:300] if isinstance(v, str) else v for k, v in (features or {}).items()},
        "predicted_rule_id": predicted.get("rule_id"),
        "predicted_permit_type": predicted.get("permit_type"),
        "predicted_confidence": predicted.get("confidence"),
        "true_rule_id": true_rule,
        "true_permit_type": true_type,
        "user": user_email,
        "note": (note or "")[:500],
    }
    try:
        _LABEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LABEL_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return record


def label_stats() -> dict[str, Any]:
    """Counts of stored labels (for QA / readiness for ML)."""
    if not _LABEL_PATH.exists():
        return {"total": 0, "by_true_rule": {}, "by_predicted_rule": {}, "path": str(_LABEL_PATH)}
    total = 0
    by_true: dict[str, int] = {}
    by_pred: dict[str, int] = {}
    with open(_LABEL_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                rec = json.loads(line)
            except Exception:
                continue
            tr = rec.get("true_rule_id") or rec.get("true_permit_type") or "Unknown"
            pr = rec.get("predicted_rule_id") or "Unknown"
            by_true[str(tr)] = by_true.get(str(tr), 0) + 1
            by_pred[str(pr)] = by_pred.get(str(pr), 0) + 1
    return {"total": total, "by_true_rule": by_true, "by_predicted_rule": by_pred, "path": str(_LABEL_PATH), "milestone_500": total >= 500}


def classify_trench_features(trench_row: dict[str, Any]) -> dict[str, Any]:
    """Compatibility shim for callers that pass a raw LLD/HLD trench row."""
    # Map common trench column names to classifier input keys
    features: dict[str, Any] = {}
    for k in ("fclass", "highway", "SURFACE", "surface", "trench_type", "TRENCH_TYPE", "CONSTRUCT", "REINSTATE", "VERIFY_STATUS", "REUSE_SOURCE", "INFRA_STATUS", "street_name", "municipality"):
        if k in trench_row and trench_row[k] not in (None, ""):
            features[k] = trench_row[k]
    # Preserve env flags if the caller passed them
    for k in ("env_flags", "env", "zone_type", "rail_crossing", "river_crossing"):
        if k in trench_row:
            features[k] = trench_row[k]
    return classify(features)
