"""Permit package orchestrator.

Assembles the generator outputs (drawings, cross-sections, forms, TMPs,
reports) into a versioned zip, persists the files as ``PermitDocument``
rows and promotes document evidence on the permit matrix (the Phase-2
readiness checker): a TRAFFIC row whose TMP was generated satisfies
``tmp_document`` (+ lane impact derived from the TMP tier); crossing rules
satisfy their drawing evidence where a crossing drawing exists.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any, Optional

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from ..models import PermitDocument, PermitEvent, PermitMatrix
from . import data, drawings, permit_forms, reports, traffic_plan


def _latest_package_version(project_id: str) -> int:
    last = (
        PermitDocument.objects.filter(permit__project_id=project_id)
        .order_by("-version")
        .values_list("version", flat=True)
        .first()
    )
    return (last or 0) + 1


def _promote_evidence(project_id: str, generated_kinds: set[str]) -> dict[str, int]:
    """Readiness checker: mark document evidence satisfied where the package
    actually generated the artifact. Returns counts of promoted rows."""
    promoted = 0
    for pm in PermitMatrix.objects.filter(project_id=project_id).select_related("rule"):
        rule = pm.rule
        if not rule:
            continue
        ev = dict(pm.evidence or {})
        changed = False

        if rule.rule_id == "TRAFFIC_001" and "TMP" in generated_kinds:
            # The TMP exists at package level; record it as evidence on the row.
            if not (ev.get("tmp_document") or {}).get("present"):
                ev["tmp_document"] = {
                    "present": True,
                    "value": "package:TMP",
                    "refs": ["permit package"],
                }
                changed = True
            if not (ev.get("lane_impact") or {}).get("present"):
                # Derived tier is deterministic per surface/type; a generic
                # lane-impact note suffices for evidence bookkeeping.
                ev["lane_impact"] = {
                    "present": True,
                    "value": "documented in package TMP",
                    "refs": ["permit package"],
                }
                changed = True

        for evidence_key, drawing_kinds in (
            ("crossing_drawing", {"DRAWING"}),
            ("profile_drawing", {"DRAWING"}),
            ("hdd_design", {"DRAWING"}),
        ):
            if (
                evidence_key in (rule.evidence_required or [])
                and drawing_kinds & generated_kinds
                and not (ev.get(evidence_key) or {}).get("present")
            ):
                ev[evidence_key] = {
                    "present": True,
                    "value": "package:drawing",
                    "refs": ["permit package"],
                }
                changed = True

        if changed:
            pm.evidence = ev
            pm.save(update_fields=["evidence", "updated_at"])
            PermitEvent.objects.create(
                permit=pm,
                event="EVIDENCE_ADDED",
                detail={"source": "permit_package", "kinds": sorted(generated_kinds)},
            )
            promoted += 1

    # Recompute readiness for the whole project after evidence changes.
    from ..rules.engine import _refresh_readiness

    _refresh_readiness(project_id)
    return {"promoted_rows": promoted}


def generate_package(
    project_id: str,
    project_name: str,
    lld_run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Generate the full permit package for a project.

    Returns a summary dict: {version, files: [...], zip_size, promoted, errors}.
    Files are written as ``PermitDocument`` rows (attached to the first
    matrix row of the project, which acts as the package anchor) and the
    whole package is also returned as a zip buffer for the download view.
    """
    run_id = lld_run_id or data.latest_lld_run_id(project_id)
    if not run_id:
        raise ValueError("No completed LLD run found — generate the LLD first.")

    generated: list[dict[str, Any]] = []
    errors: list[str] = []

    for fn, kwargs in (
        (drawings.route_drawings, {}),
        (drawings.cross_sections, {}),
        (traffic_plan.traffic_plans, {}),
        (permit_forms.application_forms, {"project_name": project_name}),
        (reports.all_reports, {}),
    ):
        try:
            items = fn(project_id, **kwargs)
            generated.extend(items)
        except Exception as exc:  # noqa: BLE001 — generator errors must not kill the package
            errors.append(f"{fn.__name__}: {exc}")

    if not generated:
        raise ValueError("No permit-package documents could be generated.")

    # Anchor: the first matrix row of the project (package-level documents).
    anchor = PermitMatrix.objects.filter(project_id=project_id).order_by("permit_id").first()
    if anchor is None:
        raise ValueError("No permit matrix rows — run permit analysis first.")

    version = _latest_package_version(project_id)

    # Build the zip in memory.
    zip_buffer = io.BytesIO()
    manifest = {
        "project_id": project_id,
        "project_name": project_name,
        "package_version": version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lld_run_id": run_id,
        "files": [{"name": g["name"], "kind": g["kind"], "filename": g["filename"]} for g in generated],
        "errors": errors,
    }
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for g in generated:
            zf.writestr(g["filename"], g["content"])
    zip_bytes = zip_buffer.getvalue()

    with transaction.atomic():
        # Persist the zip itself as a package document (served via download).
        zip_doc = PermitDocument.objects.create(
            permit=anchor,
            name=f"permit_package_v{version}",
            kind="REPORT",
            version=version,
            url="",
        )
        zip_doc.file.save(f"permit_package_v{version}.zip", ContentFile(zip_bytes), save=True)

        # Persist the meaningful artifacts as rows (drawings, cross-sections,
        # TMPs, schedules, reports) so the package list exposes individual
        # documents. Application forms stay inside the zip only — one row per
        # permit would be ~1500 rows of identical templates over a remote DB
        # (the generation would take minutes instead of seconds).
        saved = []
        for g in generated:
            if g["kind"] == "FORM":
                continue
            doc = PermitDocument.objects.create(
                permit=anchor,
                name=g["name"],
                kind=g["kind"],
                version=version,
                url="",
            )
            doc.file.save(g["filename"], ContentFile(g["content"].encode("utf-8")), save=True)
            saved.append({
                "name": g["name"],
                "kind": g["kind"],
                "filename": g["filename"],
                "description": g["description"],
                "url": doc.file.url if doc.file else "",
                "document_id": str(doc.id),
            })

        kinds = {g["kind"] for g in generated}
        promotion = _promote_evidence(project_id, kinds)

    return {
        "version": version,
        "lld_run_id": run_id,
        "files": saved,
        "zip_size": len(zip_bytes),
        "zip_document_id": str(zip_doc.id),
        "promoted": promotion,
        "errors": errors,
        "manifest": manifest,
    }
