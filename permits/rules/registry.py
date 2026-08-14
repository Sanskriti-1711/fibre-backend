"""Permit rule catalogue — the deterministic source of permit identification.

Each rule maps a spatial/attribute condition to a permit type + authority and
lists the evidence keys that must be satisfied before a matrix row is READY.
Rules never fire on missing data: the engine records a gap note instead of
claiming "no permit needed" (traceability rule from the design doc).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuleDef:
    rule_id: str
    name: str
    description: str
    layer_a: str          # route layer (final_trenches, duct_layer, ...)
    layer_b: str          # reference layer ('' = attribute-only rule)
    operator: str         # INTERSECTS | ATTRIBUTE
    permit_type: str
    required_level: str   # REQUIRED | POTENTIAL
    authority_code: str   # resolved against PermitAuthority.code
    evidence_required: list = field(default_factory=list)
    blocks_construction: bool = False


# ── Road authority — from persisted road class on trench segments ────────
# Fires only when the route layer carries an fclass/highway value (the engine
# persists it onto trench segments); until then it records a gap note.
ROAD_AUTHORITY_RULE = RuleDef(
    rule_id="ROAD_AUTHORITY_001",
    name="Road authority identification",
    description=(
        "Route runs on a classified road — resolve the responsible road "
        "authority (Straßenbaulastträger) from the road class."
    ),
    layer_a="final_trenches",
    layer_b="",  # attribute rule on fclass
    operator="ATTRIBUTE",
    permit_type="Road Opening",
    required_level="POTENTIAL",
    authority_code="DE-ROAD-UNKNOWN",  # refined per class at runtime
    evidence_required=["road_class", "road_owner"],
)

# ── Railway crossing — route intersects a railway corridor ──────────────
RAILWAY_CROSSING_RULE = RuleDef(
    rule_id="RAILWAY_CROSSING_001",
    name="Railway crossing approval",
    description=(
        "FTTH route intersects a railway corridor — railway crossing approval "
        "with HDD design / profile drawing / crossing calculations."
    ),
    layer_a="final_trenches",
    layer_b="osm_railway",
    operator="INTERSECTS",
    permit_type="Railway Crossing",
    required_level="REQUIRED",
    authority_code="DE-RAIL-DB",
    evidence_required=["crossing_coordinate", "hdd_design", "profile_drawing"],
    blocks_construction=True,
)

# ── Waterway crossing — route intersects a river / canal ────────────────
WATERWAY_CROSSING_RULE = RuleDef(
    rule_id="WATERWAY_CROSSING_001",
    name="Waterway crossing approval",
    description=(
        "FTTH route intersects a waterway — water authority approval with "
        "crossing information; environmental review potentially required."
    ),
    layer_a="final_trenches",
    layer_b="osm_waterway",
    operator="INTERSECTS",
    permit_type="Waterway Crossing",
    required_level="REQUIRED",
    authority_code="DE-WATER-BERLIN",
    evidence_required=["crossing_coordinate", "crossing_drawing"],
)

# ── Environmental — route intersects protected habitat / nature reserve ──
ENVIRONMENTAL_RULE = RuleDef(
    rule_id="ENVIRONMENTAL_001",
    name="Environmental review",
    description=(
        "FTTH route intersects protected habitat, nature reserve, forest or "
        "environmental zone — environmental review and tree/root protection "
        "where applicable."
    ),
    layer_a="final_trenches",
    layer_b="osm_environmental",
    operator="INTERSECTS",
    permit_type="Environmental Review",
    required_level="POTENTIAL",
    authority_code="DE-ENV-BERLIN",
    evidence_required=["zone_type", "impact_assessment"],
)

# ── Traffic management — every construction segment on a surfaced route ──
TRAFFIC_RULE = RuleDef(
    rule_id="TRAFFIC_001",
    name="Traffic management plan",
    description=(
        "Construction on a surfaced route (asphalt/footpath) requires a "
        "traffic management plan derived from surface, reinstatement and "
        "construction method."
    ),
    layer_a="final_trenches",
    layer_b="",  # attribute rule on SURFACE / CONSTRUCT / REINSTATE
    operator="ATTRIBUTE",
    permit_type="Traffic Management",
    required_level="POTENTIAL",
    authority_code="DE-MUNI-UNKNOWN",
    evidence_required=["road_class", "lane_impact", "tmp_document"],
)

# ── Existing utility coexistence — reused brownfield assets on the route ─
# Not a permit itself — feeds the utility-conflict evidence of the package.
UTILITY_REUSE_RULE = RuleDef(
    rule_id="UTILITY_REUSE_001",
    name="Existing utility coexistence",
    description=(
        "Route reuses existing brownfield utility assets — record coexistence "
        "and capacity evidence for the permit package."
    ),
    layer_a="final_trenches",
    layer_b="brownfield",
    operator="ATTRIBUTE",
    permit_type="Utility Coexistence",
    required_level="POTENTIAL",
    authority_code="",  # informational — no external authority
    evidence_required=["reuse_source", "capacity_check"],
)

# The catalogue — engine iterates these in order.
RULE_CATALOGUE: list[RuleDef] = [
    ROAD_AUTHORITY_RULE,
    RAILWAY_CROSSING_RULE,
    WATERWAY_CROSSING_RULE,
    ENVIRONMENTAL_RULE,
    TRAFFIC_RULE,
    UTILITY_REUSE_RULE,
]


def get_rule(rule_id: str) -> RuleDef | None:
    for rule in RULE_CATALOGUE:
        if rule.rule_id == rule_id:
            return rule
    return None
