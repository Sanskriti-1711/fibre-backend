"""Survey-stage permit evidence hook.

When a survey change is **approved**, the permit matrix rows for that route
section are enriched with the field evidence the engineer captured:

- trench classification (type / construction method / surface)
- crossings (road / footpath / rail / river / private property)
- traffic sensitivity and permit flags
- existing-asset reuse (ducts, chambers, poles → utility-coexistence evidence)
- photos / field evidence count
- risk-assessment categories (railway / water / protected area / trees)

Permits only **consume** approved survey state — the survey approval flow
itself is untouched. The hook is idempotent and never raises: a survey
approval must never fail because the permit enrichment failed.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from ftth_hld.models import FtthProject
from permits.models import PermitEvent, PermitMatrix, PermitRule
from projects.models import Project
from survey.models import (
    ExistingAsset,
    FieldEvidence,
    RiskAssessment,
    SurveyFeature,
    TrenchSurvey,
)

logger = logging.getLogger(__name__)

# Survey layer name → permit matrix layer value (final_trenches is the LLD
# route layer that TRAFFIC / UTILITY rules key off).
LAYER_ALIASES = {
    "final_trenches": "final_trenches",
    "trenches": "final_trenches",
    "trench": "final_trenches",
    "imp-final_trenches": "final_trenches",
}


def _survey_copy(ftth_project_id: str) -> Project | None:
    """The survey copy Project linked to an HLD run, or None."""
    return Project.objects.filter(source_ftth_project_id=ftth_project_id).first()


def _feature_survey_facts(feature_id) -> dict:
    """Aggregate the typed survey facts recorded against an HLD feature."""
    facts: dict = {}
    trench = TrenchSurvey.objects.filter(feature_id=feature_id).first()
    if trench is not None:
        facts["trench_type"] = trench.trench_type or ""
        facts["construction_method"] = trench.construction_method or ""
        facts["surface_type"] = trench.surface_type or ""
        facts["depth_mm"] = trench.depth_mm
        facts["road_crossing"] = bool(trench.road_crossing)
        facts["footpath_crossing"] = bool(trench.footpath_crossing)
        facts["rail_crossing"] = bool(trench.rail_crossing)
        facts["river_crossing"] = bool(trench.river_crossing)
        facts["private_property"] = bool(trench.private_property)
        facts["traffic_sensitive"] = bool(trench.traffic_sensitive)
        facts["permit_required"] = bool(trench.permit_required)
        facts["notes"] = trench.notes or ""
    assets = list(ExistingAsset.objects.filter(feature_id=feature_id))
    facts["reusable_assets"] = [
        {
            "type": a.asset_type,
            "condition": a.condition,
        }
        for a in assets
    ]
    facts["evidence_count"] = FieldEvidence.objects.filter(
        feature_id=feature_id
    ).count()
    risks = list(RiskAssessment.objects.filter(feature_id=feature_id))
    facts["risk_categories"] = sorted({r.risk_category for r in risks})
    return facts


def _evidence_from_facts(facts: dict) -> dict:
    """Map survey facts onto the evidence keys the rules declare."""
    evidence: dict = {}

    # Road / traffic evidence (TRAFFIC_001, ROAD_AUTHORITY_001)
    road_class = None
    if facts.get("surface_type"):
        road_class = facts["surface_type"]
    elif facts.get("construction_method"):
        road_class = facts["construction_method"]
    if road_class:
        evidence["road_class"] = {
            "present": True,
            "value": road_class,
            "source": "survey",
        }
    else:
        evidence["road_class"] = {"present": False, "value": None}

    # Crossing evidence (RAILWAY / WATERWAY / traffic-management rules)
    if facts.get("rail_crossing"):
        evidence["railway_crossing"] = {
            "present": True,
            "value": "rail crossing confirmed by survey",
            "source": "survey",
        }
        evidence["crossing_coordinate"] = {"present": False, "value": None}
        evidence["hdd_design"] = {"present": False, "value": None}
        evidence["profile_drawing"] = {"present": False, "value": None}
    if facts.get("river_crossing"):
        evidence["waterway_crossing"] = {
            "present": True,
            "value": "waterway crossing confirmed by survey",
            "source": "survey",
        }
    if facts.get("road_crossing") or facts.get("footpath_crossing"):
        evidence["surface_crossing"] = {
            "present": True,
            "value": "surface crossing confirmed by survey",
            "source": "survey",
        }
    if facts.get("traffic_sensitive"):
        evidence["traffic_sensitive"] = {
            "present": True,
            "value": "traffic-sensitive area flagged by survey",
            "source": "survey",
        }

    # Utility coexistence (UTILITY_REUSE_001)
    if facts.get("reusable_assets"):
        evidence["reuse_source"] = {
            "present": True,
            "value": ", ".join(
                f"{a['type']} ({a['condition']})" for a in facts["reusable_assets"]
            ),
            "source": "survey",
        }
        if facts["reusable_assets"][0]["condition"] == "reuse_possible":
            evidence["capacity_check"] = {
                "present": True,
                "value": "reuse_possible",
                "source": "survey",
            }

    # Photos / field evidence
    if facts.get("evidence_count", 0) > 0:
        evidence["photos"] = {
            "present": True,
            "value": facts["evidence_count"],
            "source": "survey",
        }

    # Environmental review (ENVIRONMENTAL_001)
    if any(c in facts.get("risk_categories", []) for c in
           ("protected_area", "environmental", "tree_roots")):
        evidence["zone_type"] = {
            "present": True,
            "value": ", ".join(
                c for c in ("protected_area", "environmental", "tree_roots")
                if c in facts.get("risk_categories", [])
            ),
            "source": "survey",
        }

    return evidence


def apply_survey_evidence(ftth_project_id: str) -> dict:
    """Enrich permit matrix rows from approved survey changes.

    Returns a summary dict. Never raises — the caller (approval endpoint)
    must not fail because of permit enrichment.
    """
    summary = {
        "project_id": ftth_project_id,
        "survey_features": 0,
        "permit_rows_updated": 0,
        "notes": [],
    }
    try:
        ftth = FtthProject.objects.get(pk=ftth_project_id)
    except FtthProject.DoesNotExist:
        return summary
    copy = _survey_copy(ftth_project_id)
    if copy is None:
        return summary

    approved = list(
        SurveyFeature.objects.filter(
            project=copy,
            survey_status__in=(
                SurveyFeature.SurveyStatus.APPROVED,
                SurveyFeature.SurveyStatus.COMPLETED,
            ),
        ).select_related("original_hld_feature")
    )
    if not approved:
        return summary
    summary["survey_features"] = len(approved)

    updated_pm_ids = set()
    for sf in approved:
        # Route section key matches the LLD layer feature_id the permit
        # matrix uses (TRAFFIC / UTILITY rows key on the survey-copy Feature
        # UUID; new field features key on the SurveyFeature UUID itself).
        route_section = str(sf.original_hld_feature_id or sf.id)
        layer = LAYER_ALIASES.get(
            (sf.layer_name or sf.layer_id or "").strip().lower(),
            (sf.layer_id or "final_trenches"),
        )
        facts = _feature_survey_facts(sf.original_hld_feature_id or sf.id)
        evidence = _evidence_from_facts(facts)

        rows = list(
            PermitMatrix.objects.filter(
                project=ftth,
                route_section=route_section,
            )
        )
        # Also try the layer-scoped lookup for robustness.
        if not rows:
            rows = list(
                PermitMatrix.objects.filter(
                    project=ftth, layer=layer, route_section=route_section
                )
            )
        if not rows:
            summary["notes"].append(f"no permit row for route section {route_section}")
            continue

        for pm in rows:
            merged = {**pm.evidence, **evidence}
            pm.evidence = merged
            pm.analysis_notes = (
                f"Survey evidence applied {timezone.now().isoformat()} — "
                f"{facts.get('trench_type', '')} "
                f"{facts.get('construction_method', '')} "
                f"{facts.get('surface_type', '')}".strip()
            ).strip()
            # Recompute readiness from the rule's evidence checklist and let
            # the shared promotion rule decide the status (same semantics as
            # the engine's refresh — guarded so a later review status like
            # submitted/approved is never overwritten by a survey re-run).
            required_keys = pm.rule.evidence_required if pm.rule_id and pm.rule else []
            if not required_keys:
                pct = 100
            else:
                present = sum(
                    1 for k in required_keys
                    if (merged.get(k) or {}).get("present")
                )
                pct = round(present / len(required_keys) * 100)
            pm.readiness_pct = pct
            pm.status = pm.status_for_readiness(pct) or pm.status
            pm.save(update_fields=[
                "evidence", "analysis_notes", "readiness_pct", "status",
                "updated_at",
            ])
            updated_pm_ids.add(pm.permit_id)
            PermitEvent.objects.get_or_create(
                permit=pm,
                event="SURVEY_EVIDENCE",
                defaults={
                    "detail": {
                        "source": "survey_approval",
                        "route_section": route_section,
                    }
                },
            )

    summary["permit_rows_updated"] = len(updated_pm_ids)
    logger.info(
        "Survey evidence applied for %s: %s features → %s permit rows",
        ftth_project_id, summary["survey_features"], len(updated_pm_ids),
    )
    return summary


def ensure_survey_evidence(ftth_project_id: str) -> dict:
    """Best-effort wrapper for the approval endpoints — never raises."""
    try:
        return apply_survey_evidence(ftth_project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Survey permit evidence failed for %s: %s", ftth_project_id, exc)
        return {"project_id": ftth_project_id, "error": str(exc)}
