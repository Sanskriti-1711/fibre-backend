"""
Cross-run diff summary (Tier-1 A24).

Answers "what changed between LLD-V05 and LLD-V06" from data that is
already persisted per run: per-layer feature counts, line lengths in
metres, and the run-level validation summary. Pure arithmetic over the
stored GeoJSON — no ML, no re-rendering.

The optional AI paragraph only *describes* the deterministic numbers;
it never produces them.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .models import LldRun


def diff_runs(project_id: str, from_version: str, to_version: str) -> Dict:
    """Compare two completed LLD runs of one project.

    Returns {from, to, layers: [...], totals, summary, ai_summary?}.
    Raises ValueError with a user-readable message when either run is
    missing; callers map that to HTTP 404/400.
    """
    run_a = _get_run(project_id, from_version)
    run_b = _get_run(project_id, to_version)

    snap_a = _snapshot(run_a)
    snap_b = _snapshot(run_b)

    names = sorted(set(snap_a["layers"]) | set(snap_b["layers"]))
    layer_rows: List[Dict] = []
    for name in names:
        a = snap_a["layers"].get(name)
        b = snap_b["layers"].get(name)
        d_features = (b["feature_count"] if b else 0) - (a["feature_count"] if a else 0)
        d_length = (b["length_m"] if b else 0.0) - (a["length_m"] if a else 0.0)
        if a is None:
            status = "added"
        elif b is None:
            status = "removed"
        elif d_features or abs(d_length) >= 1.0:  # 1 m noise floor
            status = "changed"
        else:
            status = "unchanged"
        layer_rows.append({
            "name": name,
            "from_count": a["feature_count"] if a else 0,
            "to_count": b["feature_count"] if b else 0,
            "delta_count": d_features,
            "from_length_m": a["length_m"] if a else 0.0,
            "to_length_m": b["length_m"] if b else 0.0,
            "delta_length_m": round(d_length, 1),
            "status": status,
        })

    totals = {
        "from_features": snap_a["features"],
        "to_features": snap_b["features"],
        "delta_features": snap_b["features"] - snap_a["features"],
        "from_length_m": round(snap_a["length_m"], 1),
        "to_length_m": round(snap_b["length_m"], 1),
        "delta_length_m": round(snap_b["length_m"] - snap_a["length_m"], 1),
        "layers_changed": sum(1 for r in layer_rows if r["status"] == "changed"),
        "layers_added": sum(1 for r in layer_rows if r["status"] == "added"),
        "layers_removed": sum(1 for r in layer_rows if r["status"] == "removed"),
    }

    result = {
        "project_id": project_id,
        "from": _run_meta(run_a),
        "to": _run_meta(run_b),
        "layers": layer_rows,
        "totals": totals,
        "summary": _deterministic_summary(run_a, run_b, totals),
    }

    # Optional AI rewrite of the deterministic summary — advisory only.
    if totals["layers_changed"] or totals["layers_added"] or totals["layers_removed"]:
        try:
            from permits.ai.provider import AI_DISCLAIMER, chat_completion

            note = chat_completion(
                system=(
                    "You are a fibre design reviewer. In at most 3 short sentences, "
                    "explain what this version-to-version design diff means for the "
                    "reviewer. Use only the numbers given; do not invent any."
                ),
                user=(
                    f"From {run_a.lld_version} to {run_b.lld_version}: "
                    f"features {totals['from_features']} → {totals['to_features']} "
                    f"({totals['delta_features']:+d}), length "
                    f"{totals['from_length_m']} m → {totals['to_length_m']} m "
                    f"({totals['delta_length_m']:+.1f} m). Changed layers: "
                    + ", ".join(
                        f"{r['name']} ({r['delta_count']:+d} features, "
                        f"{r['delta_length_m']:+.1f} m)"
                        for r in layer_rows if r["status"] != "unchanged"
                    )
                ),
                max_tokens=400,
                timeout_s=15.0,
            )
            if note:
                result["ai_summary"] = note
                result["ai_disclaimer"] = AI_DISCLAIMER
        except Exception:
            pass

    return result


# ── internals ──────────────────────────────────────────────────────────


def _get_run(project_id: str, lld_version: str) -> LldRun:
    run = (
        LldRun.objects.filter(
            ftth_project__project_id=project_id,
            lld_version=lld_version,
        )
        .prefetch_related("layers")
        .first()
    )
    if run is None:
        raise ValueError(f"LLD run {lld_version!r} not found for this project.")
    return run


def _snapshot(run: LldRun) -> Dict:
    """Per-layer {feature_count, length_m} plus project totals for a run."""
    from ftth_hld.boq import _geometry_length

    layers: Dict[str, Dict] = {}
    features = 0
    length_m = 0.0
    for layer in run.layers.all():
        geojson = layer.geojson or {}
        feats = geojson.get("features") or []
        layer_len = 0.0
        for f in feats:
            layer_len += _geometry_length((f or {}).get("geometry"))
        count = layer.feature_count or len(feats)
        layers[layer.name] = {
            "feature_count": int(count),
            "length_m": round(layer_len, 1),
        }
        features += int(count)
        length_m += layer_len
    return {"layers": layers, "features": features, "length_m": length_m}


def _run_meta(run: LldRun) -> Dict:
    validation = run.validation or {}
    return {
        "lld_version": run.lld_version,
        "mode": run.mode,
        "status": run.status,
        "hld_version": run.hld_version,
        "approved_survey_version": (
            run.approved_survey_version.version
            if run.approved_survey_version_id else None
        ),
        "run_date": run.run_date.isoformat() if run.run_date else None,
        "validation_issues": validation.get("issues"),
        "validation_checked": validation.get("checked"),
    }


def _deterministic_summary(run_a: LldRun, run_b: LldRun, totals: Dict) -> str:
    """One plain-language sentence built only from the numbers above."""
    if not (totals["layers_changed"] or totals["layers_added"] or totals["layers_removed"]):
        return (
            f"{run_b.lld_version} is identical to {run_a.lld_version} at layer "
            "level — same feature counts and lengths in every layer."
        )
    parts: List[str] = []
    if totals["layers_added"]:
        parts.append(f"{totals['layers_added']} layer(s) added")
    if totals["layers_removed"]:
        parts.append(f"{totals['layers_removed']} layer(s) removed")
    if totals["layers_changed"]:
        parts.append(f"{totals['layers_changed']} layer(s) changed")
    return (
        f"{run_a.lld_version} → {run_b.lld_version}: " + ", ".join(parts)
        + f"; {totals['delta_features']:+d} features and "
        f"{totals['delta_length_m']:+.1f} m of network overall."
    )


def latest_pair(project_id: str) -> Optional[tuple]:
    """(second_latest, latest) completed runs, or None when < 2 exist."""
    runs = list(
        LldRun.objects.filter(
            ftth_project__project_id=project_id,
            status=LldRun.STATUS_COMPLETED,
        ).order_by("-run_date")[:2]
    )
    if len(runs) < 2:
        return None
    return runs[1], runs[0]
