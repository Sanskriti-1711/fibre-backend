"""
Completion rate prediction (Tier-1 A6).

Predicts the final survey completion percentage and its date from current
progress and capture velocity. Purely empirical — no ML:

    progress  = approved+modified features / total planned features
    velocity  = features completed per active day over the recent window
    eta_days  = remaining features / velocity (bounded)
    predicted = 100 * done / (done + remaining_scaled)

The prediction is exposed on the survey copy project via
GET /api/survey/projects/<id>/completion-forecast/ and consumed by the
dashboard progress widgets.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Dict

from django.db.models import Count
from django.utils import timezone

# Recent window for velocity (days). Longer windows smooth spikes but react
# slower to staffing changes.
VELOCITY_WINDOW_DAYS = 14
# Assume at least this many features/day so ETAs stay finite on stalled sites.
MIN_VELOCITY = 0.25
# Cap the ETA so a 1-feature project doesn't show "4 hours".
MAX_ETA_DAYS = 120


def completion_forecast(project) -> Dict:
    """Forecast survey completion for a survey copy Project.

    Returns progress, velocity, predicted final %, ETA date and the inputs
    used, so the UI can show the assumptions behind the number.
    """
    from .models import SurveyFeature

    qs = SurveyFeature.objects.filter(project=project).values("survey_status")
    counts = {row["survey_status"]: row["n"] for row in qs.annotate(n=Count("id"))}

    total = sum(counts.values())
    # Features counted as "done" for progress: approved / completed.
    done = counts.get("approved", 0) + counts.get("completed", 0)
    # In-flight work counts partially (modified = 0.5 — change captured but
    # not yet reviewed).
    in_flight = counts.get("modified", 0) + counts.get("pending_review", 0)
    blocked = counts.get("rejected", 0) + counts.get("needs_correction", 0)
    remaining_new = counts.get("new", 0)

    progress_pct = _pct(done, total)

    # ── Velocity: features reaching approved/modified per active day ──────
    now = timezone.now()
    window_start = now - timedelta(days=VELOCITY_WINDOW_DAYS)
    recent = SurveyFeature.objects.filter(
        project=project, updated_at__gte=window_start
    )
    recent_done = recent.filter(
        survey_status__in=["approved", "completed", "modified", "pending_review"]
    ).count()
    velocity = recent_done / float(VELOCITY_WINDOW_DAYS)
    if velocity < MIN_VELOCITY:
        velocity = MIN_VELOCITY

    # ── Remaining work ────────────────────────────────────────────────────
    # New features count fully; blocked ones need a rework pass (~50% of a
    # fresh capture); unreviewed in-flight needs only review (~20%).
    remaining_units = (
        remaining_new
        + 0.5 * blocked
        + 0.2 * max(0, in_flight - recent_done)
    )

    eta_days = min(MAX_ETA_DAYS, remaining_units / velocity)
    predicted_pct = 100.0
    if remaining_units > 0:
        # Scale: predicted final = done / (done + scaled remaining), floored
        # at current progress (never predict a regression).
        predicted_pct = max(progress_pct, _pct(done, done + remaining_units))
    predicted_date = (now + timedelta(days=math.ceil(eta_days))).date().isoformat()

    return {
        "project_id": str(project.id),
        "total_features": total,
        "done": done,
        "in_flight": in_flight,
        "blocked": blocked,
        "new": remaining_new,
        "progress_pct": round(progress_pct, 1),
        "predicted_completion_pct": round(predicted_pct, 1),
        "velocity_per_day": round(velocity, 2),
        "eta_days": int(math.ceil(eta_days)),
        "eta_date": predicted_date,
        "window_days": VELOCITY_WINDOW_DAYS,
    }


def _pct(a: float, b: float) -> float:
    return (a / b * 100.0) if b else 0.0
