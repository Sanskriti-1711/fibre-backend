"""Emailed review actions — the public, token-gated Approve/Reject endpoints.

An emailed button cannot carry a session, so these views are the only permit
endpoints reachable without JWT auth. What authorises the call is a signed,
expiring token (``permits.notifications.action_token``) naming one submission
and one action; nothing else about the request is trusted.

The split between GET and POST is deliberate and is not a formality:

* ``GET`` renders a confirmation page and **mutates nothing**. Mail providers
  and corporate gateways fetch the links inside an email to scan them, and a
  GET that approved a permit would let a link scanner sign off an application
  nobody has read;
* the confirmation page posts the same token back, which is what applies the
  decision — automatically on load, so a reviewer still spends exactly one
  click, but from a browser that actually ran the page.

The action itself goes through ``permits.submissions.apply_review_action``, so
it obeys ``ALLOWED_TRANSITIONS`` like any other review and lands on the permit
audit trail with ``sync_source='email'``.
"""

from __future__ import annotations

import logging

from django.http import HttpResponse, JsonResponse
from django.utils.html import escape
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from . import notifications
from .models import PermitNotification, PermitSubmission
from .submissions import apply_review_action

logger = logging.getLogger(__name__)

_CARD = (
    'max-width:560px;margin:0 auto;background:#ffffff;border:1px solid #e5e7eb;'
    'border-radius:10px;padding:28px 30px'
)
_BODY = 'margin:0;padding:40px 16px;background:#f9fafb;color:#111827;'
_FONT = 'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif'


def _page(title: str, body: str, status: int = 200, script: str = '') -> HttpResponse:
    """One self-contained page — this backend has no template directory."""
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(title)}</title><style>'
        f'body{{{_BODY}{_FONT}}} .card{{{_CARD}}} '
        'h1{font-size:19px;margin:0 0 10px} p{margin:0 0 12px;line-height:1.55} '
        '.meta{color:#6b7280;font-size:13px} .note{color:#6b7280;font-size:13px} '
        'a.track{color:#1d4ed8;font-size:13px} '
        f'button{{border:0;border-radius:6px;color:#fff;font-weight:600;'
        f'font-size:15px;padding:11px 22px;cursor:pointer;{_FONT}}}'
        '</style></head>'
        f'<body><div class="card">{body}</div>'
        + (f'<script>{script}</script>' if script else '')
        + '</body></html>'
    )
    return HttpResponse(html, status=status, content_type='text/html; charset=utf-8')


def _facts(submission: PermitSubmission) -> str:
    """Project · authority · size — what a reviewer needs to recognise it."""
    authority_obj = submission.authority
    authority = authority_obj.name if authority_obj is not None else 'no authority recorded'
    project = submission.project.name if submission.project_id else '—'
    facts = [
        escape(project),
        escape(authority),
        f'{submission.permit_rows.count()} street section(s)',
        f'status {escape(submission.status)}',
    ]
    if submission.reference:
        facts.append(f'reference {escape(submission.reference)}')
    return ' · '.join(facts)


def _tracker_link(submission: PermitSubmission) -> str:
    tracker = notifications.tracker_url(submission)
    if not tracker:
        return ''
    return (
        f'<p class="note" style="margin-top:18px"><a class="track" href="{escape(tracker)}">'
        'Open in the tracker</a></p>'
    )


class PermitSubmissionActionView(APIView):
    """GET renders the confirmation page; POST applies it. Both take ``?token=``."""

    # Public by design — the signed token is the credential. See the module
    # docstring for why GET never mutates.
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request):
        return self._render(request, apply=False)

    def post(self, request):
        return self._render(request, apply=True)

    def _render(self, request, *, apply: bool):
        token = (request.GET.get('token') or request.data.get('token') or '').strip()
        if not token:
            return self._error('This link is missing its token.', 400)
        try:
            submission, action = notifications.read_action_token(token)
        except ValueError as exc:
            expired = 'expired' in str(exc).lower()
            return self._error(str(exc), 410 if expired else 400)

        if not apply:
            return self._confirm(submission, action, token)
        try:
            target, path = apply_review_action(
                submission,
                action,
                actor_email=(notifications.notify_recipients() or ['email link'])[0],
            )
        except ValueError as exc:
            # Illegal or already-applied: 409 with the state machine's own words.
            return self._error(str(exc), 409)
        return self._result(submission, action, target, path)

    def _confirm(self, submission: PermitSubmission, action: str, token: str) -> HttpResponse:
        label = notifications.ACTION_LABELS.get(action, action)
        colour = '#15803d' if action == notifications.ACTION_APPROVE else '#b91c1c'
        body = (
            f'<h1>{escape(label)} this application?</h1>'
            f'<p class="meta">{_facts(submission)}</p>'
            '<p class="note">Nothing has been changed yet. Confirming applies the decision to '
            'every street section in the application and records who it was on the permit '
            'audit trail.</p>'
            '<form method="post" id="review-action">'
            f'<input type="hidden" name="token" value="{escape(token)}">'
            f'<button type="submit" style="background:{colour}">{escape(label)}</button>'
            '</form>'
            '<noscript><p class="note">JavaScript is off, so confirm with the button above.</p>'
            '</noscript>' + _tracker_link(submission)
        )
        # A human still spends one click: the page confirms itself. A link
        # scanner only ever ran the GET and changed nothing.
        return _page(
            f'{label} application',
            body,
            script='document.getElementById("review-action").submit();',
        )

    def _result(
        self,
        submission: PermitSubmission,
        action: str,
        target: str,
        path: list[str],
    ) -> HttpResponse:
        already = not path
        verb = notifications.ACTION_VERBS.get(action, 'updated')
        intro: str
        if already:
            heading = f'Already {verb}'
            intro = escape(
                f'This application was already {target}; nothing was changed. '
                'Use the tracker if it needs amending.'
            )
        else:
            heading = f'Application {verb}'
            intro = f'The application is now {escape(target)}.'
            if len(path) > 1:
                intro += escape(
                    ' It moved through '
                    + ' → '.join(path)
                    + ' because the workflow records the authority acknowledgment before a '
                    'decision.'
                )
        follow_up = ''
        if action == notifications.ACTION_APPROVE:
            follow_up = (
                '<p class="note">An email approval carries no conditions and no expiry date. '
                'Set either in the tracker if this approval has them — the renewal reminder '
                'can only warn about an expiry it knows about.</p>'
            )
        body = (
            f'<h1>{escape(heading)}</h1>'
            f'<p>{intro}</p>'
            f'<p class="meta">{_facts(submission)}</p>' + follow_up + _tracker_link(submission)
        )
        return _page(heading, body)

    def _error(self, message: str, status: int) -> HttpResponse:
        body = (
            '<h1>That link did not work</h1>'
            f'<p>{escape(message)}</p>'
            '<p class="note">Approve or reject from the tracker instead — the submission list '
            'shows the same applications.</p>'
            f'<p class="note"><a class="track" href="{escape(notifications.tracker_url())}">'
            'Open the permits tracker</a></p>'
        )
        return _page('Permit action', body, status=status)


class PermitNotificationFeedView(APIView):
    """``GET /api/ftth/permits/notifications/`` — the email delivery log.

    Email is fire-and-forget, so this is how a reviewer answers \"did anyone
    actually get told?\" without reading the server log. ``?limit=`` caps the
    page (default 100, max 500).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            limit = int((request.GET.get('limit') or '100').strip())
        except ValueError:
            return JsonResponse({'detail': 'limit must be an integer.'}, status=400)
        rows = notifications.notification_feed(limit=limit)
        return JsonResponse(
            {
                'total': len(rows),
                'recipients': notifications.notify_recipients(),
                'notifications': rows,
                'kinds': [choice[0] for choice in PermitNotification.KIND_CHOICES],
            }
        )
