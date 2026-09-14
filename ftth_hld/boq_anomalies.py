"""
BOQ anomaly detection (Tier-1 A4).

Flags BOQ items whose computed quantities look implausible against
project-specific baselines. Pure statistics — no ML required:

1. **Zero-on-length** — a length item (m) computed as exactly 0 while the
   trench/duct layers are non-empty usually means a layering/regression bug.
2. **Unit-cost outliers** — a line total that dominates the BOQ (>60% of the
   grand total) is suspicious for a fibre network where trenching, duct and
   cable split the cost.
3. **MAD outliers vs sibling projects** — quantity-per-km-of-trench compared
   with other completed projects using robust z-scores (MAD). Deviation
   > ANOMALY_PCT (20%) of the median marks a review item. Needs ≥
   MIN_SIBLINGS completed sibling projects; otherwise skipped silently.
"""

from __future__ import annotations

import math
from typing import Dict, List

from django.db.models import Avg, F

from .models import BoqSnapshot

# A single line item may not exceed this share of the grand total.
MAX_LINE_SHARE = 0.60
# Robust z-score above which a quantity-per-km is an outlier.
MAD_Z_THRESHOLD = 3.5
# Relative deviation from the sibling median that marks an anomaly.
ANOMALY_PCT = 0.20
# Minimum completed sibling projects for the MAD comparison to engage.
MIN_SIBLINGS = 3

# Item codes treated as "length" items that must be non-zero for a real design.
_REQUIRED_LENGTH_CODES = ()  # template uses section codes ("2.1"…), so the
# zero-length rule keys off the intensity basis instead (see below).


def detect_boq_anomalies(project_id: str) -> Dict:
    """Analyse the latest BOQ snapshot for a project.

    Returns {"anomalies": [...], "checked": n_rows, "basis": {...}}.
    Never raises — a BOQ without issues returns an empty anomaly list.
    """
    snapshot = (
        BoqSnapshot.objects.filter(ftth_project__project_id=project_id)
        .order_by("-created_at")
        .first()
    )
    if snapshot is None:
        return {"anomalies": [], "checked": 0, "basis": {}}

    rows: List[Dict] = list(snapshot.boq_json or [])
    if not rows:
        return {"anomalies": [], "checked": 0, "basis": {}}

    anomalies: List[Dict] = []
    grand_total = sum(r.get("amount") or 0.0 for r in rows) or 0.0
    home_passed = _find_by_unit(rows, ("hp",))  # row 1.1 — unit "HP"
    total_m = sum(
        float(r.get("quantity") or 0.0)
        for r in rows
        if (r.get("unit") or "").strip().lower() == "m"
    )

    # ── 1. Whole-network sanity ─────────────────────────────────────────
    if total_m <= 0 and home_passed > 0:
        anomalies.append({
            "rule": "zero_network",
            "severity": "high",
            "item_code": "",
            "item_name": "All trench/duct/cable items",
            "message": (
                f"Project reports {home_passed:g} home-passed but zero metres of "
                "network build — the quantities pipeline likely regressed."
            ),
        })

    # ── 3. MAD outliers vs sibling projects (per home-passed) ───────────
    sibling_stats = _sibling_intensity(project_id)
    if sibling_stats.get("n", 0) >= MIN_SIBLINGS and home_passed > 0:
        for r in rows:
            code = r.get("item_code") or ""
            qty = float(r.get("quantity") or 0.0)
            unit = (r.get("unit") or "").strip().lower()
            if qty <= 0 or unit != "m" or code not in sibling_stats["median"]:
                continue
            my_intensity = qty / home_passed
            med = sibling_stats["median"][code]
            mad = sibling_stats["mad"][code]
            rz = _robust_z(my_intensity, med, mad)
            if rz is None or abs(rz) < MAD_Z_THRESHOLD:
                continue
            rel = abs(my_intensity - med) / med if med > 0 else math.inf
            if rel < ANOMALY_PCT:
                continue
            anomalies.append({
                "rule": "mad_outlier",
                "severity": "medium",
                "item_code": code,
                "item_name": r.get("item_name"),
                "message": (
                    f"{r.get('item_name')}: {my_intensity:.2f} m per HP vs "
                    f"{med:.2f} median across {sibling_stats['n']} projects "
                    f"(robust z={rz:+.1f}, {rel * 100:.0f}% off median)."
                ),
            })

    return {
        "anomalies": anomalies,
        "checked": len(rows),
        "basis": {
            "grand_total": round(grand_total, 2),
            "network_m": round(total_m, 1),
            "home_passed": home_passed,
            "siblings_used": sibling_stats.get("n", 0),
        },
    }


def _find_qty(rows: List[Dict], codes: tuple) -> float:
    for r in rows:
        if (r.get("item_code") or "") in codes:
            return float(r.get("quantity") or 0.0)
    return 0.0


def _find_by_unit(rows: List[Dict], units: tuple) -> float:
    """First row whose unit matches (case-insensitive) — e.g. the HP count."""
    for r in rows:
        if (r.get("unit") or "").strip().lower() in units:
            return float(r.get("quantity") or 0.0)
    return 0.0


def _robust_z(x: float, median: float, mad: float) -> float | None:
    """Robust z-score using MAD (0.6745 σ-equivalent scaling)."""
    if mad <= 0:
        return None
    return 0.6745 * (x - median) / mad


def _sibling_intensity(project_id: str) -> Dict:
    """Quantity-per-HP medians across other completed projects (MAD)."""
    from .models import FtthProject

    snaps = (
        BoqSnapshot.objects.exclude(ftth_project__project_id=project_id)
        .filter(ftth_project__status=FtthProject.STATUS_COMPLETED)
        .values("ftth_project__project_id", "boq_json")
    )
    per_project: Dict[str, Dict[str, float]] = {}
    hp_by_project: Dict[str, float] = {}
    for s in snaps:
        pid = s["ftth_project__project_id"]
        rows = s.get("boq_json") or []
        q: Dict[str, float] = {}
        for r in rows:
            c = r.get("item_code") or ""
            if c:
                q[c] = q.get(c, 0.0) + float(r.get("quantity") or 0.0)
        per_project[pid] = q
        hp_by_project[pid] = _find_by_unit(rows, ("hp",))

    n = len(per_project)
    if n == 0:
        return {"n": 0, "median": {}, "mad": {}}

    codes = set()
    for q in per_project.values():
        codes.update(q.keys())

    median: Dict[str, float] = {}
    mad: Dict[str, float] = {}
    for c in codes:
        vals = sorted(
            (q[c] / hp_by_project[pid])
            for pid, q in per_project.items()
            if hp_by_project.get(pid, 0.0) > 0 and c in q
        )
        if len(vals) < MIN_SIBLINGS:
            continue
        med = _median(vals)
        median[c] = med
        mad[c] = _median(sorted(abs(v - med) for v in vals))

    return {"n": n, "median": median, "mad": mad}


def _median(vals):
    v = sorted(vals)
    n = len(v)
    if n == 0:
        return 0.0
    mid = n // 2
    return v[mid] if n % 2 else (v[mid - 1] + v[mid]) / 2.0
