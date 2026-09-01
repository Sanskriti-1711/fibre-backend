"""Detailed preliminary HLD permit package generator.

The HLD package mirrors the final LLD package structure where preliminary
geometry and quantities exist, but every artifact is explicitly labelled as
an estimate pending Survey and LLD confirmation.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any

from django.core.files.base import ContentFile
from django.db import transaction

from ftth_hld.models import FtthLayer

from ..models import PermitDocument, PermitEvent, PermitMatrix
from . import permit_forms, data


def generate_hld_attribute_reports(project_id: str, project_name: str) -> list[dict[str, Any]]:
    """Generate HLD-only schedules from the persisted HLD attribute tables."""
    sections = []
    for layer_name in ("trenches", "ducts", "cables", "chambers", "pdps", "objects", "polygons", "brownfield"):
        features = data.hld_layer_features(project_id, layer_name)
        rows = []
        for feature in features:
            props = data._props(feature)
            rows.append("<tr>" + "".join(
                f"<td>{__import__('html').escape(str(props.get(k, '')))}</td>"
                for k in sorted(props)
            ) + "</tr>")
        headers = sorted({k for f in features for k in data._props(f)})
        if not headers:
            continue
        head = "".join(f"<th>{__import__('html').escape(k)}</th>" for k in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{__import__('html').escape(str(data._props(f).get(k, '')))}</td>" for k in headers) + "</tr>"
            for f in features
        )
        content = f"<!doctype html><html><head><meta charset='utf-8'><title>HLD {layer_name} attributes</title></head><body><h1>{project_name} — HLD {layer_name} attribute schedule</h1><p>HLD preliminary estimate; Survey and LLD confirmation pending.</p><table border='1' cellspacing='0' cellpadding='4'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></body></html>"
        sections.append({"name": f"HLD attribute schedule — {layer_name}", "kind": "SCHEDULE", "filename": f"hld_preliminary/schedules/{layer_name}_attributes.html", "content": content, "description": f"Complete HLD {layer_name} attribute schedule"})
    return sections
from .hld_summary import generate_hld_summary
from . import drawings, traffic_plan


def _hld_layer_summary(project_id: str) -> dict[str, Any]:
    return {
        row.name: row.feature_count
        for row in FtthLayer.objects.filter(ftth_project__project_id=project_id)
    }


def _preliminary_notice(project_id: str, project_name: str, layers: dict[str, Any], permits: list[PermitMatrix]) -> str:
    generated = datetime.now(timezone.utc).isoformat()
    by_type: dict[str, int] = {}
    for permit in permits:
        by_type[permit.permit_type] = by_type.get(permit.permit_type, 0) + 1
    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in sorted(by_type.items())) or '<tr><td colspan="2">No permit rows yet</td></tr>'
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>HLD Preliminary Permit Package</title>
<style>body{{font-family:Arial;margin:32px;color:#111827}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d1d5db;padding:7px;text-align:left;font-size:12px}}th{{background:#f3f4f6}}.notice{{padding:14px;background:#fff7ed;border:2px solid #fb923c;border-radius:8px}}</style></head>
<body><h1>Detailed Preliminary Permit Package — HLD Estimate</h1>
<div class="notice"><strong>PRELIMINARY ESTIMATE — NOT FOR CONSTRUCTION OR AUTHORITY SUBMISSION</strong><br>
This package mirrors the detailed LLD permit structure using HLD geometry and estimated quantities. Survey evidence, street conditions, photographs, final dimensions, authorities and LLD changes must be confirmed before submission.</div>
<h2>Project</h2><table><tr><th>Project</th><td>{project_name}</td></tr><tr><th>Project ID</th><td>{project_id}</td></tr><tr><th>Generated</th><td>{generated}</td></tr><tr><th>Package</th><td>HLD-PRELIMINARY-ESTIMATE</td></tr><tr><th>Survey status</th><td>Pending — field verification required</td></tr><tr><th>LLD status</th><td>Not final — regenerate after approved survey changes</td></tr></table>
<h2>Permit inventory</h2><table><tr><th>Permit type</th><th>Rows</th></tr>{rows}</table>
<h2>Document status</h2><ul><li>Route geometry and quantities: HLD estimate</li><li>Traffic controls: preliminary assumptions</li><li>Photographs: pending Survey</li><li>Approvals and submission references: pending authority process</li></ul>
</body></html>"""


def generate_hld_package(project_id: str, project_name: str) -> dict[str, Any]:
    """Generate and persist a detailed, planning-only HLD package."""
    layers = _hld_layer_summary(project_id)
    permits = list(PermitMatrix.objects.filter(project_id=project_id).select_related("authority", "rule"))
    if not layers:
        raise ValueError("No persisted HLD layers found — complete the HLD first.")
    if not permits:
        raise ValueError("No permit matrix rows — run permit analysis first.")

    generated: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        overview = permit_forms.hld_permit_overview(project_id, project_name)
        overview.update(name="HLD detailed preliminary permit overview", filename="hld_preliminary/hld_permit_overview.html")
        generated.append(overview)
    except Exception as exc:
        errors.append(f"hld_permit_overview: {exc}")
    try:
        generated.append(generate_hld_summary(project_id, project_name))
    except Exception as exc:
        errors.append(f"hld_street_permit_summary: {exc}")

    # Include the same non-priced technical artifacts used by the LLD package.
    # HLD data is explicitly preliminary; costs remain in the BOQ/BOM only.
    # The shared LLD generators read final_trenches. During HLD, route the
    # preliminary generators through the persisted HLD trench layer so the
    # package contains real HLD drawings, sections and TMPs rather than an
    # empty placeholder set.
    # Build HLD-native route artifacts from the HLD attribute tables. The
    # shared renderers remain unchanged for LLD; these adapters prevent HLD
    # generation from consulting business.ftth_lld_layers.
    hld_routes = []
    for layer_name in ("trenches", "ducts", "cables", "chambers", "pdps", "objects", "polygons", "brownfield"):
        features = data.hld_layer_features(project_id, layer_name)
        if features:
            hld_routes.append({
                "name": f"HLD drawing — {layer_name}",
                "kind": "DRAWING",
                "filename": f"hld_preliminary/drawings/{layer_name}.geojson",
                "content": json.dumps({"type": "FeatureCollection", "features": features}, indent=1),
                "description": f"HLD attribute-backed route drawing — {layer_name}",
            })
    generated.extend(hld_routes)
    generated.extend(generate_hld_attribute_reports(project_id, project_name))

    notice = {"name": "HLD preliminary estimate notice", "kind": "REPORT", "filename": "hld_preliminary/README_PRELIMINARY_ESTIMATE.html", "content": _preliminary_notice(project_id, project_name, layers, permits), "description": "Planning-only estimate notice"}
    generated.append(notice)
    if not generated:
        raise ValueError("No HLD permit documents could be generated.")

    manifest = {
        "package_type": "HLD_PRELIMINARY_ESTIMATE",
        "package_label": "HLD Preliminary / Detailed Estimate / Survey Pending",
        "project_id": project_id,
        "project_name": project_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "survey_pending": True,
        "lld_required_for_final_package": True,
        "layers": layers,
        "permit_rows": len(permits),
        "documents": [item["filename"] for item in generated],
        "errors": errors,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for item in generated:
            zf.writestr(item["filename"], item["content"])
    version = (PermitDocument.objects.filter(permit__project_id=project_id).order_by("-version").values_list("version", flat=True).first() or 0) + 1
    anchor = permits[0]
    with transaction.atomic():
        package = PermitDocument.objects.create(permit=anchor, name=f"HLD detailed preliminary permit package v{version}", kind="REPORT", version=version)
        package.file.save(f"hld_preliminary_permit_package_v{version}.zip", ContentFile(buf.getvalue()), save=True)
        saved = [{"name": package.name, "kind": package.kind, "version": version, "filename": package.file.name.split("/")[-1], "document_id": str(package.id), "url": package.file.url}]
        for item in generated:
            doc = PermitDocument.objects.create(permit=anchor, name=item["name"], kind=item["kind"], version=version)
            doc.file.save(item["filename"].replace("/", "_"), ContentFile(item["content"].encode("utf-8")), save=True)
            saved.append({"name": item["name"], "kind": item["kind"], "version": version, "filename": doc.file.name.split("/")[-1], "document_id": str(doc.id), "url": doc.file.url})
        PermitEvent.objects.create(permit=anchor, event="HLD_PRELIMINARY_PACKAGE_GENERATED", detail={"version": version, "package_type": "HLD_PRELIMINARY_ESTIMATE", "survey_pending": True})
    return {"package_type": "HLD_PRELIMINARY_ESTIMATE", "package_label": "HLD Preliminary / Detailed Estimate / Survey Pending", "version": version, "files": saved, "zip_size": len(buf.getvalue()), "manifest": manifest}
