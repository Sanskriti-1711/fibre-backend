"""In-app notifications — prove the right person is told, exactly once.

Two properties matter more than the happy path and are pinned here:

* **The inbox is per person.** A survey engineer must not see a planner's
  design notice, and neither can read or dismiss the other's rows.  Every
  query is scoped by ``request.user``, so the tests assert on the payload a
  stranger actually gets back, not just on the queryset.
* **One event, one bell.** The field app retries an edit when its sync queue
  replays offline work; ``dedupe_key`` is what stops that from ringing twice.

The rest is ordinary behaviour: the badge counts what is unread, marking read
is idempotent, and a notification failure never breaks the thing that caused it.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from notifications import service
from notifications.models import Notification
from testutils.factories import (
    make_ftth_project,
    make_lld_run,
    make_survey_copy,
    make_survey_feature,
    make_user,
)

LIST_URL = '/api/notifications/'
SUMMARY_URL = '/api/notifications/summary/'
READ_ALL_URL = '/api/notifications/read-all/'


class NotificationApiTestCase(TestCase):
    def setUp(self):
        self.planner = make_user('planner@example.com')
        self.engineer = make_user('field@example.com', role='ENGINEER')
        self.client = APIClient()
        self.client.force_authenticate(user=self.planner)
        self.project = make_ftth_project(name='Hagley Road')

    def _seed(self, recipient=None, **kwargs):
        payload = {
            'kind': Notification.KIND_SURVEY_CHANGE,
            'title': 'Survey change submitted — chambers',
            'body': 'A field engineer edited a chamber.',
            'category': Notification.CATEGORY_DESIGN,
            'project_id': self.project.project_id,
            'project_name': self.project.name,
            'url': 'ftth-lld-feature-review.html?project_id=x&change_id=y',
        }
        payload.update(kwargs)
        return service.notify([recipient or self.planner], **payload)

    # ── the inbox ──────────────────────────────────────────────────────

    def test_list_returns_only_the_callers_rows_newest_first(self):
        older = self._seed(title='Older')[0]
        newer = self._seed(title='Newer')[0]
        service.notify(
            [self.engineer],
            kind=Notification.KIND_SURVEY_DECISION,
            title='Not yours',
            category=Notification.CATEGORY_SURVEY,
        )

        # ``created_at`` defaults to ``timezone.now()``, and two inserts in the
        # same tick make "newest first" decide nothing at all — on Windows the
        # clock advances in ~15 ms steps. Stamp the rows apart so the order
        # under test is the order that was asked for (same reasoning as
        # ``testutils.factories.make_lld_run``).
        now = timezone.now()
        Notification.objects.filter(pk=older.pk).update(created_at=now - timedelta(minutes=5))
        Notification.objects.filter(pk=newer.pk).update(created_at=now)

        body = self.client.get(LIST_URL).json()

        self.assertEqual(body['total'], 2)
        self.assertEqual([row['title'] for row in body['notifications']], ['Newer', 'Older'])
        self.assertEqual(body['unread'], 2)

    def test_unread_filter_and_category_filter(self):
        self._seed(title='Design one')
        service.notify(
            [self.planner],
            kind=Notification.KIND_PERMIT,
            title='Permit one',
            category=Notification.CATEGORY_PERMITS,
        )

        only_design = self.client.get(LIST_URL + '?category=design').json()
        self.assertEqual([row['title'] for row in only_design['notifications']], ['Design one'])

        marked = self.client.post(READ_ALL_URL, {}, format='json')
        self.assertEqual(marked.json()['marked'], 2)
        self.assertEqual(self.client.get(LIST_URL + '?unread=1').json()['total'], 0)

    def test_summary_counts_unread_by_category(self):
        self._seed()
        service.notify(
            [self.planner],
            kind=Notification.KIND_PERMIT,
            title='Permit',
            category=Notification.CATEGORY_PERMITS,
        )

        body = self.client.get(SUMMARY_URL).json()

        self.assertEqual(body['unread'], 2)
        self.assertEqual(body['by_category'], {'design': 1, 'permits': 1})
        self.assertIsNotNone(body['latest_at'])

    def test_marking_read_is_idempotent_and_cannot_touch_somebody_else(self):
        [row] = self._seed()
        other = service.notify([self.engineer], kind=Notification.KIND_SYSTEM, title='Theirs')[0]

        first = self.client.post(f'/api/notifications/{row.pk}/read/', {}, format='json')
        second = self.client.post(f'/api/notifications/{row.pk}/read/', {}, format='json')

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()['changed'])
        self.assertFalse(second.json()['changed'])
        self.assertEqual(first.json()['unread'], 0)

        stranger = self.client.post(f'/api/notifications/{other.pk}/read/', {}, format='json')
        self.assertEqual(stranger.status_code, 404)
        other.refresh_from_db()
        self.assertIsNone(other.read_at)

    def test_read_all_narrows_to_the_supplied_ids(self):
        rows = [self._seed(title='A')[0], self._seed(title='B')[0]]

        body = self.client.post(READ_ALL_URL, {'ids': [str(rows[0].pk)]}, format='json').json()

        self.assertEqual(body['marked'], 1)
        self.assertEqual(body['unread'], 1)

    # ── the service ────────────────────────────────────────────────────

    def test_the_same_event_is_not_delivered_twice(self):
        kwargs = {
            'kind': Notification.KIND_SURVEY_CHANGE,
            'title': 'Survey change submitted',
            'dedupe_key': 'survey_change:abc:2',
        }
        first = service.notify([self.planner], **kwargs)
        second = service.notify([self.planner], **kwargs)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(Notification.objects.filter(recipient=self.planner).count(), 1)

    def test_platform_notice_reaches_every_active_planner_only(self):
        make_user('planner2@example.com')
        departed = make_user('gone@example.com')
        departed.is_active = False
        departed.save(update_fields=['is_active'])

        service.notify_platform(kind=Notification.KIND_LLD_RUN, title='LLD completed')

        recipients = set(Notification.objects.values_list('recipient__email', flat=True))
        self.assertEqual(recipients, {'planner@example.com', 'planner2@example.com'})

    def test_a_database_failure_does_not_reach_the_caller(self):
        with mock.patch.object(
            Notification.objects, 'bulk_create', side_effect=RuntimeError('db down')
        ):
            created = service.notify([self.planner], kind=Notification.KIND_SYSTEM, title='x')

        self.assertEqual(created, [])
        self.assertEqual(Notification.objects.count(), 0)

    def test_unauthenticated_callers_get_nothing(self):
        anonymous = APIClient()

        self.assertIn(anonymous.get(LIST_URL).status_code, (401, 403))
        self.assertIn(anonymous.get(SUMMARY_URL).status_code, (401, 403))


class EventAudienceTests(TestCase):
    """Each event helper aims at the person who has to act, and nobody else.

    These pin the audience of every wiring point in the flow, because the
    failure mode that matters is silent: a change lands in the wrong inbox and
    the person responsible never learns about it.
    """

    def setUp(self):
        self.planner = make_user('planner@example.com')
        self.engineer = make_user('field@example.com', role='ENGINEER')
        self.other_engineer = make_user('other@example.com', role='ENGINEER')
        self.ftth = make_ftth_project(name='Ring Road')
        self.copy = make_survey_copy(self.ftth, name='Ring Road survey')
        self.change = make_survey_feature(self.copy, self.engineer, status='pending_review')

    def _recipients(self, **kwargs):
        return set(Notification.objects.filter(**kwargs).values_list('recipient__email', flat=True))

    def test_a_field_edit_lands_in_the_planner_inbox_with_a_deep_link(self):
        [row] = service.survey_change_submitted(self.change, actor=self.engineer)

        self.assertEqual(row.recipient, self.planner)
        self.assertEqual(row.category, Notification.CATEGORY_DESIGN)
        self.assertIn(str(self.change.pk), row.url)
        self.assertIn(str(self.copy.id), row.url)
        self.assertEqual(row.actor, self.engineer)

    def test_a_decision_goes_back_only_to_the_engineer_who_edited(self):
        rows = service.survey_change_decided(
            self.change, decision='needs_correction', actor=self.planner, comment='deeper'
        )

        self.assertEqual([row.recipient for row in rows], [self.engineer])
        self.assertEqual(rows[0].severity, Notification.SEVERITY_WARNING)
        self.assertIn('deeper', rows[0].body)
        self.assertNotIn(self.other_engineer.email, self._recipients())
        self.assertNotIn(
            self.planner.email,
            self._recipients(kind=Notification.KIND_SURVEY_DECISION),
        )

    def test_assignment_notifies_each_assigned_engineer_once(self):
        rows = service.survey_assigned(
            [self.engineer, self.other_engineer],
            project_id=str(self.copy.id),
            project_name=self.copy.name,
            feature_count=12,
            actor=self.planner,
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            self._recipients(kind=Notification.KIND_SURVEY_ASSIGNED),
            {'field@example.com', 'other@example.com'},
        )
        self.assertIn('12 feature', rows[0].body)
        # A repeated assign must not double-ring the same bell.
        self.assertEqual(
            service.survey_assigned(
                [self.engineer, self.other_engineer],
                project_id=str(self.copy.id),
                project_name=self.copy.name,
                feature_count=12,
            ),
            [],
        )

    def test_an_approved_version_tells_planners_they_can_run_lld(self):
        [row] = service.approved_survey_ready(
            project_id=self.ftth.project_id, project_name=self.ftth.name, version='AS-V01'
        )

        self.assertEqual(row.recipient, self.planner)
        self.assertEqual(row.kind, Notification.KIND_LLD_READY)
        self.assertIn(self.ftth.project_id, row.url)

    def test_send_back_reaches_the_package_assignees_and_accept_is_success(self):
        rows = service.survey_package_decided(
            [self.engineer],
            project_id=str(self.copy.id),
            project_name=self.copy.name,
            action='redo',
            actor=self.planner,
        )
        self.assertEqual(rows[0].severity, Notification.SEVERITY_WARNING)

        # The same package being accepted later is a new, separate event.
        accepted = service.survey_package_decided(
            [self.engineer],
            project_id=str(self.copy.id),
            project_name=self.copy.name,
            action='accept',
            actor=self.planner,
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].severity, Notification.SEVERITY_SUCCESS)

    def test_only_the_engineer_facing_actions_notify(self):
        # start_review / reviewed are internal planner bookkeeping.
        for action in ('start_review', 'reviewed'):
            self.assertEqual(
                service.survey_package_decided(
                    [self.engineer], project_id=str(self.copy.id), action=action
                ),
                [],
            )

    def test_a_finished_run_tells_planners_success_or_failure(self):
        run = make_lld_run(self.ftth, version='LLD-V01', run_by=self.planner)

        ok = service.lld_run_finished(run, ok=True)
        failed = service.lld_run_finished(run, ok=False, error='engine exploded')

        self.assertEqual(ok[0].kind, Notification.KIND_LLD_RUN)
        self.assertEqual(ok[0].severity, Notification.SEVERITY_SUCCESS)
        self.assertEqual(failed[0].severity, Notification.SEVERITY_CRITICAL)
        self.assertIn('engine exploded', failed[0].body)
