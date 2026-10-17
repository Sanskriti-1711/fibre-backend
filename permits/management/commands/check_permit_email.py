"""Diagnose the permit review mailbox, and optionally prove it works.

``python manage.py check_permit_email [--send] [--to <addr>] [--json]``

The notification layer degrades instead of failing — no recipient, no
credential, or an unreachable SMTP server all leave the submission untouched and
record the attempt on ``PermitNotification`` later. That is the right behaviour
and a bad thing to debug: from the outside "nothing arrived" looks identical in
every one of those cases, and a submission that quietly emails nobody is worse
than one that errors.

This command is the answer. Without ``--send`` it prints the configuration the
running process actually resolved (not the one in the file — a missing restart
or an unexported variable shows up here) and names every misconfiguration it
recognises, including the two Gmail refusals that cost the most time: an account
password where an app password is needed, and a ``From:`` that is not the
authenticated account.

``--send`` mails a test message to the reviewer and reports the transport's own
answer, so the mailbox is proven rather than assumed.

Exit codes: 0 the configuration resolves (and, with ``--send``, delivered);
2 ``--send`` was attempted and the transport refused it; 3 the configuration is
incomplete — nothing was sent, or it would only be printed.

No permit or submission is touched, and no ``PermitNotification`` row is written
— this tests the mailbox, not the review flow.
"""

from __future__ import annotations

import json
import sys

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand

from permits import notifications

# What the settings must resolve to for a mail to have a chance.
PROBLEM = 'problem'
WARNING = 'warning'


class Command(BaseCommand):
    help = 'Report the permit review email configuration, and optionally send a test message.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--send',
            action='store_true',
            help='Send a real test message to the configured recipient(s)',
        )
        parser.add_argument(
            '--to',
            dest='to',
            default='',
            help='Override the recipient for the test message (comma-separated)',
        )
        parser.add_argument('--json', action='store_true', help='Emit JSON instead of text')

    def handle(self, *args, **options):
        recipients = self._recipients(options.get('to'))
        report = self._report(recipients)
        delivery = None
        if options.get('send'):
            delivery = self._send(recipients)
            report['delivery'] = delivery

        if options.get('json'):
            self.stdout.write(json.dumps(report, indent=2))
        else:
            self._print_text(report)

        if delivery is not None and delivery['status'] == 'failed':
            sys.exit(2)
        if any(item['level'] == PROBLEM for item in report['findings']):
            sys.exit(3)
        # 0 — the configuration resolves (and delivered, when asked)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _recipients(self, override: str) -> list[str]:
        if override:
            return [addr.strip() for addr in override.replace(';', ',').split(',') if addr.strip()]
        return notifications.notify_recipients()

    def _report(self, recipients: list[str]) -> dict:
        backend = settings.EMAIL_BACKEND or ''
        user = (settings.EMAIL_HOST_USER or '').strip()
        password = settings.EMAIL_HOST_PASSWORD or ''
        sender = (settings.DEFAULT_FROM_EMAIL or '').strip()
        host = settings.EMAIL_HOST or ''
        findings: list[dict] = []

        if not recipients:
            findings.append(
                {
                    'level': PROBLEM,
                    'detail': 'PERMIT_NOTIFY_EMAIL is empty — nothing will ever be sent. '
                    'Set it to the reviewer address.',
                }
            )
        if 'console' in backend or 'locmem' in backend:
            findings.append(
                {
                    'level': PROBLEM,
                    'detail': f'EMAIL_BACKEND is {backend} — messages are written to this log, not '
                    'delivered. Set EMAIL_HOST_USER + EMAIL_HOST_PASSWORD and leave '
                    'EMAIL_BACKEND unset (it switches to SMTP once a user is configured).',
                }
            )
        elif not user:
            findings.append(
                {
                    'level': PROBLEM,
                    'detail': 'EMAIL_HOST_USER is empty while EMAIL_BACKEND is SMTP — the server '
                    'will refuse the login.',
                }
            )
        elif not password:
            findings.append(
                {
                    'level': PROBLEM,
                    'detail': f'EMAIL_HOST_PASSWORD is empty for {user} — expect the SMTP server to '
                    'refuse the send ("530 Authentication Required" from Gmail).',
                }
            )

        if host.endswith('gmail.com'):
            if password and not _looks_like_app_password(password):
                findings.append(
                    {
                        # A problem, not a warning. Google issues app passwords as 16
                        # lowercase letters and nothing else, so on smtp.gmail.com a
                        # credential of any other shape cannot authenticate — verified
                        # live: a 12-character account password reaches Gmail and is
                        # refused (`SMTPServerDisconnected: Connection unexpectedly
                        # closed`). Grading that a warning would let a preflight exit 0
                        # on a mailbox that provably cannot send.
                        'level': PROBLEM,
                        'detail': 'Gmail refuses an account password for SMTP, and its app '
                        'passwords are 16 lowercase letters — this value is not that shape. '
                        'Create one at https://myaccount.google.com/apppasswords (2-step '
                        'verification must be on); it is usually shown in four groups of '
                        'four, and it is the only value that works here.',
                    }
                )
            if sender and user and sender.lower() != user.lower():
                findings.append(
                    {
                        # A problem, not a warning: Gmail refuses this outright, so
                        # the send cannot succeed however the rest is set up.
                        'level': PROBLEM,
                        'detail': f'DEFAULT_FROM_EMAIL ({sender}) differs from EMAIL_HOST_USER '
                        f'({user}) — Gmail refuses to send as an address that is not the '
                        'authenticated account (or a verified alias).',
                    }
                )

        if not notifications.public_base_url():
            findings.append(
                {
                    'level': WARNING,
                    'detail': 'PERMITS_PUBLIC_BASE_URL is empty outside DEBUG, so the email will '
                    'carry tracker links instead of one-click Approve/Reject buttons — a mail '
                    "client cannot reach localhost. Set it to this backend's public URL.",
                }
            )

        return {
            'recipients': recipients,
            'notifications_enabled': bool(recipients),
            'backend': backend,
            'host': host,
            'port': settings.EMAIL_PORT,
            'use_tls': settings.EMAIL_USE_TLS,
            'timeout': getattr(settings, 'EMAIL_TIMEOUT', None),
            'username': user,
            'password_set': bool(password),
            'from_email': sender,
            'tracker_url': notifications.tracker_url(),
            'public_base_url': notifications.public_base_url(),
            'token_max_age_days': notifications.token_max_age_days(),
            'findings': findings,
        }

    def _print_text(self, report: dict) -> None:
        def out(line: str) -> None:
            self.stdout.write(_console(line))

        out('Permit review email — configuration as this process resolved it')
        out(f"  Recipients  : {', '.join(report['recipients']) or '(none)'}")
        out(f"  Backend     : {report['backend']}")
        out(
            f"  SMTP server : {report['host']}:{report['port']} "
            f"(TLS {report['use_tls']}, timeout {report['timeout']}s)"
        )
        out(
            f"  Login       : {report['username'] or '(none)'} · "
            f"password {'set' if report['password_set'] else 'EMPTY'}"
        )
        out(f"  From        : {report['from_email']}")
        out(f"  Tracker     : {report['tracker_url'] or '(none)'}")
        out(f"  Public base : {report['public_base_url'] or '(none — no one-click buttons)'}")
        out(f"  Link life   : {report['token_max_age_days']} days")
        out('')

        if not report['findings']:
            out('  No configuration problems found.')
        for item in report['findings']:
            marker = 'PROBLEM' if item['level'] == PROBLEM else 'WARNING'
            out(f'  [{marker}] {item["detail"]}')

        delivery = report.get('delivery')
        if not delivery:
            return
        out('')
        if delivery['status'] == 'sent':
            out(f"  Test message sent to {', '.join(delivery['recipients'])}")
        elif delivery['status'] == 'skipped':
            out(f"  Test message NOT sent — {delivery['detail']}")
        else:
            out(f"  Test message FAILED — {delivery['detail']}")

    # ------------------------------------------------------------------
    # Test send
    # ------------------------------------------------------------------

    def _send(self, recipients: list[str]) -> dict:
        if not recipients:
            return {'status': 'skipped', 'detail': 'No recipient configured.'}
        subject = '[Permits] Test message from the permit review mailbox'
        text = (
            'This is a test from `manage.py check_permit_email --send`.\n\n'
            'The mailbox is configured correctly, so the permit tracker will email you here\n'
            'when permits are sent for approval and when an application is approved, rejected\n'
            'or re-submitted. No permit was touched by this message.\n\n'
            f'Tracker:  {notifications.tracker_url() or "(not configured)"}\n'
            f'Backend:  {notifications.public_base_url() or "(no public URL configured)"}\n'
        )
        html = (
            '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
            'max-width:560px;color:#111827;font-size:14px;line-height:1.6">'
            '<h2 style="font-size:17px;margin:0 0 10px">Permit review mailbox works</h2>'
            '<p>This is a test from <code>manage.py check_permit_email --send</code>. The '
            'permit tracker will email you here when permits are sent for approval, and when '
            'an application is approved, rejected or re-submitted.</p>'
            '<p style="color:#6b7280;font-size:13px">No permit was touched by this message.</p>'
            '</div>'
        )
        message = EmailMultiAlternatives(
            subject=subject,
            body=text,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipients,
        )
        message.attach_alternative(html, 'text/html')
        try:
            message.send(fail_silently=False)
        except Exception as exc:
            return {
                'status': 'failed',
                'detail': f'{type(exc).__name__}: {exc}'[:2000],
                'recipients': recipients,
            }
        return {'status': 'sent', 'detail': '', 'recipients': recipients}


def _console(text: str) -> str:
    """Keep the report readable where stdout is cp1252 (the Windows console).

    The text is only ASCII in the code for the sake of a terminal that cannot
    encode an em dash — the JSON output and the emails keep the real characters.
    """
    return text.replace('\u2014', '-').replace('\u00b7', '|')


def _looks_like_app_password(value: str) -> bool:
    """Gmail app passwords are 16 lowercase letters, spaces/grouping aside."""
    compact = value.replace(' ', '')
    return len(compact) == 16 and compact.isalpha() and compact.islower()
