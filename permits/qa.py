"""
Package QA (P19b) — deterministic full-package audit + optional AI paragraph.

Checks a generated permit package (LLD) against the live permit matrix +
LLD layers and reports gaps: missing drawings/TMPs/forms, count mismatches,
naming, HDD guard, empty files, grouping, and readiness. Works offline;
when PERMITS_LLM_* is configured an AI paragraph is appended (advisory only).

Never mutates PermitMatrix / PermitDocument / PermitSubmission. Cross-project
when project_id is None (summary across all projects with packages/rows).
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count

from .ai.provider import AI_DISCLAIMER, chat_completion
from .models import PermitDocument, PermitMatrix

# LLM is optional — QA is useful offline.


def _expected_counts(project_id: str) -> dict[str, Any]:
    """Derive expected artifact counts from LLD layers + permit matrix.

    Returns dict with keys: drawings, cross_sections, tmps, forms, hdd,
    reports, schedules, german_forms, by_surface_combos, etc. On any DB
    error we return zeros + gap note rather than raising.
    """
    expected: dict[str, Any] = {
        "drawings": 0,
        "cross_sections": 0,
        "tmps": 0,
        "forms": 0,
        "german_forms": 0,
        "hdd": 0,
        "reports": 5,  # all_reports() always returns 5 when LLD exists
        "schedules": 2,  # chamber + pdp within reports
        "gaps": [],
        "by_surface_combos": 0,
        "street_groups": 0,
        "road_groups": 0,
        "crossing_groups": 0,
        "lld_run_id": None,
        "permit_rows": 0,
        "lld_layers_with_features": [],
    }
    try:
        from .generators import data as _data

        # LLD run
        lld_run = _data.latest_lld_run_id(project_id)
        expected["lld_run_id"] = lld_run
        if not lld_run:
            expected["gaps"].append("No completed LLD run — package cannot be generated until LLD completes.")

        # Permit rows
        rows = list(PermitMatrix.objects.filter(project_id=project_id).select_related("rule"))
        expected["permit_rows"] = len(rows)

        # LLD package layers with features
        layers_with = []
        for layer in _data.PACKAGE_LAYERS:
            feats = _data.lld_layer_features(project_id, layer)
            if feats:
                layers_with.append(layer)
        expected["lld_layers_with_features"] = layers_with
        expected["drawings"] = len(layers_with)

        # Trench type/surface combos from final_trenches
        feats = _data.lld_layer_features(project_id, "final_trenches")
        combos: set[tuple[str, str]] = set()
        for f in feats:
            p = _data._props(f)
            ttype = (p.get("trench_type") or "Unknown").strip() or "Unknown"
            surf = (p.get("SURFACE") or "Unknown").strip() or "Unknown"
            combos.add((ttype, surf))
        expected["by_surface_combos"] = len(combos) if feats else 0
        expected["cross_sections"] = len(combos) if feats else 0
        expected["tmps"] = len(combos) if feats else 0

        # Street groups (forms) — one per (rule, permit_group) excluding UTILITY_REUSE
        groups: set[tuple[str, str]] = set()
        road_groups: set[tuple[str, str]] = set()
        for pm in rows:
            if not pm.rule or pm.rule.rule_id == "UTILITY_REUSE_001":
                continue
            key = (pm.rule.rule_id, (pm.permit_group or "").strip() or pm.route_section)
            groups.add(key)
            if pm.permit_type == "Road Opening":
                rk = ((pm.permit_group or "Unnamed").strip()[:40], (pm.municipality or "").strip())
                road_groups.add(rk)
        expected["street_groups"] = len(groups)
        expected["forms"] = len(groups)
        expected["road_groups"] = len(road_groups)
        expected["german_forms"] = len(road_groups)

        # HDD crossing groups
        crossing_rows = [pm for pm in rows if pm.rule and pm.rule.rule_id in ("RAILWAY_CROSSING_001", "WATERWAY_CROSSING_001")]
        crossing_groups: set[tuple[str, str]] = set()
        for pm in crossing_rows:
            key = (pm.rule.rule_id, (pm.permit_group or pm.route_section or "").strip() or pm.route_section)
            crossing_groups.add(key)
        expected["crossing_groups"] = len(crossing_groups)
        expected["hdd"] = len(crossing_groups)

        if not rows:
            expected["gaps"].append("No permit matrix rows — run permit analysis first.")
    except Exception as exc:  # pragma: no cover — never fail QA
        expected["gaps"].append(f"Could not derive expected counts: {type(exc).__name__}: {exc}")
    return expected


def _latest_lld_package(project_id: str) -> tuple[int | None, list[PermitDocument]]:
    """Return (version, docs) for the latest LLD package, or (None, [])."""
    # LLD zip doc is the anchor for LLD package versions
    latest = (
        PermitDocument.objects.filter(permit__project_id=project_id, name__startswith="permit_package_v")
        .order_by("-version")
        .values_list("version", flat=True)
        .first()
    )
    if latest is None:
        return None, []
    docs = list(PermitDocument.objects.filter(permit__project_id=project_id, version=latest).order_by("kind", "name"))
    return latest, docs


def _latest_hld_package(project_id: str) -> tuple[int | None, list[PermitDocument]]:
    latest = (
        PermitDocument.objects.filter(permit__project_id=project_id, name__startswith="HLD detailed")
        .order_by("-version")
        .values_list("version", flat=True)
        .first()
    )
    if latest is None:
        return None, []
    docs = list(PermitDocument.objects.filter(permit__project_id=project_id, version=latest).order_by("kind", "name"))
    return latest, docs


def _file_issues(docs: list[PermitDocument]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for d in docs:
        fname = (d.file.name or d.url or "").split("/")[-1] or d.name
        # Filename uniqueness
        if fname in seen:
            issues.append({"level": "error", "code": "duplicate_filename", "message": f"Duplicate filename: {fname}", "hint": "Regenerate — version should be unique per artifact."})
        seen[fname] = seen.get(fname, 0) + 1
        # Empty file check — file may not exist on disk in test env, so check size via file if available
        try:
            if d.file and hasattr(d.file, "size") and d.file.size == 0:
                issues.append({"level": "error", "code": "empty_file", "message": f"Empty file: {d.name} ({fname})", "hint": "Generator produced 0 bytes — check layer data / generator errors."})
        except Exception:
            pass
        # Kind sanity
        if d.kind not in ("DRAWING", "CROSS_SECTION", "TMP", "FORM", "REPORT", "SCHEDULE"):
            issues.append({"level": "warn", "code": "unknown_kind", "message": f"Unknown kind {d.kind} for {d.name}", "hint": "Check PermitDocument kind choices."})
    return issues


def qa_for_project(project_id: str, project_name: str | None = None) -> dict[str, Any]:
    """Deterministic QA for one project. Always returns a result dict.

    Levels: pass | warn | fail. Issues carry code/message/hint for the UI.
    """
    from ftth_hld.models import FtthProject

    # Resolve name if not supplied
    if project_name is None:
        try:
            proj = FtthProject.objects.filter(pk=project_id).first()
            project_name = proj.name if proj and proj.name else project_id
        except Exception:
            project_name = project_id

    expected = _expected_counts(project_id)
    version, docs = _latest_lld_package(project_id)
    hld_version, hld_docs = _latest_hld_package(project_id)

    # Count actuals by kind (LLD package version only)
    actual_by_kind: dict[str, int] = {}
    actual_hdd = 0
    actual_drawings = 0
    actual_cross = 0
    actual_tmp = 0
    actual_forms = 0
    actual_reports = 0
    actual_schedules = 0
    hdd_filenames: list[str] = []
    for d in docs:
        actual_by_kind[d.kind] = actual_by_kind.get(d.kind, 0) + 1
        fname = (d.file.name or "").lower()
        if "hdd_" in fname:
            actual_hdd += 1
            hdd_filenames.append(fname.split("/")[-1])
        if d.kind == "DRAWING" and "hdd_" not in fname:
            actual_drawings += 1
        if d.kind == "CROSS_SECTION":
            actual_cross += 1
        if d.kind == "TMP":
            actual_tmp += 1
        if d.kind == "FORM":
            actual_forms += 1
        if d.kind == "REPORT":
            actual_reports += 1
        if d.kind == "SCHEDULE":
            actual_schedules += 1

    issues: list[dict[str, Any]] = []

    # Gaps from expected derivation
    for gap in expected.get("gaps") or []:
        issues.append({"level": "warn", "code": "gap", "message": gap, "hint": "Complete the missing prerequisite, then re-run analysis and regenerate."})

    # Package existence
    if version is None:
        issues.append({"level": "error", "code": "no_lld_package", "message": "No LLD permit package generated yet.", "hint": "Generate the LLD package (POST /projects/<id>/package/) — QA audits the latest version."})
    else:
        # Manifest vs docs sanity: if docs is only the zip (1) but expected more, flag
        if len(docs) <= 1 and expected.get("permit_rows"):
            issues.append({"level": "error", "code": "package_incomplete", "message": f"Package v{version} has only {len(docs)} document(s) — expected {expected.get('forms', 0) + expected.get('drawings', 0) + 5} or more.", "hint": "Check package generation errors; the package may have failed mid-way."})

    # Count mismatches (only when we have a package to compare)
    if version is not None:
        # Drawings
        if expected["drawings"] and actual_drawings != expected["drawings"]:
            issues.append({"level": "warn" if actual_drawings else "error", "code": "drawing_count_mismatch", "message": f"Drawings: expected {expected['drawings']} (layers with features), got {actual_drawings}.", "hint": "Check that every PACKAGE_LAYER with features produced a drawing GeoJSON."})
        # Cross-sections
        if expected["cross_sections"] and actual_cross != expected["cross_sections"]:
            issues.append({"level": "warn", "code": "cross_section_mismatch", "message": f"Cross-sections: expected {expected['cross_sections']} (trench type\u00d7surface combos), got {actual_cross}.", "hint": "Each trench type/surface combo should have one SVG cross-section."})
        # TMPs
        if expected["tmps"] and actual_tmp != expected["tmps"]:
            issues.append({"level": "warn", "code": "tmp_count_mismatch", "message": f"TMPs: expected {expected['tmps']}, got {actual_tmp}.", "hint": "One TMP per trench type/surface; traffic rule TRAFFIC_001 drives this."})
        # Forms
        if expected["forms"] and actual_forms != expected["forms"]:
            # German Aufbruch forms are counted as FORM too, so forms includes both
            # application_forms + german_street_opening_form + hld_overview + street summary
            # The deterministic check is: at least street_groups forms should exist
            # (the extra 2—3 reports/forms are the overviews). So compare with tolerance.
            # We count FORM kind includes application forms + German + overviews.
            # The expected street_groups is the minimum.
            if actual_forms < expected["forms"]:
                issues.append({"level": "error", "code": "form_count_mismatch", "message": f"Forms: expected at least {expected['forms']} (one per street group), got {actual_forms}.", "hint": "Street-level grouping (permit_group) drives form count; check that every street group produced a form."})
            elif actual_forms > expected["forms"] + 4:
                issues.append({"level": "warn", "code": "form_count_extra", "message": f"Forms: expected {expected['forms']} street groups (+2 overviews), got {actual_forms} — extra forms present.", "hint": "Check for duplicate street slugs or stale grouping."})
        # HDD guard (P16)
        if expected["hdd"] and actual_hdd != expected["hdd"]:
            if expected["hdd"] > actual_hdd:
                issues.append({"level": "error", "code": "missing_hdd", "message": f"HDD drawings: expected {expected['hdd']} crossing groups, got {actual_hdd}.", "hint": "Each railway/waterway crossing group needs one hdd_* SVG; promotion of crossing evidence is gated on hdd_ prefix."})
            else:
                issues.append({"level": "warn", "code": "extra_hdd", "message": f"HDD drawings: expected {expected['hdd']}, got {actual_hdd} — extra HDD profiles.", "hint": "HDD should only fire where RAILWAY_CROSSING_001 / WATERWAY_CROSSING_001 rows exist."})
        if not expected["hdd"] and actual_hdd:
            issues.append({"level": "warn", "code": "unexpected_hdd", "message": f"HDD drawings present ({actual_hdd}) but no crossing rows exist.", "hint": "No railway/waterway intersections in this project area — HDD should be 0 (verified on Berlin/Mariendorf)."})

    # File-level issues
    issues.extend(_file_issues(docs))

    # Readiness check: any street group not READY when its form exists?
    try:
        rows = list(PermitMatrix.objects.filter(project_id=project_id).select_related("rule"))
        not_ready_groups: set[tuple[str, str]] = set()
        for pm in rows:
            if pm.status not in ("ready", "approved", "submitted", "under_review", "closed") and pm.rule and pm.rule.rule_id != "UTILITY_REUSE_001":
                # Only flag if the group has a form (i.e. it should be submittable)
                key = (pm.rule.rule_id, (pm.permit_group or "").strip() or pm.route_section)
                not_ready_groups.add(key)
        if not_ready_groups and version is not None:
            # If package exists but groups are not ready, the TMP/drawings may not have promoted evidence
            sample = list(not_ready_groups)[:3]
            issues.append({"level": "warn", "code": "groups_not_ready", "message": f"{len(not_ready_groups)} street group(s) still not Ready (e.g. {sample[0][0]} on {sample[0][1][:24]}).", "hint": "Check completeness (missing evidence keys) — the package's TMP/drawings auto-promote evidence on generation."})
    except Exception:
        pass

    # Naming sanity: form filenames should be slugified, not empty
    for d in docs:
        if d.kind == "FORM" and len((d.file.name or "")) < 4:
            issues.append({"level": "warn", "code": "form_naming", "message": f"Form has odd filename: {d.name} -> {d.file.name or 'no file'}", "hint": "Form filenames are slugified from permit type + street; check grouping."})
            break

    # Overall level
    has_error = any(i["level"] == "error" for i in issues)
    has_warn = any(i["level"] == "warn" for i in issues)
    if has_error:
        overall = "fail"
    elif has_warn:
        overall = "warn"
    else:
        overall = "pass"

    summary = f"QA {overall.upper()} \u2014 {len(issues)} issue(s) \u2014 LLD package v{version or 'none'} on {project_name} ({len(docs)} docs: {actual_by_kind})."

    # Per-project counts for the overview card
    result: dict[str, Any] = {
        "project_id": project_id,
        "project_name": project_name,
        "level": overall,
        "summary": summary,
        "issues": issues,
        "issue_count": len(issues),
        "error_count": sum(1 for i in issues if i["level"] == "error"),
        "warn_count": sum(1 for i in issues if i["level"] == "warn"),
        "lld_run_id": expected.get("lld_run_id"),
        "lld_package_version": version,
        "hld_package_version": hld_version,
        "total_docs": len(docs),
        "total_hld_docs": len(hld_docs),
        "actual_by_kind": actual_by_kind,
        "actual_hdd": actual_hdd,
        "hdd_filenames": hdd_filenames[:10],
        "expected": expected,
        "is_ai_generated": False,
        "disclaimer": "Deterministic package QA — rule + package derived, not a model. AI paragraph is advisory only.",
    }

    # Optional AI paragraph (advisory — never changes level/counts)
    # Keep prompt short; failures fall back to deterministic.
    try:
        if issues:
            issue_lines = "\n".join(f"- [{i['level']}] {i['code']}: {i['message']}" for i in issues[:8])
        else:
            issue_lines = "No issues — package looks complete."
        ai_text = chat_completion(
            "You are a QA assistant for fibre permit packages. You summarize deterministic QA findings in one short paragraph (2-3 sentences) for a planner. Do not invent missing documents; only summarize the findings provided. Be concrete about the next fix.",
            f"Project: {project_name} ({project_id})\nPackage v{version or 'none'} — level {overall}, {len(docs)} docs, kinds {actual_by_kind}\nExpected: drawings {expected.get('drawings')} / cross {expected.get('cross_sections')} / tmp {expected.get('tmps')} / forms {expected.get('forms')} / hdd {expected.get('hdd')}\nFindings:\n{issue_lines}\n\nWrite one paragraph.",
            max_tokens=320,
            temperature=0.2,
        )
        if ai_text:
            result["ai_paragraph"] = ai_text.strip()
            result["is_ai_generated"] = True
            result["disclaimer"] = AI_DISCLAIMER
    except Exception:
        pass

    return result


def qa_summary(project_id: str | None = None) -> dict[str, Any]:
    """Cross-project QA summary (or single-project when filtered).

    Returns counts per level + the worst findings for quick display.
    """
    from ftth_hld.models import FtthProject

    if project_id:
        one = qa_for_project(project_id)
        return {
            "project_id": project_id,
            "total_projects": 1,
            "by_level": {one["level"]: 1},
            "total_issues": one["issue_count"],
            "total_errors": one["error_count"],
            "total_warns": one["warn_count"],
            "projects": [one],
            "buckets": ["pass", "warn", "fail"],
        }

    # Cross-project: every project that has either permit rows or package docs
    try:
        project_ids = set(PermitMatrix.objects.values_list("project_id", flat=True).distinct())
        doc_pids = set(PermitDocument.objects.values_list("permit__project_id", flat=True).distinct())
        all_pids = project_ids | doc_pids
        # Also include projects with HLD but no permits/docs yet (so QA can say "no package")
        try:
            hld_pids = set(FtthProject.objects.values_list("project_id", flat=True))
            all_pids |= hld_pids
        except Exception:
            pass
    except Exception:
        all_pids = set()

    projects: list[dict[str, Any]] = []
    by_level: dict[str, int] = {}
    total_issues = total_errors = total_warns = 0
    for pid in sorted(all_pids):
        try:
            one = qa_for_project(str(pid))
        except Exception as exc:  # pragma: no cover
            one = {"project_id": str(pid), "project_name": str(pid), "level": "warn", "summary": f"QA skipped: {exc}", "issues": [{"level": "warn", "code": "qa_skipped", "message": str(exc), "hint": ""}], "issue_count": 1, "error_count": 0, "warn_count": 1, "is_ai_generated": False}
        projects.append(one)
        by_level[one["level"]] = by_level.get(one["level"], 0) + 1
        total_issues += one.get("issue_count", 0)
        total_errors += one.get("error_count", 0)
        total_warns += one.get("warn_count", 0)

    # Sort worst first
    rank = {"fail": 3, "warn": 2, "pass": 1}
    projects.sort(key=lambda r: (-rank.get(r["level"], 0), -r.get("issue_count", 0)))

    return {
        "project_id": None,
        "total_projects": len(projects),
        "by_level": by_level,
        "total_issues": total_issues,
        "total_errors": total_errors,
        "total_warns": total_warns,
        "projects": projects[:100],
        "buckets": ["pass", "warn", "fail"],
    }
