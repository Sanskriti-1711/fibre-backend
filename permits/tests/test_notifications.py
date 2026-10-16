"""Permit review email — prove the reviewer is actually told, and safely.

The notification layer sits between a submitted permit and whoever has to act
on it, which makes two properties worth pinning down harder than the happy
path:

* **an email problem never costs a submission** — an unconfigured mailbox and a
  dead SMTP server both leave the state machine's work intact, and the failure
  is visible on ``PermitNotification`` instead of in a log nobody reads;
* **the emailed Approve/Reject link cannot be triggered by a link scanner** —
  ``GET`` renders and changes nothing, only the ``POST`` from the confirmation
  page applies the decision, and every edge it applies is one
  ``ALLOWED_TRANSITIONS`` already allowed.

The rest is ordinary behaviour: the right events email, the wrong ones do not,
and the renewal digest announces each bucket once.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from permits import notifications
from permits.models import (
    PermitAuthority,
    PermitEvent,
    PermitMatrix,
    PermitNotification,
    PermitSubmission,
)
from permits.submissions import (
    ALLOWED_TRANSITIONS,
    EMAIL_REJECTION_NOTE,
    apply_review_action,
    create_submissions,
    status_path,
    transition_submission,
)
from testutils.factories import make_ftth_project, make_user

REVIEWER = 'sansshevgaonkar1711@gmail.com'
ACTION_URL = '/api/ftth/permits/submissions/action/'
FEED_URL = '/api/ftth/permits/notifications/'

MAIL_SETTINGS = {
    'PERMIT_NOTIFY_EMAIL': REVIEWER,
    'PERMITS_PUBLIC_BASE_URL': 'https://backend.example.test',
    'PERMITS_TRACKER_URL': 'https://tracker.example.test/ftth-permits.html',
    'DEFAULT_FROM_EMAIL': REVIEWER,
}


class PermitNotificationTestCase(TestCase):
    """One project, one authority, one street of READY permits."""

    def setUp(self):
        self.project = make_ftth_project(name='Berlin Ring')
        self.authority = PermitAuthority.objects.create(
            code='DE-BE-MITTE',
            name='Bezirksamt Mitte',
            authority_type=PermitAuthority.TYPE_MUNICIPALITY,
        )
        self.planner = make_user('planner@example.com')
        self.rows = [self._row('Kantstraße', f'sec-{i}') for i in range(3)]
        # APIClient throughout: the tracker talks JWT, and force_authenticate is
        # how these tests present it.
        self.client = APIClient()
        self.client.force_authenticate(user=self.planner)

    def _row(self, group: str, section: str) -> PermitMatrix:
        return PermitMatrix.objects.create(
            project=self.project,
            route_section=section,
            layer='final_trenches',
            authority=self.authority,
            permit_type='Road Opening',
            permit_group=group,
            required=True,
            status=PermitMatrix.STATUS_READY,
            readiness_pct=100,
        )

    def submit(self, rows=None, **kwargs) -> list[PermitSubmission]:
        """create_submissions with its post-commit email actually delivered."""
        rows = rows if rows is not None else self.rows
        with self.captureOnCommitCallbacks(execute=True):
            return create_submissions([str(r.permit_id) for r in rows], user=self.planner, **kwargs)

    def transition(self, submission, status, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return transition_submission(submission, status, **kwargs)

    def outbox(self) -> list:
        return mail.outbox


# ======================================================================
# Sent for approval
# ======================================================================


@override_settings(**MAIL_SETTINGS)
class SubmissionNotificationTests(PermitNotificationTestCase):
    def test_submitting_tells_the_reviewer(self):
        submissions = self.submit()

        self.assertEqual(len(self.outbox()), 1)
        message = self.outbox()[0]
        self.assertEqual(message.to, [REVIEWER])
        self.assertEqual(message.from_email, REVIEWER)
        self.assertIn('sent for approval', message.subject)
        self.assertIn('Berlin Ring', message.subject)
        for submission in submissions:
            self.assertIn(str(submission.id), message.body)
        self.assertIn('Kantstraße', message.body)
        self.assertIn('planner@example.com', message.body)  # who submitted

    def test_a_nameless_project_leaves_no_dangling_dash(self):
        """Project.name is optional, and the subject carries it verbatim."""
        self.project.name = ''
        self.project.save(update_fields=['name'])

        self.submit()

        self.assertEqual(self.outbox()[0].subject, '[Permits] 1 application sent for approval')
        self.assertIn('Project   : —', self.outbox()[0].body)

    def test_the_batch_is_one_email_not_one_per_submission(self):
        """A submission batch is one decision, so it is one message."""
        extra = self._row('Kantstraße', 'sec-9')
        extra.permit_type = 'Railway Crossing'
        extra.save(update_fields=['permit_type'])

        self.assertEqual(len(self.submit([*self.rows, extra])), 2)
        self.assertEqual(len(self.outbox()), 1)

    def test_the_email_carries_working_one_click_links(self):
        self.submit()
        body = self.outbox()[0].body
        submission = PermitSubmission.objects.get()
        token = notifications.action_token(submission, notifications.ACTION_APPROVE)

        self.assertIn(f'{ACTION_URL}?token={token}', body)
        self.assertIn(notifications.tracker_url(submission), body)

    def test_the_html_part_offers_approve_and_reject(self):
        self.submit()
        alternatives = self.outbox()[0].alternatives
        self.assertTrue(alternatives)
        html, content_type = alternatives[0]
        self.assertEqual(content_type, 'text/html')
        self.assertIn('>Approve<', html)
        self.assertIn('>Reject<', html)

    def test_delivery_is_recorded(self):
        submissions = self.submit()

        rows = PermitNotification.objects.filter(kind=PermitNotification.KIND_SUBMISSION)
        self.assertEqual(rows.count(), len(submissions))
        for row in rows:
            self.assertEqual(row.status, PermitNotification.STATUS_SENT)
            self.assertEqual(row.recipients, REVIEWER)

    def test_the_submit_request_emails_the_reviewer(self):
        """End to end through the API: POST a batch, a reviewer gets told.

        The send is a commit callback, so the response is built before the
        flush here (a TestCase holds its transaction open) even though a
        deployed request commits first. What this pins is the wiring — the
        request, the submission, and the delivered message.
        """
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                '/api/ftth/permits/submissions/',
                {'permit_ids': [str(r.permit_id) for r in self.rows]},
                format='json',
            )

        self.assertEqual(response.status_code, 201)
        notification = response.json()['notification']
        self.assertEqual(notification['recipients'], [REVIEWER])
        self.assertEqual(notification['errors'], [])
        self.assertEqual(self.outbox()[0].to, [REVIEWER])
        self.assertEqual(
            PermitNotification.objects.filter(status=PermitNotification.STATUS_SENT).count(), 1
        )


@override_settings(**MAIL_SETTINGS)
class NotificationDegradationTests(PermitNotificationTestCase):
    """A mailbox that is missing or broken must never break the workflow."""

    @override_settings(PERMIT_NOTIFY_EMAIL='')
    def test_no_recipient_configured_still_submits(self):
        submissions = self.submit()

        self.assertEqual(len(submissions), 1)
        self.assertEqual(
            PermitMatrix.objects.filter(status=PermitMatrix.STATUS_SUBMITTED).count(), 3
        )
        self.assertEqual(self.outbox(), [])
        row = PermitNotification.objects.get()
        self.assertEqual(row.status, PermitNotification.STATUS_SKIPPED)
        self.assertIn('PERMIT_NOTIFY_EMAIL', row.error)

    def test_smtp_failure_is_recorded_and_swallowed(self):
        with mock.patch.object(
            notifications.EmailMultiAlternatives, 'send', side_effect=OSError('smtp refused')
        ):
            submissions = self.submit()

        self.assertEqual(len(submissions), 1)  # the submission survived
        row = PermitNotification.objects.get()
        self.assertEqual(row.status, PermitNotification.STATUS_FAILED)
        self.assertIn('smtp refused', row.error)

    def test_a_failure_does_not_stop_the_next_attempt(self):
        with mock.patch.object(
            notifications.EmailMultiAlternatives, 'send', side_effect=OSError('smtp refused')
        ):
            self.submit()
        PermitNotification.objects.all().delete()

        self.submit(self._row_only())  # a second, healthy batch
        self.assertEqual(self.outbox()[0].to, [REVIEWER])

    def _row_only(self):
        row = self._row('Kantstraße', 'sec-retry')
        return [row]

    def test_no_public_base_url_falls_back_to_tracker_links(self):
        with override_settings(PERMITS_PUBLIC_BASE_URL=''):
            self.submit()

        body = self.outbox()[0].body
        self.assertNotIn('?token=', body)
        self.assertIn('PERMITS_PUBLIC_BASE_URL', body)
        self.assertIn('tracker.example.test', body)


# ======================================================================
# Review outcome
# ======================================================================


@override_settings(**MAIL_SETTINGS)
class StatusChangeNotificationTests(PermitNotificationTestCase):
    def setUp(self):
        super().setUp()
        self.submission = self.submit()[0]
        mail.outbox.clear()

    def test_approval_emails_conditions_and_expiry(self):
        expiry = timezone.now() + timedelta(days=365)
        self.transition(
            self.submission,
            PermitSubmission.STATUS_UNDER_REVIEW,
            user=self.planner,
        )
        self.assertEqual(self.outbox(), [], 'an acknowledgment is bookkeeping, not news')

        self.transition(
            self.submission,
            PermitSubmission.STATUS_APPROVED,
            user=self.planner,
            conditions='work 22:00-06:00',
            expiry_date=expiry,
        )

        self.assertEqual(len(self.outbox()), 1)
        message = self.outbox()[0]
        self.assertIn('Approved', message.subject)
        self.assertIn('work 22:00-06:00', message.body)
        self.assertIn('expires', message.body)
        self.assertEqual(
            PermitNotification.objects.filter(kind=PermitNotification.KIND_STATUS_CHANGE).count(),
            1,
        )

    def test_rejection_emails_the_reason(self):
        self.transition(self.submission, PermitSubmission.STATUS_UNDER_REVIEW, user=self.planner)
        self.transition(
            self.submission,
            PermitSubmission.STATUS_REJECTED,
            user=self.planner,
            notes='missing traffic plan',
        )

        self.assertIn('Rejected', self.outbox()[0].subject)
        self.assertIn('missing traffic plan', self.outbox()[0].body)

    def test_resubmission_emails_as_a_new_revision(self):
        self.transition(self.submission, PermitSubmission.STATUS_UNDER_REVIEW, user=self.planner)
        self.transition(
            self.submission, PermitSubmission.STATUS_REJECTED, user=self.planner, notes='no'
        )
        mail.outbox.clear()

        self.transition(self.submission, PermitSubmission.STATUS_SUBMITTED, user=self.planner)

        self.assertIn('Re-submitted', self.outbox()[0].subject)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.revision, 1)

    def test_closing_is_not_emailed(self):
        self.transition(self.submission, PermitSubmission.STATUS_UNDER_REVIEW, user=self.planner)
        self.transition(self.submission, PermitSubmission.STATUS_APPROVED, user=self.planner)
        mail.outbox.clear()

        self.transition(self.submission, PermitSubmission.STATUS_CLOSED, user=self.planner)

        self.assertEqual(self.outbox(), [])

    def test_a_poller_that_flips_many_sends_one_digest(self):
        other = self._row('Kantstraße', 'sec-other')
        other.save()
        second = self.submit([other])[0]
        first = self.submission
        for submission in (first, second):
            self.transition(submission, PermitSubmission.STATUS_UNDER_REVIEW, user=self.planner)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            for submission in (first, second):
                transition_submission(
                    submission,
                    PermitSubmission.STATUS_APPROVED,
                    sync_source='portal',
                )

        self.assertEqual(len(self.outbox()), 1)
        self.assertIn('+1 more', self.outbox()[0].subject)
        self.assertIn('source portal', self.outbox()[0].body)

    def test_notify_false_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            transition_submission(
                self.submission,
                PermitSubmission.STATUS_UNDER_REVIEW,
                user=self.planner,
                notify=False,
            )
            transition_submission(
                self.submission,
                PermitSubmission.STATUS_APPROVED,
                user=self.planner,
                notify=False,
            )

        self.assertEqual(self.outbox(), [])
        self.assertFalse(
            PermitNotification.objects.filter(kind=PermitNotification.KIND_STATUS_CHANGE).exists()
        )


# ======================================================================
# One-click action links
# ======================================================================


@override_settings(**MAIL_SETTINGS)
class ActionTokenTests(PermitNotificationTestCase):
    def setUp(self):
        super().setUp()
        self.submission = self.submit()[0]

    def test_a_token_resolves_back_to_its_submission_and_action(self):
        token = notifications.action_token(self.submission, notifications.ACTION_APPROVE)

        submission, action = notifications.read_action_token(token)

        self.assertEqual(submission.id, self.submission.id)
        self.assertEqual(action, notifications.ACTION_APPROVE)

    def test_each_action_gets_its_own_token(self):
        approve = notifications.action_token(self.submission, notifications.ACTION_APPROVE)
        reject = notifications.action_token(self.submission, notifications.ACTION_REJECT)

        self.assertNotEqual(approve, reject)
        self.assertEqual(notifications.read_action_token(reject)[1], notifications.ACTION_REJECT)

    def test_an_unknown_action_is_refused(self):
        with self.assertRaises(ValueError):
            notifications.action_token(self.submission, 'delete')

    def test_a_tampered_token_is_refused(self):
        token = notifications.action_token(self.submission, notifications.ACTION_APPROVE)

        with self.assertRaises(ValueError):
            notifications.read_action_token(token[:-2] + 'xy')

    def test_an_expired_token_is_refused(self):
        token = notifications.action_token(self.submission, notifications.ACTION_APPROVE)

        with (
            mock.patch.object(notifications, 'token_max_age_seconds', return_value=-1),
            self.assertRaisesRegex(ValueError, 'expired'),
        ):
            notifications.read_action_token(token)

    def test_a_token_for_a_deleted_submission_is_refused(self):
        token = notifications.action_token(self.submission, notifications.ACTION_APPROVE)
        PermitSubmission.objects.all().delete()

        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            notifications.read_action_token(token)


@override_settings(**MAIL_SETTINGS)
class OneClickViewTests(PermitNotificationTestCase):
    def setUp(self):
        super().setUp()
        self.submission = self.submit()[0]
        self.token = notifications.action_token(self.submission, notifications.ACTION_APPROVE)
        self.reject_token = notifications.action_token(self.submission, notifications.ACTION_REJECT)

    def test_get_renders_a_confirmation_without_changing_anything(self):
        response = self.client.get(ACTION_URL, {'token': self.token})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Approve this application?')
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, PermitSubmission.STATUS_SUBMITTED)
        self.assertEqual(PermitSubmission.objects.get().revision, self.submission.revision)

    def test_get_is_safe_for_a_link_scanner_that_never_posts(self):
        """A mail gateway fetching the link must not sign anything off."""
        for _ in range(3):
            self.client.get(ACTION_URL, {'token': self.token})

        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, PermitSubmission.STATUS_SUBMITTED)
        self.assertFalse(PermitEvent.objects.filter(event='APPROVED').exists())

    def test_post_approves_through_the_legal_edges_only(self):
        response = self.client.post(ACTION_URL, {'token': self.token})

        self.assertEqual(response.status_code, 200)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, PermitSubmission.STATUS_APPROVED)
        self.assertEqual(self.submission.sync_source, 'email')
        # Counted rather than ordered: created_at is auto_now_add and the three
        # rows of a transition share a clock tick, so ordering is a coin toss.
        by_event = Counter(
            PermitEvent.objects.filter(permit__submission=self.submission).values_list(
                'event', flat=True
            )
        )
        self.assertEqual(by_event['SUBMITTED'], 3)
        # The acknowledgment the machine requires before a decision.
        self.assertEqual(by_event['UNDER_REVIEW'], 3)
        self.assertEqual(by_event['APPROVED'], 3)

    def test_the_audit_names_who_acted(self):
        self.client.post(ACTION_URL, {'token': self.token})

        event = PermitEvent.objects.filter(event='APPROVED').first()
        self.assertEqual(event.detail['by'], REVIEWER)
        self.assertEqual(event.detail['sync_source'], 'email')

    def test_rejecting_from_the_email_records_a_reason(self):
        response = self.client.post(ACTION_URL, {'token': self.reject_token})

        self.assertEqual(response.status_code, 200)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, PermitSubmission.STATUS_REJECTED)
        event = PermitEvent.objects.filter(event='REJECTED').first()
        self.assertEqual(event.detail['reason'], EMAIL_REJECTION_NOTE)

    def test_the_decision_reaches_every_row(self):
        self.client.post(ACTION_URL, {'token': self.token})

        statuses = set(
            PermitMatrix.objects.filter(submission=self.submission).values_list('status', flat=True)
        )
        self.assertEqual(statuses, {PermitMatrix.STATUS_APPROVED})

    def test_a_second_click_reports_instead_of_re_approving(self):
        self.client.post(ACTION_URL, {'token': self.token})

        response = self.client.post(ACTION_URL, {'token': self.token})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Already approved')

    def test_a_decision_the_machine_forbids_is_a_conflict(self):
        self.client.post(ACTION_URL, {'token': self.token})  # → approved

        response = self.client.post(ACTION_URL, {'token': self.reject_token})  # approved → rejected

        self.assertEqual(response.status_code, 409)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, PermitSubmission.STATUS_APPROVED)

    def test_an_expired_link_is_gone_not_applied(self):
        with mock.patch.object(notifications, 'token_max_age_seconds', return_value=-1):
            response = self.client.post(ACTION_URL, {'token': self.token})

        self.assertEqual(response.status_code, 410)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, PermitSubmission.STATUS_SUBMITTED)

    def test_a_bogus_token_is_refused(self):
        response = self.client.get(ACTION_URL, {'token': 'not-a-real-token'})

        self.assertEqual(response.status_code, 400)

    def test_a_missing_token_is_refused(self):
        self.assertEqual(self.client.get(ACTION_URL).status_code, 400)

    def test_the_endpoint_is_public_but_only_for_a_valid_token(self):
        """No session, no JWT — the token is the whole credential."""
        anonymous = APIClient()
        self.assertEqual(anonymous.get(ACTION_URL, {'token': 'x' * 40}).status_code, 400)
        self.assertEqual(anonymous.get(ACTION_URL, {'token': self.token}).status_code, 200)


class ReviewPathTests(PermitNotificationTestCase):
    """The emailed shortcut walks the state machine; it never widens it."""

    def test_approving_from_submitted_acknowledges_first(self):
        self.assertEqual(
            status_path(PermitSubmission.STATUS_SUBMITTED, PermitSubmission.STATUS_APPROVED),
            [PermitSubmission.STATUS_UNDER_REVIEW, PermitSubmission.STATUS_APPROVED],
        )

    def test_rejecting_from_submitted_acknowledges_first(self):
        self.assertEqual(
            status_path(PermitSubmission.STATUS_SUBMITTED, PermitSubmission.STATUS_REJECTED),
            [PermitSubmission.STATUS_UNDER_REVIEW, PermitSubmission.STATUS_REJECTED],
        )

    def test_every_step_is_a_legal_transition(self):
        path = status_path(PermitSubmission.STATUS_SUBMITTED, PermitSubmission.STATUS_APPROVED)
        current = PermitSubmission.STATUS_SUBMITTED
        for step in path:
            self.assertIn(step, ALLOWED_TRANSITIONS[current])
            current = step

    def test_a_closed_submission_has_no_path(self):
        self.assertEqual(
            status_path(PermitSubmission.STATUS_CLOSED, PermitSubmission.STATUS_APPROVED), []
        )

    def test_the_email_path_did_not_widen_the_state_machine(self):
        """If this list ever grows, the emailed shortcut should be revisited."""
        self.assertEqual(
            ALLOWED_TRANSITIONS[PermitSubmission.STATUS_SUBMITTED],
            frozenset({PermitSubmission.STATUS_UNDER_REVIEW}),
        )

    def test_the_action_helper_refuses_an_impossible_target(self):
        submission = PermitSubmission.objects.create(
            project=self.project,
            authority=self.authority,
            permit_type='Road Opening',
            status=PermitSubmission.STATUS_CLOSED,
        )

        with self.assertRaisesRegex(ValueError, 'cannot be moved'):
            apply_review_action(submission, notifications.ACTION_APPROVE)

    def test_the_action_helper_is_idempotent(self):
        submission = PermitSubmission.objects.create(
            project=self.project,
            authority=self.authority,
            permit_type='Road Opening',
            status=PermitSubmission.STATUS_APPROVED,
        )

        target, path = apply_review_action(submission, notifications.ACTION_APPROVE)

        self.assertEqual(target, PermitSubmission.STATUS_APPROVED)
        self.assertEqual(path, [])


# ======================================================================
# Renewal reminders
# ======================================================================


@override_settings(**MAIL_SETTINGS)
class ExpiryReminderTests(PermitNotificationTestCase):
    def setUp(self):
        super().setUp()
        self.submission = self.submit()[0]
        with self.captureOnCommitCallbacks(execute=True):
            transition_submission(
                self.submission, PermitSubmission.STATUS_UNDER_REVIEW, user=self.planner
            )
            transition_submission(
                self.submission,
                PermitSubmission.STATUS_APPROVED,
                user=self.planner,
                expiry_date=timezone.now() + timedelta(days=5),
            )
        mail.outbox.clear()

    def test_an_expiring_permit_is_emailed(self):
        result = notifications.send_expiry_reminders()

        self.assertEqual(result['reminded'], 1 + 3)  # the submission and its 3 rows
        self.assertEqual(result['delivery']['sent'], 1)
        self.assertEqual(len(self.outbox()), 1)
        self.assertIn('renewal reminder', self.outbox()[0].subject.lower())

    def test_the_same_bucket_is_announced_only_once(self):
        notifications.send_expiry_reminders()
        mail.outbox.clear()

        result = notifications.send_expiry_reminders()

        self.assertEqual(result['reminded'], 0)
        self.assertEqual(result['already_sent'], 4)
        self.assertEqual(self.outbox(), [])

    def test_force_re_sends(self):
        notifications.send_expiry_reminders()
        mail.outbox.clear()

        result = notifications.send_expiry_reminders(force=True)

        self.assertEqual(result['reminded'], 4)
        self.assertEqual(len(self.outbox()), 1)

    def test_a_renewal_with_a_new_expiry_is_announced_again(self):
        """A renewed permit is news even in a bucket already seen."""
        self.submission.expiry_date = timezone.now() + timedelta(days=400)
        self.submission.save(update_fields=['expiry_date'])
        PermitMatrix.objects.filter(submission=self.submission).update(
            expiry_date=self.submission.expiry_date
        )
        notifications.send_expiry_reminders()

        # Back to a short expiry: the key carries the date, so this is fresh.
        self.submission.expiry_date = timezone.now() + timedelta(days=3)
        self.submission.save(update_fields=['expiry_date'])
        PermitMatrix.objects.filter(submission=self.submission).update(
            expiry_date=self.submission.expiry_date
        )
        mail.outbox.clear()

        result = notifications.send_expiry_reminders()

        self.assertEqual(result['reminded'], 4)
        self.assertEqual(len(self.outbox()), 1)

    def test_nothing_due_sends_nothing(self):
        PermitSubmission.objects.update(expiry_date=None)
        PermitMatrix.objects.update(expiry_date=None)

        result = notifications.send_expiry_reminders()

        self.assertEqual(result['total'], 0)
        self.assertEqual(self.outbox(), [])

    def test_the_dedupe_key_is_the_entity_bucket_and_date(self):
        key = notifications.reminder_dedupe_key(
            'submission', 'abc', 'due_7d', timezone.now() + timedelta(days=5)
        )

        self.assertTrue(key.startswith('submission:abc:due_7d:'))
        self.assertLessEqual(len(key), 160)

    def test_the_management_command_reports_the_email(self):
        out = StringIO()
        with self.assertRaises(SystemExit) as exit_info:
            call_command('check_permit_expiries', '--email', stdout=out)

        self.assertEqual(exit_info.exception.code, 1)  # due, not yet expired
        self.assertIn('Email:', out.getvalue())
        self.assertEqual(len(self.outbox()), 1)

    def test_the_command_exits_three_when_nobody_could_be_reached(self):
        out = StringIO()
        with (
            mock.patch.object(
                notifications.EmailMultiAlternatives,
                'send',
                side_effect=OSError('smtp refused'),
            ),
            self.assertRaises(SystemExit) as exit_info,
        ):
            call_command('check_permit_expiries', '--email', stdout=out)

        self.assertEqual(exit_info.exception.code, 3)


# ======================================================================
# Delivery log API
# ======================================================================


@override_settings(**MAIL_SETTINGS)
class NotificationFeedTests(PermitNotificationTestCase):
    def setUp(self):
        super().setUp()
        self.submit()

    def test_the_feed_requires_authentication(self):
        response = APIClient().get(FEED_URL)

        self.assertIn(response.status_code, (401, 403))

    def test_the_feed_lists_what_was_sent(self):
        response = self.client.get(FEED_URL)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['recipients'], [REVIEWER])
        self.assertEqual(len(payload['notifications']), 1)
        row = payload['notifications'][0]
        self.assertEqual(row['status'], PermitNotification.STATUS_SENT)
        self.assertEqual(row['kind'], PermitNotification.KIND_SUBMISSION)
        self.assertEqual(row['recipients'], [REVIEWER])

    def test_a_bad_limit_is_a_400(self):
        self.assertEqual(self.client.get(FEED_URL, {'limit': 'lots'}).status_code, 400)


class UnconfiguredEnvironmentTests(PermitNotificationTestCase):
    """With no mailbox at all the layer is inert — nothing is even attempted."""

    @override_settings(PERMIT_NOTIFY_EMAIL='')
    def test_the_helpers_report_the_layer_as_off(self):
        self.assertFalse(notifications.notifications_enabled())
        self.assertEqual(notifications.notify_recipients(), [])

    def test_recipients_accept_a_list(self):
        with override_settings(PERMIT_NOTIFY_EMAIL=f'{REVIEWER}, lead@example.com; second@x.test'):
            self.assertEqual(
                notifications.notify_recipients(),
                [REVIEWER, 'lead@example.com', 'second@x.test'],
            )
