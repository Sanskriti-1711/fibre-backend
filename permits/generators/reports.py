"""Permit package reports: utility conflict, schedules, surface
restoration plan and BOQ reference — all derived from persisted data."""

from __future__ import annotations

import html as _html
from datetime import datetime, timezone
from typing import Any, Optional

from ..models import PermitMatrix
from . import data


def _e(v: Any) -> str:
    return _html.escape("" if v is None else str(v))


def _page(title: str, subtitle: str, body: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>{_e(title)}</title></head>
<body style="font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#111827;">
  <h2 style="margin:0 0 4px;">{_e(title)}</h2>
  <p style="margin:0 0 16px;color:#6B7280;font-size:12px;">{_e(subtitle)}</p>
  {body}
  <p style="margin-top:24px;font-size:10px;color:#9CA3AF;">Generated {now} by the FTTH permit engine.</p>
</body></html>
"""


def utility_conflict_report(project_id: str) -> dict[str, Any]:
    """Coexistence + reuse report from UTILITY_REUSE_001 rows and the
    brownfield layers."""
    rows = [pm for pm in data.permit_rows(project_id) if pm.rule and pm.rule.rule_id == "UTILITY_REUSE_001"]
    brownfield = data.lld_layer_features(project_id, "existing_infrastructure")
    reuse_sources: dict[str, int] = {}
    for pm in rows:
        src = (pm.evidence.get("reuse_source") or {}).get("value")
        if src:
            reuse_sources[str(src)] = reuse_sources.get(str(src), 0) + 1
    tr = "".join(
        f'<tr><td style="padding:4px 8px;border:1px solid #E5E7EB;font-size:12px;">{_e(pm.route_section)}</td>'
        f'<td style="padding:4px 8px;border:1px solid #E5E7EB;font-size:12px;">{_e(pm.get_status_display())}</td>'
        f'<td style="padding:4px 8px;border:1px solid #E5E7EB;font-size:12px;">{pm.readiness_pct}%</td></tr>'
        for pm in rows[:200]
    ) or '<tr><td colspan="3" style="padding:6px 8px;font-size:12px;color:#6B7280;">No reuse/coexistence rows recorded.</td></tr>'
    srcs = "".join(
        f"<li>{_e(k)}: {v} segment(s)</li>" for k, v in sorted(reuse_sources.items())
    ) or "<li>No reuse source recorded.</li>"
    body = f"""
      <h3 style="font-size:14px;margin:0 0 8px;">Existing infrastructure footprint</h3>
      <p style="font-size:12px;">{len(brownfield)} brownfield feature(s) present in the design area.</p>
      <h3 style="font-size:14px;margin:16px 0 8px;">Reuse sources</h3>
      <ul style="font-size:12px;line-height:1.6;">{srcs}</ul>
      <h3 style="font-size:14px;margin:16px 0 8px;">Coexistence rows</h3>
      <table style="border-collapse:collapse;width:100%;">
        <thead><tr style="background:#F9FAFB;">
          <th style="text-align:left;padding:4px 8px;border:1px solid #E5E7EB;font-size:12px;">Route Section</th>
          <th style="text-align:left;padding:4px 8px;border:1px solid #E5E7EB;font-size:12px;">Status</th>
          <th style="text-align:left;padding:4px 8px;border:1px solid #E5E7EB;font-size:12px;">Readiness</th>
        </tr></thead><tbody>{tr}</tbody></table>
      <p style="font-size:11px;color:#6B7280;">Coexistence is informational — capacity checks feed the construction stage.</p>
    """
    return {
        "name": "utility_conflict_report",
        "kind": "REPORT",
        "filename": "reports/utility_conflict_report.html",
        "content": _page("Utility Conflict & Coexistence Report", "From LLD design layers + permit matrix", body),
        "description": "Utility conflict & coexistence report",
    }


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(
        f'<th style="text-align:left;padding:4px 8px;border:1px solid #E5E7EB;font-size:12px;">{_e(h)}</th>'
        for h in headers
    )
    body = "".join(
        "<tr>" + "".join(
            f'<td style="padding:4px 8px;border:1px solid #E5E7EB;font-size:12px;">{_e(c)}</td>'
            for c in row
        ) + "</tr>"
        for row in rows
    ) or '<tr><td colspan="99" style="padding:6px 8px;font-size:12px;color:#6B7280;">No rows.</td></tr>'
    return f'<table style="border-collapse:collapse;width:100%;"><thead><tr style="background:#F9FAFB;">{head}</tr></thead><tbody>{body}</tbody></table>'


def chamber_schedule_report(project_id: str) -> dict[str, Any]:
    rows = [
        [c["id"], c["chamber_type"], c["size"], c["capacity_total"],
         c["capacity_used"], c["equipment"], c["parent_trench"]]
        for c in data.chamber_schedule(project_id)
    ]
    body = _table(
        ["ID", "Type", "Size", "Capacity Total", "Capacity Used", "Equipment", "Parent Trench"],
        rows,
    )
    return {
        "name": "chamber_schedule",
        "kind": "SCHEDULE",
        "filename": "reports/chamber_schedule.html",
        "content": _page("Chamber / Handhole Schedule", "From LLD chambers layer", body),
        "description": "Chamber / handhole schedule",
    }


def pdp_schedule_report(project_id: str) -> dict[str, Any]:
    rows = [
        [p["id"], p["node_type"], p["equip_type"], p["equip_name"],
         p["split_ratio"], p["split_ports"], p["hh"], p["power_required"]]
        for p in data.pdp_schedule(project_id)
    ]
    body = _table(
        ["PDP ID", "Node", "Equipment", "Name", "Split Ratio", "Ports", "HH", "Power"],
        rows,
    )
    return {
        "name": "cabinet_schedule",
        "kind": "SCHEDULE",
        "filename": "reports/cabinet_schedule.html",
        "content": _page("Cabinet (PDP) Schedule", "From LLD pdps layer", body),
        "description": "Cabinet / PDP schedule",
    }


def surface_restoration_report(project_id: str) -> dict[str, Any]:
    stats = data.trench_stats(project_id)
    surf_rows = [
        [s, f"{m:,.1f} m"]
        for s, m in sorted(stats.get("by_surface", {}).items(), key=lambda kv: -kv[1])
    ]
    body = (
        "<h3 style='font-size:14px;margin:0 0 8px;'>Surface footprint (final_trenches)</h3>"
        + _table(["Surface", "Length"], surf_rows)
        + "<h3 style='font-size:14px;margin:16px 0 8px;'>Reinstatement requirements</h3>"
        + "<ul style='font-size:12px;line-height:1.7;'>"
        + "".join(
            f"<li><strong>{_e(s)}</strong>: reinstated to original condition (typical spec in the TMP).</li>"
            for s in stats.get("by_surface", {})
        )
        + "</ul>"
    )
    return {
        "name": "surface_restoration_plan",
        "kind": "REPORT",
        "filename": "reports/surface_restoration_plan.html",
        "content": _page("Surface Restoration Plan", "From final_trenches SURFACE attributes", body),
        "description": "Surface restoration plan",
    }


def boq_reference_report(project_id: str) -> dict[str, Any]:
    stats = data.trench_stats(project_id)
    duct = data.duct_stats(project_id)
    chambers = len(data.chamber_schedule(project_id))
    pdps = len(data.pdp_schedule(project_id))

    # Reuse summary from the BOQ snapshot (metres riding existing infra).
    reuse_rows = ""
    try:
        from ftth_hld.models import BoqSnapshot
        snap = BoqSnapshot.objects.filter(ftth_project__project_id=project_id).first()
        reuse = ((snap.boq_totals or {}).get("reuse") or {}) if snap else {}
        if reuse:
            cells = "".join(
                f"<tr><th style='text-align:left;padding:6px 8px;border:1px solid #E5E7EB;background:#F9FAFB;'>{k}</th>"
                f"<td style='padding:6px 8px;border:1px solid #E5E7EB;'>{v:,.1f} m reused</td></tr>"
                for k, v in sorted(reuse.items())
            )
            reuse_rows = (
                "<tr><th colspan='2' style='text-align:left;padding:6px 8px;border:1px solid #E5E7EB;background:#ECFDF5;color:#047857;'>"
                "♻️ Reused existing infrastructure — not billed as new material</th></tr>" + cells
            )
    except Exception:
        pass

    body = f"""
      <p style="font-size:12px;">Quantities below are computed from the LLD design layers; the authoritative
      priced BOQ/BOM is downloadable from the project's design package.</p>
      <table style="border-collapse:collapse;width:100%;font-size:12px;">
        <tr><th style="text-align:left;width:260px;padding:6px 8px;border:1px solid #E5E7EB;background:#F9FAFB;">Item</th>
            <td style="padding:6px 8px;border:1px solid #E5E7EB;">Value</td></tr>
        <tr><th style="text-align:left;padding:6px 8px;border:1px solid #E5E7EB;background:#F9FAFB;">Total trench length</th>
            <td style="padding:6px 8px;border:1px solid #E5E7EB;">{stats.get('total_length_m', 0):,.1f} m</td></tr>
        <tr><th style="text-align:left;padding:6px 8px;border:1px solid #E5E7EB;background:#F9FAFB;">Distribution ducts</th>
            <td style="padding:6px 8px;border:1px solid #E5E7EB;">{duct.get('distribution', {}).get('count', 0)} ({duct.get('distribution', {}).get('length_m', 0):,.1f} m)</td></tr>
        <tr><th style="text-align:left;padding:6px 8px;border:1px solid #E5E7EB;background:#F9FAFB;">Drop ducts</th>
            <td style="padding:6px 8px;border:1px solid #E5E7EB;">{duct.get('drop', {}).get('count', 0)} ({duct.get('drop', {}).get('length_m', 0):,.1f} m)</td></tr>
        <tr><th style="text-align:left;padding:6px 8px;border:1px solid #E5E7EB;background:#F9FAFB;">Chambers</th>
            <td style="padding:6px 8px;border:1px solid #E5E7EB;">{chambers}</td></tr>
        <tr><th style="text-align:left;padding:6px 8px;border:1px solid #E5E7EB;background:#F9FAFB;">PDPs / cabinets</th>
            <td style="padding:6px 8px;border:1px solid #E5E7EB;">{pdps}</td></tr>
        {reuse_rows}
      </table>
    """
    return {
        "name": "boq_reference",
        "kind": "REPORT",
        "filename": "reports/boq_reference.html",
        "content": _page("BOQ Reference", "Quantities derived from the LLD design", body),
        "description": "BOQ / BOM reference",
    }


def all_reports(project_id: str) -> list[dict[str, Any]]:
    return [
        utility_conflict_report(project_id),
        chamber_schedule_report(project_id),
        pdp_schedule_report(project_id),
        surface_restoration_report(project_id),
        boq_reference_report(project_id),
    ]
