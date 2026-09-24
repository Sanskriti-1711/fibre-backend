"""Drafting helpers: cover text, narrative descriptions, evidence summaries.

Every draft is:
- marked `is_ai_generated` + disclaimer,
- stored on PermitDraft (never on PermitMatrix),
- rendered with an explicit AI badge in the UI,
- never used to flip status/readiness/required/blocks_construction.

When no LLM is configured the functions return a deterministic template so
the product remains useful and tests stay deterministic.
"""

from __future__ import annotations

import html
from typing import Any

from .provider import AI_DISCLAIMER, chat_completion


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def draft_cover_text(
    project_name: str,
    project_id: str,
    permit_type: str,
    permit_group: str,
    authority_name: str,
    municipality: str,
    section_count: int,
    trench_summary: str = "",
) -> dict[str, Any]:
    """Draft an authority cover letter intro for a street-level permit."""
    deterministic = (
        f"Application for {permit_type} — {permit_group or 'this street section'} "
        f"in {municipality or 'the project area'}.\n\n"
        f"Project {project_name} ({project_id}) comprises {section_count} civil section(s) on this street. "
        f"Authority: {authority_name or 'to be confirmed'}. "
        f"{trench_summary or 'Trench dimensions and surface treatment are per the attached drawings and cross-sections.'}"
    )
    system = (
        "You draft a covering paragraph for a German fibre civil-works permit application "
        "(Aufbruchgenehmigung / street works). Write a professional, concise paragraph in English "
        "(the authority may translate). Mention the project, the street, the scope in metres if given, "
        "and that drawings/TMP/reinstatement are attached. Do not invent dates, fees, or authority commitments."
    )
    user = (
        f"Project: {project_name} ({project_id})\n"
        f"Permit: {permit_type} — {permit_group or 'this street section'}\n"
        f"Authority: {authority_name or 'to be confirmed'}\n"
        f"Municipality: {municipality or 'pending'}\n"
        f"Sections on this street: {section_count}\n"
        f"Trench summary: {trench_summary or 'per drawings/cross-sections'}\n\n"
        "Write one polished paragraph plus a one-line document checklist (drawings, cross-section, TMP, reinstatement, utility statement)."
    )
    ai_text = chat_completion(system, user, max_tokens=520, temperature=0.25)
    if ai_text:
        return {
            "text": ai_text.strip(),
            "deterministic_fallback": deterministic,
            "is_ai_generated": True,
            "disclaimer": AI_DISCLAIMER,
        }
    return {"text": deterministic, "is_ai_generated": False, "disclaimer": "Template — deterministic, no LLM used."}


def draft_narrative(
    project_name: str,
    permit_type: str,
    permit_group: str,
    evidence: dict[str, Any] | None,
    trench_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Draft a short narrative description for a permit (form field)."""
    present = [k for k, v in (evidence or {}).items() if isinstance(v, dict) and v.get("present")]
    deterministic = (
        f"Works for {permit_type} on {permit_group or 'the identified section'} as part of project {project_name}. "
        f"Evidence on file: {', '.join(present) or 'pending'}. "
        f"{('Trench lengths by surface: ' + ', '.join(f'{k} {v} m' for k, v in (trench_stats or {}).get('by_surface', {}).items())) if trench_stats else ''}"
    ).strip()
    system = (
        "You write a short factual narrative for a fibre permit application (3–5 sentences). "
        "Stay factual — use only the trench/evidence details provided. Do not invent measurements, dates, or authority outcomes."
    )
    user = (
        f"Project: {project_name}\nPermit: {permit_type} — {permit_group or 'section'}\n"
        f"Evidence present: {', '.join(present) or 'none yet'}\n"
        f"Trench stats: {trench_stats or 'not available'}\n\n"
        "Draft the narrative."
    )
    ai_text = chat_completion(system, user, max_tokens=500, temperature=0.25)
    if ai_text:
        return {"text": ai_text.strip(), "deterministic_fallback": deterministic, "is_ai_generated": True, "disclaimer": AI_DISCLAIMER}
    return {"text": deterministic, "is_ai_generated": False, "disclaimer": "Template — deterministic, no LLM used."}


def extract_requirements_from_text(raw_text: str) -> dict[str, Any]:
    """Extract authority requirements from pasted authority guidance text.

    Deterministic fallback: keyword scan. LLM (when available) returns cleaner
    JSON that the caller validates before use.
    """
    text = (raw_text or "").strip()
    if not text:
        return {"items": [], "is_ai_generated": False, "disclaimer": "No input text provided.", "raw_excerpt": ""}

    # Deterministic keyword scan (always returned as `heuristic_items`).
    keywords = {
        "drawing": "Route drawing at required scale",
        "querschnitt": "Trench cross-section (Querschnitt)",
        "cross-section": "Trench cross-section",
        "tmp": "Traffic management plan (TMP)",
        "verkehrs": "Traffic management / signing plan",
        "reinstatement": "Reinstatement undertaking / Gewährleistung",
        "wiederherstellung": "Wiederherstellung / reinstatement",
        "haftpflicht": "Liability / insurance certificate",
        "aufbruch": "Street opening approval (Aufbruchgenehmigung)",
        "sondernutzung": "Special use permit (Sondernutzung)",
        "tkg": "Telecom right-of-way (TKG)",
        "vibration": "Vibration / settlement monitoring",
        "hdd": "HDD profile / bore plan",
        "baum": "Tree / root protection measures",
        "naturschutz": "Environmental / nature protection screening",
    }
    low = text.lower()
    heuristic = [label for kw, label in keywords.items() if kw in low]
    # De-duplicate while preserving order.
    seen: set[str] = set()
    heuristic = [x for x in heuristic if not (x in seen or seen.add(x))]

    # Try LLM extraction (structured, validated).
    system = (
        "You extract authority permit requirements from pasted guidance text (German or English). "
        "Return a JSON array of short requirement phrases (max 10 items), e.g. "
        "[\"Route drawing 1:500\", \"Traffic management plan\", \"Reinstatement guarantee\"]. "
        "Do not add requirements not present in the text. Reply with JSON only."
    )
    user = f"Authority guidance text:\n\n{text[:8000]}\n\nExtract the requirements as a JSON array."
    ai_text = chat_completion(system, user, max_tokens=650, temperature=0.1)
    llm_items: list[str] | None = None
    if ai_text:
        import json as _json

        # Extract the first JSON array from the response.
        s = ai_text.strip()
        start = s.find("[")
        end = s.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                parsed = _json.loads(s[start : end + 1])
                if isinstance(parsed, list):
                    cleaned = [str(x).strip() for x in parsed if str(x).strip()]
                    if cleaned:
                        llm_items = cleaned[:12]
            except Exception:
                llm_items = None

    if llm_items is not None:
        return {
            "items": llm_items,
            "heuristic_items": heuristic,
            "is_ai_generated": True,
            "disclaimer": AI_DISCLAIMER,
            "raw_excerpt": text[:1200],
        }
    return {
        "items": heuristic,
        "is_ai_generated": False,
        "disclaimer": "Heuristic keyword scan — paste authority guidance and, where configured, the AI extraction will refine this.",
        "raw_excerpt": text[:1200],
    }
