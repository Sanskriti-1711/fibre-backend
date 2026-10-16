"""Phase-3 submission workflow — SUBMITTED → UNDER_REVIEW → APPROVED.

Two operations drive the whole review flow, and every status flip lands on
the audit trail:

* ``create_submissions`` — turn READY ``PermitMatrix`` rows into one
  ``PermitSubmission`` per (project × authority × permit type × street),
  mirroring the package's per-street application forms. Rows in the same
  submission travel as one application: they share the submission status,
  the authority reference and the package version they were built from.

* ``transition_submission`` — the review state machine. Also the hook a
  future portal/email/API status-sync poller calls (``sync_source``
  distinguishes automated flips from manual ones).

Mirrors the design's status flow (DESIGN.md §4): a submission starts
SUBMITTED, the authority acknowledges → UNDER_REVIEW, then APPROVED or
REJECTED; a rejection is re-submitted as a new revision (revision + 1) and
an approval can be CLOSED after construction. Each transition updates every
row in the submission to the same status, so the tracker's group badge and
the HLD/LLD map colouring stay consistent.

The variation workflow (rules/variation.py) detaches re-opened rows from
their submission — a superseded approval never silently stays attached to
the new revision.
"""

from __future__ import annotations

import logging
from datetime import datetime, time

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from . import notifications
from .models import PermitDocument, PermitEvent, PermitMatrix, PermitSubmission

logger = logging.getLogger(__name__)

# Statuses worth an email: the two decisions a reviewer has to act on, plus the
# re-submission that follows a rejection. An acknowledgment (under_review) or a
# closure is bookkeeping and would only add noise.
NOTIFY_ON_STATUS = frozenset(
    {
        PermitSubmission.STATUS_APPROVED,
        PermitSubmission.STATUS_REJECTED,
        PermitSubmission.STATUS_SUBMITTED,
    }
)

# Recorded as the rejection reason when the rejection arrives from an email
# link, which has nowhere to type one. Editable in the tracker afterwards.
EMAIL_REJECTION_NOTE = 'Rejected from the notification email link.'

# Valid state machine edges. A rejection re-enters at SUBMITTED (new
# revision); a closed submission is terminal.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PermitSubmission.STATUS_SUBMITTED: frozenset({PermitSubmission.STATUS_UNDER_REVIEW}),
    PermitSubmission.STATUS_UNDER_REVIEW: frozenset(
        {
            PermitSubmission.STATUS_APPROVED,
            PermitSubmission.STATUS_REJECTED,
        }
    ),
    PermitSubmission.STATUS_APPROVED: frozenset({PermitSubmission.STATUS_CLOSED}),
    PermitSubmission.STATUS_REJECTED: frozenset({PermitSubmission.STATUS_SUBMITTED}),
    PermitSubmission.STATUS_CLOSED: frozenset(),
}

# PermitEvent names per transition target.
_EVENT_FOR_STATUS = {
    PermitSubmission.STATUS_UNDER_REVIEW: 'UNDER_REVIEW',
    PermitSubmission.STATUS_APPROVED: 'APPROVED',
    PermitSubmission.STATUS_REJECTED: 'REJECTED',
    PermitSubmission.STATUS_CLOSED: 'CLOSED',
}


def _coerce_datetime(value):
    """Parse an expiry/approval date from the API into an aware datetime.

    Accepts ISO datetimes (``2027-08-18T23:00:00Z``) and date-only strings
    (``2027-08-18``, as the tracker's prompt sends). A naive value is made
    aware in the current timezone so nothing naive lands in the DB.
    """
    if value in (None, ''):
        return None
    dt: datetime | None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = parse_datetime(str(value))
        if dt is None:
            parsed = parse_date(str(value))
            if parsed is None:
                msg = f'Invalid date value: {value}'
                raise ValueError(msg)
            dt = datetime.combine(parsed, time.min)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _latest_package_version(project_id: str) -> int | None:
    """Most recent generated package version for a project, if any."""
    return (
        PermitDocument.objects.filter(
            permit__project_id=project_id,
            name__startswith='permit_package_v',
        )
        .order_by('-version')
        .values_list('version', flat=True)
        .first()
    )


def create_submissions(
    permit_ids: list[str],
    user=None,
    reference: str = '',
    notes: str = '',
    package_version: int | None = None,
    sync_source: str = 'manual',
    notify: bool = True,
) -> list[PermitSubmission]:
    """Turn READY permit rows into one submission per application group.

    Grouping key: (authority, permit_type, permit_group) — the street-level
    units the package generator produces forms for. Rows without a resolved
    authority (e.g. informational UTILITY_REUSE rows) are rejected with a
    clear message rather than silently submitted to nobody.

    Raises ValueError on any validation failure (caller maps it to a 400).
    """
    rows = list(
        PermitMatrix.objects.filter(permit_id__in=permit_ids).select_related('authority', 'rule')
    )
    if not rows:
        msg = 'No permits found for the given ids.'
        raise ValueError(msg)
    if len(rows) != len(set(permit_ids)):
        msg = 'Duplicate permit ids in the submission batch.'
        raise ValueError(msg)

    projects = {str(r.project_id) for r in rows}
    if len(projects) > 1:
        msg = 'All permits in one submission batch must belong to the same project.'
        raise ValueError(msg)
    project_id = next(iter(projects))

    not_ready = [r.permit_type for r in rows if r.status != PermitMatrix.STATUS_READY]
    if not_ready:
        raise ValueError(
            'Only READY permits can be submitted — the following are not ready: '
            + ', '.join(sorted(set(not_ready))[:5])
            + ('…' if len(set(not_ready)) > 5 else '')
        )
    already_submitted = [r.permit_type for r in rows if r.submission_id is not None]
    if already_submitted:
        raise ValueError(
            'Permits already attached to a submission: '
            + ', '.join(sorted(set(already_submitted))[:5])
        )
    no_authority = [r.permit_type for r in rows if r.authority_id is None]
    if no_authority:
        raise ValueError(
            'Cannot submit permits without a resolved authority — missing for: '
            + ', '.join(sorted(set(no_authority))[:5])
        )

    if package_version is None:
        package_version = _latest_package_version(project_id)

    now = timezone.now()
    groups: dict[tuple, list[PermitMatrix]] = {}
    for r in rows:
        key = (r.authority_id, r.permit_type, r.permit_group or '')
        groups.setdefault(key, []).append(r)

    submissions: list[PermitSubmission] = []
    for (authority_id, permit_type, permit_group), group in groups.items():
        label = permit_type + (f' — {permit_group}' if permit_group else '')
        sub = PermitSubmission.objects.create(
            project_id=project_id,
            authority_id=authority_id,
            permit_type=permit_type,
            permit_group=permit_group,
            label=label,
            status=PermitSubmission.STATUS_SUBMITTED,
            submission_date=now,
            reference=reference,
            notes=notes,
            package_version=package_version,
            sync_source=sync_source,
            created_by=user if user and user.is_authenticated else None,
        )
        for pm in group:
            pm.submission = sub
            pm.status = PermitMatrix.STATUS_SUBMITTED
            pm.submission_date = now
            pm.save(update_fields=['submission', 'status', 'submission_date', 'updated_at'])
            PermitEvent.objects.create(
                permit=pm,
                event='SUBMITTED',
                detail={
                    'submission_id': str(sub.id),
                    'reference': reference,
                    'package_version': package_version,
                    'by': getattr(user, 'email', None) if user else None,
                },
            )
        submissions.append(sub)
        logger.info(
            'Submission %s created: %s (%s) — %s row(s)',
            sub.id,
            label,
            project_id,
            len(group),
        )

    # Tell the reviewer the applications are waiting. Queued on the transaction,
    # so a rolled-back batch emails nobody, and grouped into ONE message for the
    # whole batch rather than one per authority.
    if notify and submissions:
        notifications.queue_submission_created(
            submissions,
            reference=reference,
            actor=getattr(user, 'email', None) if user else None,
            package_version=package_version,
        )
    return submissions


def transition_submission(
    submission: PermitSubmission,
    to_status: str,
    user=None,
    reference: str | None = None,
    notes: str | None = None,
    conditions: str | None = None,
    expiry_date=None,
    sync_source: str = 'manual',
    actor_email: str | None = None,
    notify: bool = True,
) -> PermitSubmission:
    """Move a submission through the review state machine.

    Every transition mirrors the status onto all attached rows and writes a
    ``PermitEvent`` per row, so the audit trail always explains a badge.
    ``to_status == submitted`` from REJECTED is a re-submission: revision
    +1, submission date refreshed, approval fields cleared.

    Raises ValueError for invalid transitions (caller maps it to a 400).

    ``actor_email`` names the actor when there is no ``User`` — an emailed
    Approve/Reject link acts without a session, and the audit entry should still
    say who it was.
    """
    from_status = submission.status
    if to_status not in ALLOWED_TRANSITIONS:
        msg = f'Unknown submission status: {to_status}'
        raise ValueError(msg)
    if to_status not in ALLOWED_TRANSITIONS.get(submission.status, frozenset()):
        msg = (
            f"Invalid transition {submission.status} → {to_status} "
            f"(allowed: {sorted(ALLOWED_TRANSITIONS.get(submission.status, frozenset())) or 'none'})"
        )
        raise ValueError(msg)

    actor = getattr(user, 'email', None) if user else (actor_email or None)
    now = timezone.now()
    rows = list(submission.permit_rows.all())
    event = _EVENT_FOR_STATUS.get(to_status, 'STATUS_UPDATE')
    detail: dict = {'by': actor, 'sync_source': sync_source}

    if to_status == PermitSubmission.STATUS_SUBMITTED:
        # Re-submission after a rejection — new revision, dates refreshed.
        submission.revision += 1
        submission.submission_date = now
        submission.approval_date = None
        submission.expiry_date = None
        submission.conditions = ''
        event = 'RESUBMITTED'
        detail['revision'] = submission.revision
        for pm in rows:
            pm.status = PermitMatrix.STATUS_SUBMITTED
            pm.submission_date = now
            pm.approval_date = None
            pm.expiry_date = None
            pm.save(
                update_fields=[
                    'status',
                    'submission_date',
                    'approval_date',
                    'expiry_date',
                    'updated_at',
                ]
            )
            PermitEvent.objects.create(
                permit=pm,
                event=event,
                detail={**detail, 'revision': submission.revision},
            )
    elif to_status == PermitSubmission.STATUS_APPROVED:
        submission.approval_date = now
        submission.conditions = (conditions or '').strip() or submission.conditions
        expiry = _coerce_datetime(expiry_date)
        if expiry:
            submission.expiry_date = expiry
        detail['conditions'] = submission.conditions
        for pm in rows:
            pm.status = PermitMatrix.STATUS_APPROVED
            pm.approval_date = now
            pm.conditions = submission.conditions
            if expiry:
                pm.expiry_date = expiry
            pm.save(
                update_fields=[
                    'status',
                    'approval_date',
                    'conditions',
                    'expiry_date',
                    'updated_at',
                ]
            )
            PermitEvent.objects.create(permit=pm, event=event, detail=detail)
    elif to_status == PermitSubmission.STATUS_REJECTED:
        reason = (notes or '').strip()
        if reason:
            detail['reason'] = reason
        for pm in rows:
            pm.status = PermitMatrix.STATUS_REJECTED
            pm.save(update_fields=['status', 'updated_at'])
            PermitEvent.objects.create(permit=pm, event=event, detail=detail)
    else:  # UNDER_REVIEW | CLOSED
        if reference is not None:
            submission.reference = reference.strip()
        if notes is not None and notes.strip():
            submission.notes = notes.strip()
        for pm in rows:
            pm.status = {
                PermitSubmission.STATUS_UNDER_REVIEW: PermitMatrix.STATUS_UNDER_REVIEW,
                PermitSubmission.STATUS_CLOSED: PermitMatrix.STATUS_CLOSED,
            }[to_status]
            pm.save(update_fields=['status', 'updated_at'])
            PermitEvent.objects.create(permit=pm, event=event, detail=detail)

    submission.status = to_status
    submission.sync_source = sync_source
    submission.save(
        update_fields=[
            'status',
            'revision',
            'submission_date',
            'approval_date',
            'expiry_date',
            'conditions',
            'reference',
            'notes',
            'sync_source',
            'updated_at',
        ]
    )
    logger.info(
        'Submission %s → %s (%s) by %s',
        submission.id,
        to_status,
        sync_source,
        actor or 'system',
    )

    if notify and to_status in NOTIFY_ON_STATUS:
        notifications.queue_status_change(
            submission,
            from_status=from_status,
            to_status=to_status,
            actor=actor,
            reason=(notes or '').strip(),
            sync_source=sync_source,
        )
    return submission


def status_path(from_status: str, to_status: str, max_hops: int = 3) -> list[str]:
    """Shortest chain of legal transitions from one status to another.

    ``ALLOWED_TRANSITIONS`` is the only authority on what may follow what, so a
    request for a status the workflow cannot reach in one step is walked along
    the legal edges instead of widening the state machine. Approving straight
    from SUBMITTED is the case that matters: the email link asks for APPROVED,
    the machine requires the acknowledgment first, so the action applies
    SUBMITTED → UNDER_REVIEW → APPROVED and the audit trail reads exactly as if
    a planner had clicked the two buttons in order.

    Returns [] when ``from_status`` already is the target, and [] when there is
    no path at all (a closed submission, say) — the caller tells those apart by
    looking at the current status.
    """
    if from_status == to_status:
        return []
    queue: list[tuple[str, list[str]]] = [(from_status, [])]
    seen = {from_status}
    while queue:
        current, path = queue.pop(0)
        for nxt in sorted(ALLOWED_TRANSITIONS.get(current, frozenset())):
            if nxt == to_status:
                return [*path, nxt]
            if nxt not in seen and len(path) + 1 < max_hops:
                seen.add(nxt)
                queue.append((nxt, [*path, nxt]))
    return []


def apply_review_action(
    submission: PermitSubmission,
    action: str,
    *,
    actor_email: str | None = None,
    sync_source: str = 'email',
) -> tuple[str, list[str]]:
    """Apply an emailed Approve/Reject link. Returns (target status, steps).

    Reuses ``transition_submission`` for every edge, so an emailed decision is
    indistinguishable from a clicked one in the state machine and the audit
    trail apart from the honest ``sync_source='email'``.

    Raises ValueError when the submission cannot reach the requested status.
    """
    if action not in notifications.ACTION_STATUSES:
        msg = f'Unknown review action: {action}'
        raise ValueError(msg)
    target = notifications.ACTION_STATUSES[action]
    if submission.status == target:
        return target, []
    path = status_path(submission.status, target)
    if not path:
        msg = (
            f'This application is {submission.status} — it cannot be moved to {target}. '
            f'Open it in the tracker.'
        )
        raise ValueError(msg)
    with transaction.atomic():
        for step in path:
            transition_submission(
                submission,
                step,
                user=None,
                actor_email=actor_email,
                notes=EMAIL_REJECTION_NOTE if step == PermitSubmission.STATUS_REJECTED else None,
                sync_source=sync_source,
            )
    submission.refresh_from_db()
    return target, path
