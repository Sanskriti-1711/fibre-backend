"""Permit review email — sent-for-approval, review outcome, renewal digest.

The submission workflow in ``permits.submissions`` already records *what*
happened; this module is the missing notification leg, so a reviewer finds out
without watching the tracker. Three events are mailed:

* **sent for approval** — ``create_submissions`` accepted a batch of READY
  permits and the applications are waiting for the reviewer;
* **review outcome** — an application was approved / rejected / re-submitted,
  whether a planner clicked it in the tracker or a portal poller applied it;
* **renewal reminder** — a digest from ``check_permit_expiries --email``
  covering permits inside the 30-day window.

Everything here is a *notification*: no function in this module mutates a
permit or a submission. Approving from an email link goes through
``permits.submissions.transition_submission`` like every other review action,
so the state machine and the audit trail stay the single source of truth.

Delivery follows the same rule as every other permit integration — degrade,
never fail — so an unconfigured or unreachable mailbox can never cost a
submission:

* no ``PERMIT_NOTIFY_EMAIL`` -> nothing is sent and the attempt is recorded as
  ``skipped`` rather than raising;
* no ``EMAIL_HOST_USER`` -> Django's console backend prints the message, so
  local development works end to end with no mailbox at all;
* an SMTP error -> caught, logged, recorded on ``PermitNotification``. A
  reviewer who never got the mail is visible in the API instead of silent.

Sending is deferred to ``transaction.on_commit`` and **coalesced per kind**:
one approval is one email, while a portal poll that flips a dozen submissions
in one pass is one digest listing all twelve rather than a dozen messages. If a
transaction is ever rolled back, no mail is sent at all.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from django.conf import settings
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from .models import PermitNotification, PermitSubmission

logger = logging.getLogger(__name__)

# Salt for the emailed Approve/Reject tokens — changing it invalidates every
# link already sitting in an inbox, which is the point of keeping it constant.
TOKEN_SALT = 'permits.review-action'

ACTION_APPROVE = 'approve'
ACTION_REJECT = 'reject'
ACTION_LABELS = {ACTION_APPROVE: 'Approve', ACTION_REJECT: 'Reject'}
ACTION_VERBS = {ACTION_APPROVE: 'approved', ACTION_REJECT: 'rejected'}
ACTION_STATUSES = {
    ACTION_APPROVE: PermitSubmission.STATUS_APPROVED,
    ACTION_REJECT: PermitSubmission.STATUS_REJECTED,
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def notify_recipients() -> list[str]:
    """Reviewers to email, from ``PERMIT_NOTIFY_EMAIL`` (comma or semicolon)."""
    raw = getattr(settings, 'PERMIT_NOTIFY_EMAIL', '') or ''
    return [addr.strip() for addr in raw.replace(';', ',').split(',') if addr.strip()]


def notifications_enabled() -> bool:
    """True when there is somewhere to send to."""
    return bool(notify_recipients())


def public_base_url() -> str:
    """Absolute base URL of this backend, for the links inside an email.

    A mailed link is opened by a mail client, which cannot resolve
    ``127.0.0.1``, so ``PERMITS_PUBLIC_BASE_URL`` defaults to the deployed host
    in ``config.settings``. Under DEBUG an explicitly empty value falls back to
    the local server (local development needs no configuration); anything else
    that empties the variable gets tracker links instead of one-click buttons
    rather than a dead localhost URL.
    """
    explicit = (getattr(settings, 'PERMITS_PUBLIC_BASE_URL', '') or '').strip().rstrip('/')
    if explicit:
        return explicit
    if settings.DEBUG:
        return 'http://localhost:8000'
    return ''


def tracker_url(submission: PermitSubmission | None = None) -> str:
    """Link back into the planner UI, focused on one submission when given."""
    base = (getattr(settings, 'PERMITS_TRACKER_URL', '') or '').rstrip('/')
    if not base:
        return ''
    if submission is None:
        return base
    return f'{base}?tab=submissions&submission={submission.id}'


def token_max_age_seconds() -> int:
    return int(getattr(settings, 'PERMIT_ACTION_TOKEN_MAX_AGE', 60 * 60 * 24 * 14))


def token_max_age_days() -> int:
    """Lifetime in whole days, for the email copy."""
    return max(1, round(token_max_age_seconds() / 86400))


# ---------------------------------------------------------------------------
# One-click action tokens
# ---------------------------------------------------------------------------


def action_token(submission: PermitSubmission, action: str) -> str:
    """Signed, expiring token for one (submission, action) pair."""
    if action not in ACTION_STATUSES:
        msg = f'Unknown email action: {action}'
        raise ValueError(msg)
    return signing.dumps({'s': str(submission.id), 'a': action}, salt=TOKEN_SALT, compress=True)


def action_url(submission: PermitSubmission, action: str) -> str:
    """Absolute one-click URL, or '' when no public base URL is configured."""
    base = public_base_url()
    if not base:
        return ''
    return f'{base}{reverse("ftth-permits-submission-action")}?token={action_token(submission, action)}'


def read_action_token(token: str) -> tuple[PermitSubmission, str]:
    """Resolve a token to its (submission, action).

    Raises ValueError with a human-readable reason — an expired or tampered
    link is a 400/410 page, never a stack trace.
    """
    try:
        payload = signing.loads(token, salt=TOKEN_SALT, max_age=token_max_age_seconds())
    except signing.SignatureExpired:
        msg = 'This link has expired — open the submission in the tracker instead.'
        raise ValueError(msg) from None
    except signing.BadSignature:
        msg = 'This link is not valid (it may have been truncated by the mail client).'
        raise ValueError(msg) from None
    submission_id, action = payload.get('s'), payload.get('a')
    if not submission_id or action not in ACTION_STATUSES:
        msg = 'This link is not valid.'
        raise ValueError(msg)
    submission = PermitSubmission.objects.filter(pk=submission_id).first()
    if submission is None:
        msg = 'That submission no longer exists.'
        raise ValueError(msg)
    return submission, action


# ---------------------------------------------------------------------------
# The outbound queue (one email per kind, per transaction)
# ---------------------------------------------------------------------------


@dataclass
class QueuedEvent:
    """One notification waiting for its transaction to commit."""

    kind: str
    submission: PermitSubmission
    payload: dict = field(default_factory=dict)


_state = threading.local()


def _pending() -> list[QueuedEvent]:
    queue = getattr(_state, 'pending', None)
    if queue is None:
        queue = []
        _state.pending = queue
    return queue


def _enqueue(kind: str, submission: PermitSubmission, **payload) -> None:
    _pending().append(QueuedEvent(kind=kind, submission=submission, payload=payload))
    # Post-commit, so a rolled-back submission never emails anybody, and inside
    # one transaction every event lands in a single digest.
    transaction.on_commit(flush)


def reset_queue() -> None:
    """Drop queued events without sending — used by tests."""
    _pending().clear()


def flush() -> dict:
    """Send whatever is queued, grouped into one email per kind.

    Never raises: it runs after a commit has already succeeded, so an email
    problem must not surface as a failed request.
    """
    queue = _pending()
    if not queue:
        return {'sent': 0, 'failed': 0, 'skipped': 0}
    events, queue[:] = list(queue), []
    result = {'sent': 0, 'failed': 0, 'skipped': 0}
    try:
        for kind in (PermitNotification.KIND_SUBMISSION, PermitNotification.KIND_STATUS_CHANGE):
            group = [e for e in events if e.kind == kind]
            if not group:
                continue
            outcome = _send_group(kind, group)
            for key in result:
                result[key] += outcome[key]
    except Exception:
        # A mail failure must never surface as a failed submission.
        logger.exception('Permit notification flush failed')
    return result


def _send_group(kind: str, group: list[QueuedEvent]) -> dict:
    """Build and deliver part of the queue, then log one row per submission."""
    if kind == PermitNotification.KIND_SUBMISSION:
        subject, text, html = _compose_submission_email(group)
    else:
        subject, text, html = _compose_status_email(group)
    return _deliver(
        kind,
        subject,
        text,
        html,
        submissions=[e.submission for e in group],
    )


def queue_submission_created(submissions: list[PermitSubmission], **payload) -> None:
    """Queue the \"sent for approval\" email for a freshly created batch."""
    for submission in submissions:
        _enqueue(PermitNotification.KIND_SUBMISSION, submission, **payload)


def queue_status_change(
    submission: PermitSubmission,
    *,
    from_status: str,
    to_status: str,
    actor: str | None = None,
    reason: str = '',
    sync_source: str = 'manual',
) -> None:
    """Queue a review-outcome email (approved / rejected / re-submitted)."""
    _enqueue(
        PermitNotification.KIND_STATUS_CHANGE,
        submission,
        from_status=from_status,
        to_status=to_status,
        actor=actor,
        reason=reason,
        sync_source=sync_source,
    )


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _deliver(
    kind: str,
    subject: str,
    text: str,
    html: str,
    *,
    submissions: list[PermitSubmission] | None = None,
    log_rows: list[tuple[str, str]] | None = None,
) -> dict:
    """Send one email and record the outcome on ``PermitNotification``.

    ``log_rows`` overrides what gets recorded — reminders log the entities the
    digest covered rather than a submission, since many reminders are about
    permits that were never submitted at all.
    """
    recipients = notify_recipients()
    if not recipients:
        _record(
            kind,
            status=PermitNotification.STATUS_SKIPPED,
            recipients=[],
            subject=subject,
            error='No PERMIT_NOTIFY_EMAIL configured.',
            submissions=submissions,
            log_rows=log_rows,
        )
        logger.info('Permit notification suppressed (no recipient): %s', subject)
        return {'sent': 0, 'failed': 0, 'skipped': 1}

    message = EmailMultiAlternatives(
        subject=subject,
        body=text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    if html:
        message.attach_alternative(html, 'text/html')
    try:
        message.send(fail_silently=False)
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'[:2000]
        _record(
            kind,
            status=PermitNotification.STATUS_FAILED,
            recipients=recipients,
            subject=subject,
            error=error,
            submissions=submissions,
            log_rows=log_rows,
        )
        logger.warning('Permit notification failed (%s): %s', subject, error)
        return {'sent': 0, 'failed': 1, 'skipped': 0}
    _record(
        kind,
        status=PermitNotification.STATUS_SENT,
        recipients=recipients,
        subject=subject,
        submissions=submissions,
        log_rows=log_rows,
    )
    logger.info('Permit notification sent to %s: %s', ', '.join(recipients), subject)
    return {'sent': 1, 'failed': 0, 'skipped': 0}


def _record(
    kind: str,
    *,
    status: str,
    recipients: list[str],
    subject: str,
    submissions: list[PermitSubmission] | None = None,
    error: str = '',
    log_rows: list[tuple[str, str]] | None = None,
) -> None:
    """Write the delivery log. A logging failure must not break the request."""
    rows: list[PermitNotification] = []
    if log_rows is not None:
        rows = [
            PermitNotification(
                kind=kind,
                dedupe_key=dedupe_key,
                project_id=project_id,
                recipients=','.join(recipients)[:512],
                subject=subject[:512],
                status=status,
                error=error,
            )
            for dedupe_key, project_id in log_rows
        ]
    else:
        rows = [
            PermitNotification(
                kind=kind,
                submission_id=submission.pk,
                project_id=str(submission.project_id or ''),
                recipients=','.join(recipients)[:512],
                subject=subject[:512],
                status=status,
                error=error,
            )
            for submission in (submissions or [])
        ]
    if not rows:
        return
    try:
        PermitNotification.objects.bulk_create(rows)
    except Exception:  # pragma: no cover - logging is best-effort by design
        logger.exception('Could not record permit notification')


# ---------------------------------------------------------------------------
# Message bodies
# ---------------------------------------------------------------------------


def _timestamp(value) -> str:
    return timezone.localtime(value).strftime('%Y-%m-%d %H:%M %Z') if value else '—'


def _line(submission: PermitSubmission) -> str:
    """One human-readable identity for a submission."""
    authority_obj = submission.authority
    authority = authority_obj.name if authority_obj is not None else 'no authority'
    return f'{submission.label or submission.permit_type} — {authority}'


def _sections(submission: PermitSubmission) -> int:
    return submission.permit_rows.count()


def _action_links_text(submission: PermitSubmission) -> list[str]:
    lines = []
    for action in (ACTION_APPROVE, ACTION_REJECT):
        url = action_url(submission, action)
        if url:
            lines.append(f'   {ACTION_LABELS[action]:<7}: {url}')
    tracker = tracker_url(submission)
    if tracker:
        lines.append(f'   Tracker: {tracker}')
    return lines


def _action_buttons_html(submission: PermitSubmission) -> str:
    buttons = []
    colours = {ACTION_APPROVE: '#15803d', ACTION_REJECT: '#b91c1c'}
    for action in (ACTION_APPROVE, ACTION_REJECT):
        url = action_url(submission, action)
        if not url:
            continue
        buttons.append(
            f'<a href="{escape(url)}" style="display:inline-block;padding:9px 18px;'
            f'margin:0 8px 0 0;background:{colours[action]};color:#ffffff;'
            f'text-decoration:none;border-radius:5px;font-weight:600;font-size:14px">'
            f'{ACTION_LABELS[action]}</a>'
        )
    tracker = tracker_url(submission)
    if tracker:
        buttons.append(
            f'<a href="{escape(tracker)}" style="display:inline-block;padding:9px 18px;'
            f'background:#e5e7eb;color:#111827;text-decoration:none;border-radius:5px;'
            f'font-size:14px">Open in tracker</a>'
        )
    if not buttons:
        return ''
    return f'<div style="margin:12px 0 4px">{"".join(buttons)}</div>'


def _wrap_html(title: str, intro: str, blocks: list[str], footer: str) -> str:
    """Minimal inline-styled HTML shell — mail clients strip <style> blocks."""
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:640px;color:#111827;font-size:14px;line-height:1.5">'
        f'<h2 style="font-size:18px;margin:0 0 8px">{escape(title)}</h2>'
        f'<p style="margin:0 0 16px;color:#374151">{escape(intro)}</p>'
        + ''.join(blocks)
        + f'<p style="margin:20px 0 0;padding-top:12px;border-top:1px solid #e5e7eb;'
        f'color:#6b7280;font-size:12px">{footer}</p></div>'
    )


def _submission_block_text(index: int, event: QueuedEvent) -> list[str]:
    submission = event.submission
    lines = [
        f'{index}. {_line(submission)}',
        f'   {_sections(submission)} street section(s) · submission {submission.id}',
    ]
    if submission.reference:
        lines.append(f'   reference {submission.reference}')
    lines.extend(_action_links_text(submission))
    return lines


def _compose_submission_email(group: list[QueuedEvent]) -> tuple[str, str, str]:
    first = group[0].submission
    count = len(group)
    # A project name is optional on FtthProject, so the suffix is added only
    # when there is a name to add — otherwise the subject ends in a bare dash.
    project = (first.project.name or '').strip() if first.project_id else ''
    subject = f'[Permits] {count} application{"s" if count != 1 else ""} sent for approval'
    if project:
        subject += f' — {project}'
    project_label = project or '—'
    reference = group[0].payload.get('reference') or ''
    package_version = first.package_version
    actor = group[0].payload.get('actor') or '—'

    head = [
        f'{count} permit application{"s have" if count != 1 else " has"} been sent for approval.',
        '',
        f'Project   : {project_label}',
        f'Reference : {reference or "—"}',
        f'Package   : {f"v{package_version}" if package_version else "—"}',
        f'Submitted : {_timestamp(first.submission_date)}',
        f'Sent by   : {actor}',
        '',
    ]
    body: list[str] = []
    for index, event in enumerate(group, start=1):
        body.extend(_submission_block_text(index, event))
        body.append('')

    has_buttons = bool(public_base_url())
    footer = (
        f'Approving straight from this email records the approval with no conditions '
        f'and no expiry date — open the tracker first if you need either. '
        f'Links are valid for {token_max_age_days()} days. '
        if has_buttons
        else 'Configure PERMITS_PUBLIC_BASE_URL on the backend to get one-click '
        'Approve/Reject buttons in this email.\n'
    )
    tracker = tracker_url()
    text = '\n'.join(head + body)
    if tracker:
        text += f'\nAll submissions: {tracker}\n'
    text += f'\n{footer}'

    blocks = []
    for index, event in enumerate(group, start=1):
        submission = event.submission
        blocks.append(
            '<div style="border:1px solid #e5e7eb;border-radius:6px;padding:12px 14px;'
            f'margin:0 0 12px">'
            f'<div style="font-weight:600">{index}. {escape(_line(submission))}</div>'
            f'<div style="color:#6b7280;margin-top:2px">{_sections(submission)} street '
            f'section(s) · submission {escape(str(submission.id))}'
            + (f' · reference {escape(submission.reference)}' if submission.reference else '')
            + '</div>'
            + _action_buttons_html(submission)
            + '</div>'
        )
    meta = (
        f'Project {escape(project_label)} · reference {escape(reference) or "—"} · '
        f'package {f"v{package_version}" if package_version else "—"} · '
        f'submitted {escape(_timestamp(first.submission_date))} by {escape(str(actor))}'
    )
    html = _wrap_html(subject.replace('[Permits] ', ''), meta, blocks, escape(footer.strip()))
    return subject, text, html


_STATUS_PHRASES = {
    PermitSubmission.STATUS_APPROVED: 'approved',
    PermitSubmission.STATUS_REJECTED: 'rejected',
    PermitSubmission.STATUS_SUBMITTED: 're-submitted',
    PermitSubmission.STATUS_UNDER_REVIEW: 'acknowledged — under review',
    PermitSubmission.STATUS_CLOSED: 'closed',
}


def _compose_status_email(group: list[QueuedEvent]) -> tuple[str, str, str]:
    first = group[0]
    first_status = str(first.payload.get('to_status') or '')
    first_phrase = _STATUS_PHRASES.get(first_status, first_status or 'updated')
    subject = f'[Permits] {first_phrase.capitalize()} — {_line(first.submission)}'
    if len(group) > 1:
        subject += f' (+{len(group) - 1} more)'

    body: list[str] = []
    blocks = []
    for index, event in enumerate(group, start=1):
        submission = event.submission
        to_status = str(event.payload.get('to_status') or '')
        phrase = _STATUS_PHRASES.get(to_status, to_status or 'updated')
        body.append(f'{index}. {phrase.capitalize()} — {_line(submission)}')
        facts = [f'{_sections(submission)} street section(s) · submission {submission.id}']
        if submission.reference:
            facts.append(f'reference {submission.reference}')
        if event.payload.get('actor'):
            facts.append(f'by {event.payload["actor"]}')
        if event.payload.get('sync_source') and event.payload['sync_source'] != 'manual':
            facts.append(f'source {event.payload["sync_source"]}')
        if submission.conditions:
            facts.append(f'conditions: {submission.conditions}')
        if submission.expiry_date:
            facts.append(f'expires {_timestamp(submission.expiry_date)}')
        if event.payload.get('reason'):
            facts.append(f'reason: {event.payload["reason"]}')
        body.append('   ' + ' · '.join(str(f) for f in facts))
        tracker = tracker_url(submission)
        if tracker:
            body.append(f'   Tracker: {tracker}')
        body.append('')

        blocks.append(
            '<div style="border:1px solid #e5e7eb;border-radius:6px;padding:12px 14px;'
            'margin:0 0 12px">'
            f'<div style="font-weight:600">{escape(str(phrase).capitalize())} — '
            f'{escape(_line(submission))}</div>'
            f'<div style="color:#6b7280;margin-top:2px">{escape(" · ".join(facts))}</div>'
            + (
                f'<div style="margin-top:8px;color:#b91c1c">Reason: '
                f'{escape(str(event.payload["reason"]))}</div>'
                if event.payload.get('reason')
                else ''
            )
            + (
                '<div style="margin-top:10px">'
                f'<a href="{escape(tracker_url(submission))}" style="display:inline-block;'
                'padding:8px 16px;background:#e5e7eb;color:#111827;text-decoration:none;'
                'border-radius:5px;font-size:14px">Open in tracker</a></div>'
                if tracker_url(submission)
                else ''
            )
            + '</div>'
        )
    review_note = ''
    if first_status == PermitSubmission.STATUS_REJECTED:
        review_note = 'Re-submit from the tracker when the objection is addressed '
        review_note += '(that opens a new revision and keeps the audit trail).'
    elif first_status == PermitSubmission.STATUS_APPROVED:
        review_note = 'Set conditions or an expiry date in the tracker if the approval has either.'
    footer = (
        'This is the review state machine, recorded on the permit audit trail. ' + review_note
    ).strip()
    html = _wrap_html(subject.replace('[Permits] ', ''), '', blocks, escape(footer))
    return subject, '\n'.join(body) + '\n' + footer + '\n', html


# ---------------------------------------------------------------------------
# Renewal reminders (check_permit_expiries --email)
# ---------------------------------------------------------------------------


def reminder_dedupe_key(kind_of_entity: str, entity_id: str, bucket: str, expiry_date) -> str:
    """Identity of one reminder, so a nightly cron does not repeat itself.

    The expiry date is part of the key: a renewed permit has a new date and is
    therefore announced again even for a bucket already seen.
    """
    stamp = expiry_date.date().isoformat() if hasattr(expiry_date, 'date') else str(expiry_date)
    return f'{kind_of_entity}:{entity_id}:{bucket}:{stamp}'[:160]


def already_reminded(keys: list[str]) -> set[str]:
    """Which of ``keys`` have already been emailed (any status but failed)."""
    if not keys:
        return set()
    return set(
        PermitNotification.objects.filter(dedupe_key__in=keys)
        .exclude(status=PermitNotification.STATUS_FAILED)
        .values_list('dedupe_key', flat=True)
    )


def send_expiry_reminders(
    *,
    project_id: str | None = None,
    threshold_days: int = 30,
    now=None,
    include_ok: bool = False,
    force: bool = False,
) -> dict:
    """Email a renewal digest for everything inside the threshold.

    Returns ``{total, reminded, already_sent, buckets}``. ``force`` re-sends
    rows that were already announced (the digest is otherwise deduped per
    bucket, so a nightly cron tells you once per bucket instead of every night).
    """
    # Imported here: permits.expiry pulls in the analysis layer, and this module
    # is imported by permits.submissions on the submission hot path.
    from .expiry import get_expiring_permits, get_expiring_submissions

    now = now or timezone.now()
    permit_rows = get_expiring_permits(
        project_id, threshold_days=threshold_days, include_ok=include_ok, now=now
    )
    submission_rows = get_expiring_submissions(
        project_id, threshold_days=threshold_days, include_ok=include_ok, now=now
    )

    candidates: list[tuple[str, str, dict]] = []
    for row in permit_rows:
        candidates.append(
            (
                reminder_dedupe_key('permit', row['permit_id'], row['bucket'], row['expiry_date']),
                str(row.get('project_id') or ''),
                {'entity': 'permit', **row},
            )
        )
    for row in submission_rows:
        candidates.append(
            (
                reminder_dedupe_key(
                    'submission', row['submission_id'], row['bucket'], row['expiry_date']
                ),
                str(row.get('project_id') or ''),
                {'entity': 'submission', **row},
            )
        )

    keys = [key for key, _, _ in candidates]
    done = set() if force else already_reminded(keys)
    fresh = [c for c in candidates if c[0] not in done]
    result = {
        'total': len(candidates),
        'reminded': len(fresh),
        'already_sent': len(candidates) - len(fresh),
        'buckets': _bucket_counts(fresh),
        'delivery': {'sent': 0, 'failed': 0, 'skipped': 0},
    }
    if not fresh:
        return result

    subject, text, html = _compose_expiry_email(fresh, threshold_days=threshold_days, now=now)
    scope = f'project {project_id}' if project_id else 'all projects'
    subject = f'{subject} — {scope}'
    result['delivery'] = _deliver(
        PermitNotification.KIND_EXPIRY_REMINDER,
        subject,
        text,
        html,
        log_rows=[(key, pid) for key, pid, _ in fresh],
    )
    return result


def _bucket_counts(rows: list[tuple[str, str, dict]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, _, row in rows:
        bucket = row.get('bucket') or 'unknown'
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _expiry_line(row: dict) -> str:
    if row.get('entity') == 'submission':
        return (
            f"{row['permit_type']} — {row.get('permit_group') or row.get('label') or 'submission'}"
            f" (ref {row.get('reference') or '—'})"
        )
    return (
        f"{row['permit_type']} — "
        f"{row.get('permit_group') or row.get('route_section') or 'permit'}"
    )


def _days_until(row: tuple[str, str, dict]) -> int:
    """Sort key for the expiry digest: rows without a count sort last."""
    value = row[2].get('days_until')
    return value if isinstance(value, int) else 9999


def _compose_expiry_email(
    rows: list[tuple[str, str, dict]], *, threshold_days: int, now
) -> tuple[str, str, str]:
    counts = _bucket_counts(rows)
    expired = counts.get('expired', 0)
    subject = f'[Permits] {len(rows)} renewal reminder(s) within {threshold_days} days'
    if expired:
        subject = f'[Permits] {expired} permit(s) EXPIRED + {len(rows) - expired} expiring'

    ordered = sorted(rows, key=_days_until)
    lines = [
        f'{len(rows)} permit row(s) need attention (threshold {threshold_days} days, as of {_timestamp(now)}).',
        '',
    ]
    for _, _, row in ordered:
        days = row.get('days_until')
        when = 'expired' if row.get('bucket') == 'expired' else f'{days}d left'
        lines.append(
            f'  [{when:>9}] {_expiry_line(row)} — project {str(row.get("project_id"))[:8]}'
        )
        lines.append(f'              expiry {row.get("expiry_date") or "—"}')
    tracker = tracker_url()
    if tracker:
        lines.append('')
        lines.append(f'Renewals: {tracker}')
    lines.append('')
    lines.append(
        'Sent by `manage.py check_permit_expiries --email`. Each bucket is announced once; '
        'the next reminder for a row arrives when it reaches a shorter bucket or after a renewal.'
    )
    text = '\n'.join(lines)

    blocks = []
    for _, _, row in ordered:
        days = row.get('days_until')
        tone = '#b91c1c' if row.get('bucket') == 'expired' else '#b45309'
        when = 'expired' if row.get('bucket') == 'expired' else f'{days}d left'
        blocks.append(
            '<div style="border-left:3px solid '
            + tone
            + ';padding:6px 0 6px 12px;margin:0 0 10px">'
            f'<div style="font-weight:600">{escape(when)} — {escape(_expiry_line(row))}</div>'
            f'<div style="color:#6b7280">project {escape(str(row.get("project_id"))[:8])} · '
            f'expiry {escape(str(row.get("expiry_date") or "—"))}</div></div>'
        )
    html = _wrap_html(
        f'{len(rows)} renewal reminder(s)',
        f'Threshold {threshold_days} days · as of {_timestamp(now)}',
        blocks,
        'Sent by check_permit_expiries --email. Each bucket is announced once per permit.',
    )
    return subject, text, html


def notification_feed(limit: int = 100) -> list[dict]:
    """Recent delivery log rows — the \"did it actually go out?\" view."""
    rows = PermitNotification.objects.all()[: max(1, min(limit, 500))]
    return [
        {
            'id': str(row.id),
            'kind': row.kind,
            'status': row.status,
            'submission_id': str(row.submission_id) if row.submission_id else None,
            'project_id': row.project_id,
            'recipients': [a for a in (row.recipients or '').split(',') if a],
            'subject': row.subject,
            'error': row.error,
            'dedupe_key': row.dedupe_key,
            'created_at': row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]
