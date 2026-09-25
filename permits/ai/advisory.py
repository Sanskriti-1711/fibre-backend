"""Advisory logic: completeness checks, risk explainer, timeline, authority requirements.

All four are deterministic-first: each has a rule-derived baseline that is
always returned. Where an LLM is available the text is enriched, but the
structured findings (blocking/missing/level/days) come from the baseline so
a hallucinated sentence can never flip readiness or blocking.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

from .provider import AI_DISCLAIMER, chat_completion

# Bezirk contact/fee helpers — optional so advisory works even without the
# bezirke module (tests / older checkouts).
try:
    from ..bezirke import bezirk_fee_note as _bezirk_fee_note  # type: ignore
    from ..bezirke import bezirk_form_hint as _bezirk_form_hint  # type: ignore
    from ..bezirke import get_bezirk as _get_bezirk  # type: ignore
    _HAS_BEZIRKE = True
except ImportError:  # pragma: no cover
    _HAS_BEZIRKE = False
    _bezirk_fee_note = lambda *_a, **_kw: ""  # type: ignore
    _bezirk_form_hint = lambda *_a, **_kw: ""  # type: ignore
    _get_bezirk = lambda *_a, **_kw: None  # type: ignore


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


# ── Authority requirement knowledge (deterministic baseline) ──────────────

# Per-permit-type checklist that the forms already satisfy structurally.
# Kept separate from RuleDef.evidence_required so this can carry the
# authority-facing wording without polluting the matrix.
AUTHORITY_CHECKLISTS: dict[str, list[str]] = {
    "Road Opening": [
        "Route drawing (scale 1:500) with affected streets & chainage",
        "Trench cross-section & reinstatement spec (depth/width/surface)",
        "Traffic management plan (lane impact, diversions, pedestrian measures)",
        "Work window & duration estimate",
        "Utility conflict statement (brownfield reuse vs new trench)",
        "Reinstatement & defect liability undertaking",
        "Third-party consents where private land is affected",
    ],
    "Railway Crossing": [
        "Crossing coordinate & chainage along the railway corridor",
        "HDD profile drawing (entry/exit, depth under track, radius)",
        "Rail operator approval letter / crossing agreement",
        "Construction method statement (HDD) & risk assessment",
        "Reinstatement & monitoring plan for track formation",
    ],
    "Waterway Crossing": [
        "Crossing coordinate & waterway name / authority reference",
        "Crossing drawing (depth under bed, method)",
        "Water authority consent / environmental screening",
    ],
    "Environmental Review": [
        "Zone type & boundary (habitat / protected area)",
        "Impact assessment (habitats, species, mitigation)",
        "Authority screening decision (where required)",
    ],
    "Tree Protection": [
        "Tree inventory reference (OSM id / survey tag)",
        "Root protection zone drawing & method statement",
        "Arboricultural supervision arrangement",
    ],
    "Traffic Management": [
        "Traffic management plan (impact tier, lane closures, diversions)",
        "Reinstatement table by surface",
        "Signing & guarding schedule",
    ],
    "Utility Coexistence": [
        "Reuse source & capacity check (brownfield duct/cable occupancy)",
        "Clearance & coexistence method where shared trench is used",
    ],
}


def authority_requirements(
    permit_type: str,
    authority_name: str = "",
    municipality: str = "",
    authority_code: str = "",
) -> dict[str, Any]:
    """Deterministic checklist for a permit type + authority.

    When a Berlin Bezirk can be resolved from ``municipality`` (via
    ``permits.bezirke``), the tail carries the Bezirk-specific office/form/
    fee note so the Copilot checklist is municipality-aware (P17).
    """
    items = AUTHORITY_CHECKLISTS.get(permit_type) or [
        "Route drawing & schedule of works",
        "Construction method & reinstatement spec",
        "Authority-specific conditions (check local guidance)",
    ]
    # Bezirk-aware tail (P17) — falls back to the generic Berlin note.
    tail: list[str] = []
    if _HAS_BEZIRKE:
        try:
            hint = _bezirk_form_hint(municipality or "", permit_type, authority_code or authority_name or "")
            # Only surface the Bezirk hint when it resolved a real contact;
            # otherwise keep the old Berlin tail for generic Berlin authorities.
            if municipality and _get_bezirk(municipality):
                tail = [hint]
            else:
                low = (authority_name or "").lower()
                if "bezirk" in low or "senmvku" in low or "berlin" in low:
                    tail = [hint or "Berlin: confirm SenMVKU vs Bezirksamt responsibility by road class (fclass mapping)"]
        except Exception:
            tail = []
    else:
        low = (authority_name or "").lower()
        if "bezirk" in low or "senmvku" in low or "berlin" in low:
            tail = ["Berlin: confirm SenMVKU vs Bezirksamt responsibility by road class (fclass mapping)"]
    return {
        "permit_type": permit_type,
        "authority": authority_name or None,
        "municipality": municipality or None,
        "items": items + tail,
        "source": "deterministic checklists (no LLM)",
        "is_ai_generated": False,
        "disclaimer": "Check the authority's current guidance — local requirements can change.",
    }


def enrich_requirements_with_ai(
    permit_type: str,
    authority_name: str,
    project_name: str,
    evidence: dict[str, Any] | None,
    municipality: str = "",
    authority_code: str = "",
) -> dict[str, Any]:
    """Optionally enrich the checklist with an LLM paragraph (advisory only)."""
    base = authority_requirements(permit_type, authority_name, municipality, authority_code)
    prompt_user = (
        f"Permit type: {permit_type}\n"
        f"Authority: {authority_name or 'unspecified'}\n"
        f"Project: {project_name}\n"
        f"Evidence keys present: {', '.join(sorted((evidence or {}).keys())) or 'none'}\n\n"
        "Write 3–5 short bullet points expanding what a planner should double-check "
        "with this authority before submitting. Do not invent fees or deadlines; "
        "say 'confirm with the authority' where they vary."
    )
    system = (
        "You are a permit assistant for fibre civil works in Germany. "
        "You draft helpful checklists but you do not make legal decisions. "
        "Be concrete and cite the kind of document the authority typically asks for."
    )
    text = chat_completion(system, prompt_user, max_tokens=500, temperature=0.2)
    if text:
        base = {
            **base,
            "ai_notes": text.strip(),
            "is_ai_generated": True,
            "disclaimer": AI_DISCLAIMER,
        }
    return base


# ── Completeness / readiness explainer ──────────────────────────────────

@dataclass(frozen=True)
class CompletenessInput:
    permit_type: str
    status: str
    readiness_pct: int
    required_keys: list[str]
    evidence: dict[str, Any]
    municipality: str
    authority_name: str
    permit_group: str


def completeness_check(inp: CompletenessInput) -> dict[str, Any]:
    """Explain in plain language why a row is / isn't READY.

    `blocking` is deterministic (evidence_required that is not present).
    `summary` is deterministic prose; an AI paragraph is appended where available.
    """
    missing = [k for k in (inp.required_keys or []) if not (inp.evidence.get(k) or {}).get("present")]
    present = [k for k in (inp.required_keys or []) if (inp.evidence.get(k) or {}).get("present")]
    blocking = missing  # same set — naming matters for the UI
    if not missing and inp.readiness_pct >= 100:
        verdict = "Ready to submit (all required evidence present)."
        level = "ready"
    elif not missing:
        verdict = "All checklist items present — the row will promote to Ready on next refresh."
        level = "ready"
    elif inp.readiness_pct == 0:
        verdict = f"Not started — {len(missing)} of {len(inp.required_keys)} required item(s) still missing."
        level = "blocked"
    else:
        verdict = f"{len(present)}/{len(inp.required_keys)} items present — {len(missing)} still needed before it can be Ready."
        level = "partial"

    summary = (
        f"{_e(inp.permit_type)} ({_e(inp.permit_group) or 'ungrouped'}) — {verdict} "
        f"Status is {inp.status} at {inp.readiness_pct}% readiness."
    )
    if not inp.municipality:
        summary += " Municipality is still blank — it auto-fills from the OSM admin boundary when available."
    if not inp.authority_name:
        summary += " Authority is unresolved — check road class / reference layers or assign manually."

    # Deterministic next-steps the UI can render as a checklist.
    next_steps = []
    for k in missing:
        next_steps.append(f"Add evidence for '{k.replace('_', ' ')}' (see checklist below).")
    if not inp.municipality:
        next_steps.append("Confirm municipality (Gemeinde / Bezirk) for this street.")
    if not next_steps and level == "ready":
        next_steps.append("Generate or refresh the permit package — its TMP/drawings satisfy document evidence.")

    result: dict[str, Any] = {
        "permit_type": inp.permit_type,
        "status": inp.status,
        "readiness_pct": inp.readiness_pct,
        "required_keys": inp.required_keys,
        "present": present,
        "missing": missing,
        "blocking": blocking,
        "level": level,  # ready | partial | blocked
        "summary": summary,
        "next_steps": next_steps,
        "is_ai_generated": False,
        "disclaimer": "Deterministic — derived from rule.evidence_required vs evidence.present.",
    }

    # Optional AI paragraph (advisory only — never replaces blocking).
    ai_text = chat_completion(
        "You explain permit readiness in plain language for a planner. Do not change the checklist items; just explain them helpfully.",
        (
            f"Permit: {inp.permit_type} on {inp.permit_group or 'this section'} — "
            f"status {inp.status}, {inp.readiness_pct}% ready.\n"
            f"Required evidence: {', '.join(inp.required_keys) or 'none'}\n"
            f"Present: {', '.join(present) or 'none'}\n"
            f"Missing / blocking: {', '.join(missing) or 'none'}\n"
            f"Authority: {inp.authority_name or 'unresolved'}, municipality: {inp.municipality or 'blank'}\n\n"
            "Write 2–3 short sentences explaining what is holding this back and the most useful next action. "
            "Do not invent evidence that is not listed."
        ),
        max_tokens=350,
        temperature=0.2,
    )
    if ai_text:
        result["ai_explanation"] = ai_text.strip()
        result["is_ai_generated"] = True
        result["disclaimer"] = AI_DISCLAIMER
    return result


# ── Risk explainer ───────────────────────────────────────────────────────

# Deterministic risk signals (rule metadata only — not a model).
RISK_BY_PERMIT_TYPE: dict[str, dict[str, Any]] = {
    "Railway Crossing": {"level": "high", "reasons": ["HDD design + rail operator approval required", "High construction-blocking impact"]},
    "Waterway Crossing": {"level": "high", "reasons": ["Water authority consent + crossing design"]},
    "Environmental Review": {"level": "medium", "reasons": ["Habitat / protected-area screening can add weeks"]},
    "Tree Protection": {"level": "medium", "reasons": ["Root protection zone & arboricultural supervision"]},
    "Road Opening": {"level": "medium", "reasons": ["Road authority + traffic management coordination"]},
    "Traffic Management": {"level": "low", "reasons": ["Document-driven — TMP + reinstatement table"]},
    "Utility Coexistence": {"level": "low", "reasons": ["Informational — capacity checks & coexistence notes"]},
}


def risk_for_row(
    permit_type: str,
    status: str,
    readiness_pct: int,
    required: bool,
    blocks_construction: bool,
    permit_group: str = "",
) -> dict[str, Any]:
    """Deterministic risk level + reasons, optionally enriched by an LLM sentence.

    `level` is one of high|medium|low, driven by permit type and blocking.
    """
    base = RISK_BY_PERMIT_TYPE.get(permit_type, {"level": "medium", "reasons": ["Check authority-specific conditions"]})
    level: str = str(base.get("level") or "medium")
    reasons: list[str] = list(base.get("reasons") or [])
    if blocks_construction and required:
        level = "high"
        reasons = ["Blocks construction until approved"] + reasons
    if status in ("rejected", "evidence_required") and level == "low":
        level = "medium"
    if status == "approved":
        level = "low"
        reasons = ["Approved — monitor expiry / conditions"] + [r for r in reasons if "Approved" not in r]

    summary = (
        f"{_e(permit_type)} — {level.upper()} risk: "
        + "; ".join(reasons[:3])
        + f" (status {status}, {readiness_pct}%)."
    )
    out: dict[str, Any] = {
        "permit_type": permit_type,
        "level": level,
        "reasons": reasons,
        "summary": summary,
        "is_ai_generated": False,
        "disclaimer": "Heuristic — rule-based, not a model. Treat as planning guidance.",
    }
    ai_text = chat_completion(
        "You explain construction/permit risk for fibre civil works in one short paragraph. Be specific about what causes delay and what de-risks it. Do not promise timelines.",
        (
            f"Permit: {permit_type} ({permit_group or 'street pending'}), status {status}, "
            f"required={required}, blocks_construction={blocks_construction}, readiness {readiness_pct}%. "
            f"Deterministic reasons: {'; '.join(reasons)}\n\n"
            "Write one concise paragraph (2–3 sentences) for a planner."
        ),
        max_tokens=280,
        temperature=0.25,
    )
    if ai_text:
        out["ai_paragraph"] = ai_text.strip()
        out["is_ai_generated"] = True
        out["disclaimer"] = AI_DISCLAIMER
    return out


def project_risk_overview(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate risk across a project's permit rows.

    `rows` are PermitMatrix-like dicts with at least permit_type/status/blocks_construction.
    """
    counts = {"high": 0, "medium": 0, "low": 0}
    top_reasons: list[str] = []
    for r in rows:
        rr = risk_for_row(
            r.get("permit_type") or "Unknown",
            r.get("status") or "identified",
            int(r.get("readiness_pct") or 0),
            bool(r.get("required")),
            bool(r.get("blocks_construction")),
        )
        counts[rr["level"]] = counts.get(rr["level"], 0) + 1
        for reason in rr["reasons"]:
            if reason not in top_reasons:
                top_reasons.append(reason)
    # Project-level level is the worst present.
    level = "low"
    if counts.get("high"):
        level = "high"
    elif counts.get("medium"):
        level = "medium"
    summary = (
        f"Project risk: {level.upper()} — "
        f"{counts.get('high', 0)} high / {counts.get('medium', 0)} medium / {counts.get('low', 0)} low. "
        + ("Top drivers: " + "; ".join(top_reasons[:3]) + "." if top_reasons else "")
    )
    return {"level": level, "counts": counts, "top_reasons": top_reasons[:6], "summary": summary, "is_ai_generated": False}


# ── Timeline estimator ───────────────────────────────────────────────────

# Deterministic per-type review windows (working days). These are planning
# estimates, not authority SLAs — the UI labels them as such.
TIMELINE_DAYS: dict[str, tuple[int, int]] = {
    "Road Opening": (10, 25),
    "Traffic Management": (5, 15),
    "Railway Crossing": (25, 60),
    "Waterway Crossing": (20, 45),
    "Environmental Review": (15, 40),
    "Tree Protection": (10, 25),
    "Utility Coexistence": (3, 10),
}


def estimate_timeline(
    permit_types: list[str],
    statuses: list[str] | None = None,
) -> dict[str, Any]:
    """Deterministic range estimate across a set of permit types.

    The project estimate is the max of the per-type maxima (the critical path
    is the slowest authority), plus a small sequencing buffer.
    """
    statuses = statuses or []
    # Approved/closed rows do not add review time.
    active_types = [t for t, s in zip(permit_types, permit_types) if True]  # keep shape
    # If caller passes statuses aligned with types, drop completed ones.
    if statuses and len(statuses) == len(permit_types):
        active_types = [t for t, s in zip(permit_types, statuses) if s not in ("approved", "closed", "not_required")]

    if not active_types:
        return {
            "min_days": 0,
            "max_days": 0,
            "critical_types": [],
            "assumptions": ["No active permits — ready to construct."],
            "is_ai_generated": False,
            "disclaimer": "Deterministic — max of per-type review windows; not an authority SLA.",
        }

    per_type = {t: TIMELINE_DAYS.get(t, (10, 25)) for t in set(active_types)}
    # Critical path = slowest authority.
    critical = sorted(set(active_types), key=lambda t: per_type[t][1], reverse=True)[:2]
    max_days = max(v[1] for v in per_type.values())
    min_days = max(v[0] for v in per_type.values())
    # Buffer when several authorities are involved.
    if len(set(active_types)) > 2:
        max_days += 5
        min_days += 2

    assumptions = [
        "Working days, single submission per street group.",
        "Assumes documents are complete (readiness 100% → Ready).",
        "Does not include authority-requested corrections or re-submission time.",
        "Critical path is the slowest permit type: " + ", ".join(critical) + ".",
    ]
    out: dict[str, Any] = {
        "min_days": min_days,
        "max_days": max_days,
        "per_type": {k: {"min": v[0], "max": v[1]} for k, v in sorted(per_type.items())},
        "critical_types": critical,
        "assumptions": assumptions,
        "is_ai_generated": False,
        "disclaimer": "Deterministic planning estimate — confirm with each authority's current lead times.",
    }
    ai_text = chat_completion(
        "You write a brief timeline note for fibre permit approvals. You give planning context but you do not promise authority SLAs. Be concrete about what stretches the timeline.",
        (
            f"Active permit types: {', '.join(sorted(set(active_types)))}; "
            f"deterministic estimate {min_days}–{max_days} working days, critical: {', '.join(critical)}. "
            "Write one short paragraph (2 sentences) on what to do to stay at the low end."
        ),
        max_tokens=260,
        temperature=0.25,
    )
    if ai_text:
        out["ai_note"] = ai_text.strip()
        out["is_ai_generated"] = True
        out["disclaimer"] = AI_DISCLAIMER
    return out
