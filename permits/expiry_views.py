"""Expiry views (P21b) — read-only renewal reminders.

``GET /api/ftth/permits/expiries/`` and ``GET /api/ftth/permits/projects/<pid>/expiries/``
return the same ``permits.expiry.summary`` payload: bucketed expiring permits
+ submissions with worst-bucket counts. No mutation, JWT-required, paginated
to the first 100 expiring rows (enough for dashboard + tracker).

Also exposed as ``GET ?threshold_days=&include_ok=`` so the overview card can
request a 30-day window while the tracker can pull ``include_ok=true`` for a
full breakdown.
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from django.http import JsonResponse

from .expiry import summary


class PermitExpiryView(APIView):
    """Cross-project expiry summary (dashboard / global tracker)."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id: str | None = None):
        # Also accept ?project_id= as a query param on the collection route.
        pid = project_id or (request.GET.get("project_id") or "").strip() or None
        try:
            threshold = int((request.GET.get("threshold_days") or request.GET.get("threshold") or "30").strip())
        except ValueError:
            return JsonResponse({"detail": "threshold_days must be an integer."}, status=400)
        threshold = max(1, min(threshold, 365))

        include_ok = (request.GET.get("include_ok") or "").strip().lower() in ("1", "true", "yes")
        # When include_ok is requested we still summarize at the threshold — the
        # caller gets all buckets but can filter client-side.
        data = summary(project_id=pid, threshold_days=threshold, now=timezone.now())
        if include_ok:
            # Recompute with include_ok so the caller sees ok bucket counts.
            from .expiry import get_expiring_permits, get_expiring_submissions

            now = timezone.now()
            permits_all = get_expiring_permits(pid, threshold_days=threshold, include_ok=True, now=now)
            subs_all = get_expiring_submissions(pid, threshold_days=threshold, include_ok=True, now=now)
            data["permits_all"] = permits_all[:200]
            data["submissions_all"] = subs_all[:200]
        return JsonResponse(data)
