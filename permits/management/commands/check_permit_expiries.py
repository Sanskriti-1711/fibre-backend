"""Renewal reminder report for permits/submissions (P21b).

``python manage.py check_permit_expiries [--project <id>] [--days 30] [--json]
[--email] [--force]``

Scans ``PermitMatrix`` + ``PermitSubmission`` expiry_dates, buckets by
30/14/7/1/expired and prints a human-readable report. Exit code 2 when any
expired rows are found (so a nightly cron can alert), 1 when something is due
within the threshold but nothing is yet expired, 3 when ``--email`` could not
reach anybody, 0 when all clear or no expiries are set.

No permit status is mutated — this is a read-only audit. ``--email`` adds the
notification leg (``permits.notifications.send_expiry_reminders``): one digest
to ``PERMIT_NOTIFY_EMAIL``, deduped per (entity, bucket, expiry date) so a
nightly cron announces each bucket once instead of every night. ``--force``
re-sends already-announced rows.
"""

from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand
from django.utils import timezone

from permits.expiry import BUCKET_LABEL, summary
from permits.notifications import send_expiry_reminders


class Command(BaseCommand):
    help = 'Check permit/submission expiry dates and print renewal reminders (P21b).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--project', dest='project_id', default=None, help='Limit to one project_id'
        )
        parser.add_argument('--days', type=int, default=30, help='Threshold in days (default: 30)')
        parser.add_argument('--json', action='store_true', help='Emit JSON instead of a text table')
        parser.add_argument(
            '--email',
            action='store_true',
            help='Also email the renewal digest to PERMIT_NOTIFY_EMAIL',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='With --email, re-send reminders that were already announced',
        )

    def handle(self, *args, **options):
        project_id = options.get('project_id')
        threshold = int(options.get('days') or 30)
        as_json = bool(options.get('json'))

        now = timezone.now()
        data = summary(project_id=project_id, threshold_days=threshold, now=now)
        email_result = None
        if options.get('email'):
            email_result = send_expiry_reminders(
                project_id=project_id,
                threshold_days=threshold,
                now=now,
                force=bool(options.get('force')),
            )
            data['email'] = email_result

        if as_json:
            self.stdout.write(json.dumps(data, indent=2, default=str))
        else:
            self._print_text(data)
            if email_result is not None:
                self._print_email(email_result)

        # Exit signal for cron/alerting.
        if email_result is not None and email_result['delivery']['failed']:
            # A reminder nobody received is an alert of its own.
            sys.exit(3)
        if (data['expiring_permit_counts'].get('expired') or 0) + (
            data['expiring_submission_counts'].get('expired') or 0
        ) > 0:
            sys.exit(2)
        if data['total_expiring'] > 0:
            sys.exit(1)
        # 0 = all clear / no expiries set

    def _print_email(self, result: dict) -> None:
        delivery = result['delivery']
        self.stdout.write(
            f"  Email:     {result['reminded']} of {result['total']} reminder(s) pending "
            f"({result['already_sent']} already announced)"
        )
        if result['reminded']:
            self.stdout.write(
                f"             sent {delivery['sent']} · failed {delivery['failed']} · "
                f"skipped {delivery['skipped']} (see PermitNotification for detail)"
            )
            buckets = ', '.join(f'{k}={v}' for k, v in sorted(result['buckets'].items()))
            self.stdout.write(f'             buckets: {buckets}')

    def _print_text(self, data: dict) -> None:
        scope = f"project {data['project_id']}" if data['project_id'] else 'all projects'
        self.stdout.write(
            f"Permit expiry check — {scope} — threshold {data['threshold_days']} days — {data['now']}"
        )
        self.stdout.write(
            f"  Permits with expiry: {data['permits_with_expiry']}  Submissions with expiry: {data['submissions_with_expiry']}  Total expiring: {data['total_expiring']}"
        )
        if data['total_expiring'] == 0:
            if data['total_with_expiry'] == 0:
                self.stdout.write(
                    '  No expiry dates set — nothing to renew yet (set expiry_date on APPROVED submissions/permits).'
                )
            else:
                self.stdout.write('  No expiries within threshold — all clear.')
            return

        def fmt_bucket(counts: dict, label: str) -> str:
            parts = []
            for b in ('expired', 'due_1d', 'due_7d', 'due_14d', 'due_30d'):
                if counts.get(b):
                    parts.append(f'{counts[b]} × {BUCKET_LABEL[b]}')
            return ', '.join(parts) if parts else '—'

        self.stdout.write(f"  Permits:    {fmt_bucket(data['expiring_permit_counts'], 'permits')}")
        self.stdout.write(
            f"  Submissions:{fmt_bucket(data['expiring_submission_counts'], 'submissions')}"
        )
        self.stdout.write('')

        if data['expiring_permits']:
            self.stdout.write('  Permits expiring:')
            for r in data['expiring_permits'][:20]:
                self.stdout.write(
                    f"    [{r['bucket']:9s}] {r['days_until']:4d}d  {r['permit_type']} — {r['permit_group'] or r['route_section']}  "
                    f"({r['project_id'][:8]})  expiry {r['expiry_date'] or '—'}"
                )
        if data['expiring_submissions']:
            self.stdout.write('  Submissions expiring:')
            for r in data['expiring_submissions'][:20]:
                self.stdout.write(
                    f"    [{r['bucket']:9s}] {r['days_until']:4d}d  {r['permit_type']} — {r['permit_group'] or r['label']}  "
                    f"({r['project_id'][:8]})  ref {r['reference'] or '—'}  expiry {r['expiry_date'] or '—'}"
                )

        if data['total_expiring'] > 40:
            self.stdout.write(
                f"  … and {data['total_expiring'] - 40} more within threshold (see --json)."
            )
