"""Permit rule engine — deterministic GIS/attribute analysis for a project.

Iterates the rule catalogue, runs each rule against the project's persisted
HLD/LLD layers, and upserts ``PermitMatrix`` rows with full traceability
(rule id + version snapshot + evidence). Missing reference data is recorded as
a gap note — never as "no permit needed".
"""

from __future__ import annotations

import json

from django.db import connection

from ..analysis.spatial_intersection import (
    gis_table_exists,
    intersections_with,
)
from ..models import PermitAuthority, PermitEvent, PermitMatrix, PermitRule
from .registry import RULE_CATALOGUE, RuleDef

# Rules that read the LLD final_trenches layer from the LLD layer store.
_LLD_ROUTE_RULES = {"TRAFFIC_001", "UTILITY_REUSE_001"}


def _latest_lld_layer_rows(project_id: str, layer_name: str, limit: int = 300):
    """Rows of an LLD output layer (geojson in ``business.ftth_lld_layers``)
    for the project's most recent LLD run."""
    sql = """
        SELECT f.value->'properties' AS props
        FROM business.ftth_lld_layers l,
             jsonb_array_elements(l.geojson->'features') f
        WHERE l.name = %s
          AND l.lld_run_id = (
              SELECT id FROM business.ftth_lld_runs
              WHERE ftth_project_id = %s
              ORDER BY run_date DESC LIMIT 1
          )
        LIMIT %s
    """
    rows = []
    with connection.cursor() as cur:
        cur.execute(sql, [layer_name, project_id, limit])
        for (props,) in cur.fetchall():
            if isinstance(props, str):
                props = json.loads(props)
            rows.append(props or {})
    return rows


def _ensure_rule(rule_def: RuleDef) -> tuple[PermitRule, int]:
    """Get-or-create the PermitRule row for a catalogue rule. Returns the
    rule and its current version (the version snapshot stored on matrix rows)."""
    authority = None
    if rule_def.authority_code:
        authority = PermitAuthority.objects.filter(code=rule_def.authority_code).first()
    rule, _ = PermitRule.objects.get_or_create(
        rule_id=rule_def.rule_id,
        defaults={
            "name": rule_def.name,
            "description": rule_def.description,
            "layer_a": rule_def.layer_a,
            "layer_b": rule_def.layer_b,
            "operator": rule_def.operator,
            "required_level": rule_def.required_level,
            "authority": authority,
            "evidence_required": rule_def.evidence_required,
            "blocks_construction": rule_def.blocks_construction,
        },
    )
    return rule, rule.version


def _upsert_permit(project_id: str, rule: PermitRule, route_section: str,
                   layer: str, evidence: dict, notes: str = "",
                   required: bool | None = None) -> PermitMatrix:
    """Create (or update-in-place) a matrix row for one route section."""
    required = rule.required_level == "REQUIRED" if required is None else required
    obj, created = PermitMatrix.objects.get_or_create(
        project_id=project_id,
        rule=rule,
        route_section=route_section[:128],
        defaults={
            "layer": layer,
            "authority": rule.authority,
            "permit_type": rule.name,
            "rule_version": str(rule.version),
            "required": required,
            "blocks_construction": rule.blocks_construction,
            "evidence": evidence,
            "analysis_notes": notes,
            "status": PermitMatrix.STATUS_IDENTIFIED,
            "readiness_pct": 0,
        },
    )
    if not created:
        # Refresh the evidence/notes snapshot; keep review state untouched.
        obj.evidence = {**obj.evidence, **evidence}
        if notes:
            obj.analysis_notes = notes
        obj.save(update_fields=["evidence", "analysis_notes", "updated_at"])
    PermitEvent.objects.get_or_create(
        permit=obj,
        event="IDENTIFIED",
        defaults={"detail": {"rule_id": rule.rule_id, "created": bool(created)}},
    )
    return obj


def run_analysis(project_id: str, user=None) -> dict:
    """Run the full rule catalogue for a project. Returns a summary dict."""
    summary = {
        "project_id": project_id,
        "rules_fired": [],
        "rows_created": 0,
        "notes": [],
        "gaps": [],
    }

    for rule_def in RULE_CATALOGUE:
        rule, version = _ensure_rule(rule_def)

        # ── LLD attribute rules (traffic / utility reuse) ────────────────
        if rule_def.rule_id in _LLD_ROUTE_RULES:
            rows = _latest_lld_layer_rows(project_id, "final_trenches")
            if not rows:
                summary["gaps"].append(
                    f"{rule_def.rule_id}: no final_trenches LLD output for project"
                )
                continue
            fired = 0
            for props in rows:
                if rule_def.rule_id == "TRAFFIC_001":
                    surface = props.get("SURFACE") or ""
                    if surface not in ("Asphalt", "Footpath"):
                        continue
                    evidence = {
                        "road_class": {"present": bool(props.get("CONSTRUCT")), "value": props.get("CONSTRUCT")},
                        "lane_impact": {"present": False, "value": None},
                        "tmp_document": {"present": False, "value": None},
                    }
                else:  # UTILITY_REUSE_001
                    reuse = props.get("REUSE_SOURCE") or ""
                    if not reuse:
                        continue
                    evidence = {
                        "reuse_source": {"present": True, "value": reuse},
                        "capacity_check": {"present": bool(props.get("INFRA_STATUS") == "Reused"), "value": props.get("INFRA_STATUS")},
                    }
                route_section = str(props.get("feature_id") or props.get("id") or "unknown")
                _upsert_permit(
                    project_id, rule, route_section,
                    layer="final_trenches", evidence=evidence,
                )
                fired += 1
            summary["rules_fired"].append({"rule_id": rule_def.rule_id, "rows": fired})
            summary["rows_created"] += fired
            continue

        # ── Spatial intersection rules (railway / water / environmental) ──
        if rule_def.operator == "INTERSECTS":
            if not gis_table_exists(rule_def.layer_b):
                summary["gaps"].append(
                    f"{rule_def.rule_id}: reference layer gis.{rule_def.layer_b} "
                    "not present — no permit identified until the layer exists"
                )
                continue
            # Route layers to evaluate: the rule's declared layer when it is a
            # real gis table (e.g. final_trenches once persisted), plus the
            # HLD output trench_layer so HLD-stage permits fire today.
            route_tables = []
            if gis_table_exists(rule_def.layer_a) and rule_def.layer_a != "trench_layer":
                route_tables.append(rule_def.layer_a)
            if gis_table_exists("trench_layer"):
                route_tables.append("trench_layer")
            if not route_tables:
                summary["gaps"].append(
                    f"{rule_def.rule_id}: no route layer (gis.trench_layer / "
                    f"gis.{rule_def.layer_a}) present"
                )
                continue
            fired = 0
            for route_table in route_tables:
                hits = intersections_with(route_table, project_id, rule_def.layer_b)
                if not hits:
                    continue
                for hit in hits:
                    evidence = {
                        "crossing_coordinate": {
                            "present": hit.get("crossing_lng") is not None,
                            "value": (
                                [hit.get("crossing_lng"), hit.get("crossing_lat")]
                                if hit.get("crossing_lng") is not None else None
                            ),
                        },
                    }
                    if rule_def.rule_id == "RAILWAY_CROSSING_001":
                        evidence.update({
                            "hdd_design": {"present": False, "value": None},
                            "profile_drawing": {"present": False, "value": None},
                        })
                    _upsert_permit(
                        project_id, rule, str(hit["route_id"]),
                        layer=route_table, evidence=evidence,
                    )
                    fired += 1
            if not fired:
                summary["notes"].append(f"{rule_def.rule_id}: no intersections found")
                continue
            summary["rules_fired"].append({"rule_id": rule_def.rule_id, "rows": fired})
            summary["rows_created"] += fired
            continue

        # ── Attribute road-authority rule (fclass on trench segments) ────
        if rule_def.operator == "ATTRIBUTE":
            if not gis_table_exists("trench_layer"):
                summary["gaps"].append(
                    f"{rule_def.rule_id}: gis.trench_layer not present"
                )
                continue
            with connection.cursor() as cur:
                # Key on the gis row ``id`` (bigserial) — NOT fid: the five
                # trench sub-layers (feeder/distribution/garden/drill/final)
                # merge into trench_layer with colliding fids, so fid is not
                # unique per feature.
                cur.execute(
                    """
                    SELECT id, properties->>'fclass' AS fclass
                    FROM gis.trench_layer
                    WHERE project_id = %s
                      AND properties->>'fclass' IS NOT NULL
                      AND properties->>'fclass' <> ''
                    LIMIT 3000
                    """,
                    [project_id],
                )
                hits = cur.fetchall()
            if not hits:
                summary["gaps"].append(
                    f"{rule_def.rule_id}: no fclass persisted on trench segments — "
                    "persist road classification to enable authority mapping"
                )
                continue
            fired = 0
            for route_id, fclass in hits:
                evidence = {
                    "road_class": {"present": True, "value": fclass},
                    "road_owner": {"present": False, "value": None},
                }
                _upsert_permit(
                    project_id, rule, str(route_id),
                    layer="trench_layer", evidence=evidence,
                )
                fired += 1
            summary["rules_fired"].append({"rule_id": rule_def.rule_id, "rows": fired})
            summary["rows_created"] += fired

    # Refresh readiness on every touched row (evidence satisfaction check).
    _refresh_readiness(project_id)
    return summary


def _refresh_readiness(project_id: str) -> None:
    """Recompute readiness_pct for a project's permit rows from evidence.

    Bulk-updates only the rows whose pct changed (avoids N individual
    UPDATEs over a remote DB — the permit matrix can be thousands of rows).
    """
    rows = list(
        PermitMatrix.objects.filter(project_id=project_id).select_related("rule")
    )
    changed: list[PermitMatrix] = []
    for pm in rows:
        rule = pm.rule
        if not rule:
            continue
        required_keys = rule.evidence_required or []
        if not required_keys:
            pct = 100
        else:
            present = sum(
                1 for k in required_keys
                if (pm.evidence.get(k) or {}).get("present")
            )
            pct = round(present / len(required_keys) * 100)
        if pct != pm.readiness_pct:
            pm.readiness_pct = pct
            changed.append(pm)
    if changed:
        PermitMatrix.objects.bulk_update(changed, ["readiness_pct"], batch_size=500)


def project_summary(project_id: str) -> dict:
    """Aggregate the permit matrix for one project (status counts + readiness)."""
    rows = list(PermitMatrix.objects.filter(project_id=project_id))
    counts: dict[str, int] = {}
    for pm in rows:
        counts[pm.status] = counts.get(pm.status, 0) + 1
    avg = round(sum(pm.readiness_pct for pm in rows) / len(rows)) if rows else 0
    return {
        "project_id": project_id,
        "total": len(rows),
        "by_status": counts,
        "readiness_pct": avg,
        "blocks_construction": any(pm.blocks_construction and pm.required for pm in rows),
    }
