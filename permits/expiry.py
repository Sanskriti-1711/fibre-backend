"""Expiry-date renewal reminders (P21b) — deterministic buckets + queries.

No ML, no mutation of PermitMatrix status. Every permit/submission with an
``expiry_date`` is bucketed by days-until-expiry; the API and the management
command surface the buckets so planners see 30/14/7/1-day + expired in time to
renew. Nothing here flips ``status`` — the submission state machine still owns
transitions; expiry is a calendar risk, not an auto-close.

Buckets (working from ``timezone.now()``):
  expired  — already past (days < 0)
  due_1d   — expires within 1 day (0 ≤ days ≤ 1)
  due_7d   — within 7 days (1 < days ≤ 7)
  due_14d  — within 14 days (7 < days ≤ 14)
  due_30d  — within 30 days (14 < days ≤ 30)
  ok       — more than 30 days out
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.utils import timezone

from .models import PermitMatrix, PermitSubmission

# Ordered thresholds — bucket_for walks them in order.
_THRESHOLDS = [
    (1, 'due_1d'),
    (7, 'due_7d'),
    (14, 'due_14d'),
    (30, 'due_30d'),
]

BUCKET_LABEL: dict[str, str] = {
    'expired': 'Expired',
    'due_1d': 'Expires in ≤1 day',
    'due_7d': 'Expires in ≤7 days',
    'due_14d': 'Expires in ≤14 days',
    'due_30d': 'Expires in ≤30 days',
    'ok': 'OK (>30 days)',
}

BUCKET_PRIORITY: dict[str, int] = {
    'expired': 5,
    'due_1d': 4,
    'due_7d': 3,
    'due_14d': 2,
    'due_30d': 1,
    'ok': 0,
}

# Severities for UI colour coding.
BUCKET_SEVERITY: dict[str, str] = {
    'expired': 'critical',
    'due_1d': 'critical',
    'due_7d': 'warning',
    'due_14d': 'warning',
    'due_30d': 'info',
    'ok': 'ok',
}


def days_until_expiry(expiry_date: datetime | None, now: datetime | None = None) -> int | None:
    """Whole days from ``now`` to ``expiry_date`` (negative when expired).

    Returns None when expiry_date is not set. Naive datetimes are treated as
    UTC — the submissions helper coerces dates to aware datetimes on write, so
    naive values are legacy only.
    """
    if expiry_date is None:
        return None
    if now is None:
        now = timezone.now()
    if timezone.is_naive(expiry_date):
        expiry_date = timezone.make_aware(expiry_date, timezone.get_current_timezone())
    if timezone.is_naive(now):
        now = timezone.make_aware(now, timezone.get_current_timezone())
    # Floor to whole days — a permit expiring in 3 hours is "0 days left".
    delta = expiry_date - now
    return int(delta.total_seconds() // 86400)


def bucket_for(days: int | None) -> str:
    """Bucket name for a days-until value (or 'ok' when None/no expiry)."""
    if days is None:
        return 'ok'
    if days < 0:
        return 'expired'
    for limit, name in _THRESHOLDS:
        if days <= limit:
            return name
    return 'ok'


def _serialize_permit(pm: PermitMatrix, now: datetime) -> dict[str, Any]:
    days = days_until_expiry(pm.expiry_date, now)
    b = bucket_for(days)
    return {
        'permit_id': str(pm.permit_id),
        'project_id': str(pm.project_id),
        'project_name': (
            getattr(pm.project, 'name', None) if hasattr(pm, 'project') and pm.project_id else None
        ),
        'permit_type': pm.permit_type,
        'permit_group': pm.permit_group or '',
        'route_section': pm.route_section,
        'authority': {
            'code': pm.authority.code if pm.authority else None,
            'name': pm.authority.name if pm.authority else None,
        },
        'status': pm.status,
        'expiry_date': pm.expiry_date.isoformat() if pm.expiry_date else None,
        'days_until': days,
        'bucket': b,
        'label': BUCKET_LABEL[b],
        'severity': BUCKET_SEVERITY[b],
    }


def _serialize_submission(sub: PermitSubmission, now: datetime) -> dict[str, Any]:
    days = days_until_expiry(sub.expiry_date, now)
    b = bucket_for(days)
    return {
        'submission_id': str(sub.id),
        'project_id': str(sub.project_id),
        'project_name': (
            getattr(sub.project, 'name', None)
            if hasattr(sub, 'project') and sub.project_id
            else None
        ),
        'permit_type': sub.permit_type,
        'permit_group': sub.permit_group or '',
        'label': sub.label,
        'authority': {
            'code': sub.authority.code if sub.authority else None,
            'name': sub.authority.name if sub.authority else None,
        },
        'status': sub.status,
        'reference': sub.reference or '',
        'expiry_date': sub.expiry_date.isoformat() if sub.expiry_date else None,
        'approval_date': sub.approval_date.isoformat() if sub.approval_date else None,
        'days_until': days,
        'bucket': b,
        'label_bucket': BUCKET_LABEL[b],
        'severity': BUCKET_SEVERITY[b],
    }


def get_expiring_permits(
    project_id: str | None = None,
    threshold_days: int = 30,
    include_ok: bool = False,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Permits whose expiry is within ``threshold_days`` (or already expired).

    When ``include_ok`` is True, every row with an expiry_date is returned
    regardless of bucket (useful for the overview card's full breakdown).
    """
    if now is None:
        now = timezone.now()
    qs = PermitMatrix.objects.select_related('authority', 'project').filter(
        expiry_date__isnull=False
    )
    if project_id:
        qs = qs.filter(project_id=project_id)
    qs = qs.order_by('expiry_date')
    out: list[dict[str, Any]] = []
    for pm in qs:
        days = days_until_expiry(pm.expiry_date, now)
        b = bucket_for(days)
        if b == 'ok' and not include_ok and (days is None or days > threshold_days):
            continue
        # threshold filter: expired always included; otherwise days <= threshold
        if not include_ok and days is not None and days > threshold_days:
            continue
        out.append(_serialize_permit(pm, now))
    # Worst bucket first, then soonest expiry.
    out.sort(
        key=lambda r: (
            -BUCKET_PRIORITY.get(r['bucket'], 0),
            r['days_until'] if r['days_until'] is not None else 9999,
        )
    )
    return out


def get_expiring_submissions(
    project_id: str | None = None,
    threshold_days: int = 30,
    include_ok: bool = False,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Submissions whose expiry is within ``threshold_days`` (or already expired)."""
    if now is None:
        now = timezone.now()
    qs = PermitSubmission.objects.select_related('authority', 'project').filter(
        expiry_date__isnull=False
    )
    if project_id:
        qs = qs.filter(project_id=project_id)
    qs = qs.order_by('expiry_date')
    out: list[dict[str, Any]] = []
    for sub in qs:
        days = days_until_expiry(sub.expiry_date, now)
        b = bucket_for(days)
        if b == 'ok' and not include_ok and (days is None or days > threshold_days):
            continue
        if not include_ok and days is not None and days > threshold_days:
            continue
        out.append(_serialize_submission(sub, now))
    out.sort(
        key=lambda r: (
            -BUCKET_PRIORITY.get(r['bucket'], 0),
            r['days_until'] if r['days_until'] is not None else 9999,
        )
    )
    return out


def summary(
    project_id: str | None = None,
    threshold_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project (or cross-project) expiry summary.

    Returns counts per bucket (including 'ok' when a permit has an expiry),
    plus the soonest expiring rows for quick display.
    """
    if now is None:
        now = timezone.now()
    permits = get_expiring_permits(project_id, threshold_days, include_ok=True, now=now)
    submissions = get_expiring_submissions(project_id, threshold_days, include_ok=True, now=now)

    def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        c: dict[str, int] = {}
        for r in rows:
            c[r['bucket']] = c.get(r['bucket'], 0) + 1
        return c

    permit_counts = counts(permits)
    submission_counts = counts(submissions)

    # Expiring = within threshold or already expired.
    expiring_permits = [
        r
        for r in permits
        if r['bucket'] != 'ok'
        or (r['days_until'] is not None and r['days_until'] <= threshold_days)
    ]
    expiring_submissions = [
        r
        for r in submissions
        if r['bucket'] != 'ok'
        or (r['days_until'] is not None and r['days_until'] <= threshold_days)
    ]

    # Aggregate expiring counts per bucket for the caller.
    expiring_permit_counts: dict[str, int] = {}
    for r in expiring_permits:
        expiring_permit_counts[r['bucket']] = expiring_permit_counts.get(r['bucket'], 0) + 1
    expiring_sub_counts: dict[str, int] = {}
    for r in expiring_submissions:
        expiring_sub_counts[r['bucket']] = expiring_sub_counts.get(r['bucket'], 0) + 1

    total_with_expiry = len(permits) + len(submissions)
    total_expiring = len(expiring_permits) + len(expiring_submissions)

    return {
        'project_id': project_id,
        'threshold_days': threshold_days,
        'now': now.isoformat(),
        'total_with_expiry': total_with_expiry,
        'total_expiring': total_expiring,
        'permits_with_expiry': len(permits),
        'submissions_with_expiry': len(submissions),
        'permit_counts': permit_counts,
        'submission_counts': submission_counts,
        'expiring_permit_counts': expiring_permit_counts,
        'expiring_submission_counts': expiring_sub_counts,
        'expiring_permits': expiring_permits[:100],
        'expiring_submissions': expiring_submissions[:100],
        'buckets': list(BUCKET_LABEL.keys()),
        'bucket_labels': BUCKET_LABEL,
    }
