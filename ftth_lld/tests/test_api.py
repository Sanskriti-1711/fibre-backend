"""API tests for the FTTH LLD endpoints (``/api/ftth/lld/...``).

These cover the review workflow that guards the design: who may review, what
an approve/reject/correction does to the change and the audit trail, when an
immutable Approved Survey Version may be created (and that the frozen one is
never rewritten), and when LLD may actually run.

The LLD engine is mocked (``ftth_lld.views.engine_lld_*``) and the background
poller thread is replaced, so no network or QGIS is involved.

Run with:
    python manage.py test --settings=config.test_settings ftth_lld
"""

from __future__ import annotations

import json
from unittest import mock

from django.test import TestCase, skipUnlessDBFeature
from rest_framework.test import APIClient

from ftth_lld.models import ApprovedSurveyVersion, LldRun
from survey.models import ApprovalRecord, SurveyFeature
from testutils.factories import (
    LINE,
    OTHER_LINE,
    make_asv,
    make_feature,
    make_ftth_project,
    make_lld_layer,
    make_lld_run,
    make_project_member,
    make_survey_copy,
    make_survey_feature,
    make_user,
)
from users.models import User


class LldApiTestCase(TestCase):
    """A planner, an engineer, an HLD run and its survey copy."""

    def setUp(self):
        self.planner = make_user('planner@example.com', role=User.Role.SUBADMIN)
        self.engineer = make_user('engineer@example.com', role=User.Role.ENGINEER)

        self.ftth = make_ftth_project(name='Berlin Dry Run', created_by=self.planner)
        self.copy = make_survey_copy(self.ftth)

        self.client = APIClient()
        self.client.force_authenticate(user=self.planner)
        self.anon = APIClient()

    def auth_as(self, user):
        self.client.force_authenticate(user=user)
        return self.client


# ======================================================================
# Authentication boundary
# ======================================================================


class LldAuthTests(LldApiTestCase):
    def test_every_endpoint_requires_authentication(self):
        pid = self.ftth.project_id
        checks = [
            ('get', '/api/ftth/lld/projects/'),
            ('get', '/api/ftth/lld/runs/'),
            ('get', f'/api/ftth/lld/projects/{pid}/review/'),
            ('get', f'/api/ftth/lld/projects/{pid}/overview/'),
            ('get', f'/api/ftth/lld/projects/{pid}/members/'),
            ('post', f'/api/ftth/lld/projects/{pid}/members/'),
            ('delete', f'/api/ftth/lld/projects/{pid}/members/abc/'),
            ('get', f'/api/ftth/lld/projects/{pid}/features/f1/lineage/'),
            ('post', f'/api/ftth/lld/projects/{pid}/changes/c1/action/'),
            ('post', f'/api/ftth/lld/projects/{pid}/approved-version/'),
            ('post', f'/api/ftth/lld/projects/{pid}/runs/'),
            ('get', f'/api/ftth/lld/projects/{pid}/runs/LLD-V01/'),
            ('get', f'/api/ftth/lld/projects/{pid}/runs/LLD-V01/layers/trenches/'),
            ('get', f'/api/ftth/lld/projects/{pid}/runs/LLD-V01/download/'),
            ('get', f'/api/ftth/lld/projects/{pid}/versions/'),
            ('get', f'/api/ftth/lld/projects/{pid}/versions/diff/'),
        ]
        for method, url in checks:
            with self.subTest(url=url, method=method):
                response = getattr(self.anon, method)(url)
                self.assertEqual(response.status_code, 401, f'{method.upper()} {url}')


# ======================================================================
# GET /api/ftth/lld/projects/  — readiness
# ======================================================================


class LldProjectsViewTests(LldApiTestCase):
    @skipUnlessDBFeature('supports_distinct_on_fields')
    def test_readiness_is_pending_zero_and_no_corrections(self):
        """Readiness drives the Run LLD button, so its arithmetic matters."""
        pending = make_survey_feature(
            self.copy, self.engineer, status=SurveyFeature.SurveyStatus.MODIFIED
        )
        make_survey_feature(self.copy, self.engineer, status=SurveyFeature.SurveyStatus.APPROVED)
        make_survey_feature(self.copy, self.engineer, status=SurveyFeature.SurveyStatus.REJECTED)

        body = self.client.get('/api/ftth/lld/projects/').json()
        block = next(p for p in body['projects'] if p['project_id'] == self.ftth.project_id)

        self.assertEqual(block['total'], 3)
        self.assertEqual(block['pending'], 1)
        self.assertEqual(block['approved'], 1)
        self.assertEqual(block['rejected'], 1)
        self.assertFalse(block['ready'])

        # Resolving the last pending change makes the project ready.
        pending.survey_status = SurveyFeature.SurveyStatus.APPROVED
        pending.save(update_fields=['survey_status'])

        body = self.client.get('/api/ftth/lld/projects/').json()
        block = next(p for p in body['projects'] if p['project_id'] == self.ftth.project_id)
        self.assertEqual(block['pending'], 0)
        self.assertTrue(block['ready'])

    @skipUnlessDBFeature('supports_distinct_on_fields')
    def test_a_correction_outstanding_blocks_running_lld(self):
        """needs_correction is NOT a resolved state — it must keep LLD blocked."""
        make_survey_feature(self.copy, self.engineer, status=SurveyFeature.SurveyStatus.APPROVED)
        make_survey_feature(
            self.copy, self.engineer, status=SurveyFeature.SurveyStatus.NEEDS_CORRECTION
        )

        body = self.client.get('/api/ftth/lld/projects/').json()
        block = next(p for p in body['projects'] if p['project_id'] == self.ftth.project_id)

        self.assertEqual(block['pending'], 0)
        self.assertEqual(block['needs_correction'], 1)
        self.assertFalse(block['ready'])

    @skipUnlessDBFeature('supports_distinct_on_fields')
    def test_projects_without_changes_are_omitted(self):
        make_ftth_project(name='No survey copy at all')
        orphan = make_ftth_project(name='Copy but no changes')
        make_survey_copy(orphan)

        body = self.client.get('/api/ftth/lld/projects/').json()
        self.assertEqual(body['projects'], [])


# ======================================================================
# GET /api/ftth/lld/projects/<pid>/review/
# ======================================================================


class LldReviewViewTests(LldApiTestCase):
    def test_unknown_project_returns_404(self):
        response = self.client.get('/api/ftth/lld/projects/does-not-exist/review/')
        self.assertEqual(response.status_code, 404)

    def test_review_payload_maps_statuses_and_change_types(self):
        hld_feature = make_feature(self.copy)
        make_survey_feature(
            self.copy,
            self.engineer,
            original_hld_feature=hld_feature,
            original_geometry=LINE,
            survey_geometry=OTHER_LINE,
            status=SurveyFeature.SurveyStatus.PENDING_REVIEW,
        )
        make_survey_feature(self.copy, self.engineer, status=SurveyFeature.SurveyStatus.APPROVED)

        body = self.client.get(f'/api/ftth/lld/projects/{self.ftth.project_id}/review/').json()

        self.assertFalse(body['demo'])
        self.assertEqual(body['project']['id'], self.ftth.project_id)
        self.assertEqual(len(body['changes']), 2)
        statuses = {c['status'] for c in body['changes']}
        self.assertEqual(statuses, {'pending_review', 'approved'})

        moved = next(c for c in body['changes'] if c['change_type'] == 'geometry')
        self.assertEqual(moved['original_geometry'], LINE)
        self.assertEqual(moved['survey_geometry'], OTHER_LINE)
        # The attribute diff is what the reviewer reads before approving.
        self.assertEqual(
            [a['field'] for a in moved['attributes']],
            ['depth_mm'],
        )

        self.assertIn('hld', body['layers'])
        self.assertIn('survey', body['layers'])
        self.assertIsNone(body['approved_survey_version'])
        self.assertIsNone(body['version_chain']['approved_survey'])

    def test_review_reports_the_latest_approved_survey_version(self):
        make_survey_feature(self.copy, self.engineer, status=SurveyFeature.SurveyStatus.APPROVED)
        asv = make_asv(self.ftth, version='AS-V03')

        body = self.client.get(f'/api/ftth/lld/projects/{self.ftth.project_id}/review/').json()
        self.assertEqual(body['approved_survey_version'], asv.version)
        self.assertEqual(body['version_chain']['approved_survey']['id'], 'AS-V03')


# ======================================================================
# POST /api/ftth/lld/projects/<pid>/changes/<cid>/action/
# ======================================================================


class LldChangeActionViewTests(LldApiTestCase):
    def _action(self, change_id, action, comment=''):
        return self.client.post(
            f'/api/ftth/lld/projects/{self.ftth.project_id}/changes/{change_id}/action/',
            {'action': action, 'comment': comment},
            format='json',
        )

    def test_only_subadmin_can_review(self):
        sf = make_survey_feature(self.copy, self.engineer)
        self.auth_as(self.engineer)
        response = self._action(sf.id, 'approve')
        self.assertEqual(response.status_code, 403)

    def test_unknown_action_is_rejected(self):
        sf = make_survey_feature(self.copy, self.engineer)
        response = self._action(sf.id, 'maybe')
        self.assertEqual(response.status_code, 400)
        self.assertIn('approve', response.json()['detail'])

    def test_unknown_change_returns_404(self):
        response = self._action('00000000-0000-0000-0000-000000000000', 'approve')
        self.assertEqual(response.status_code, 404)

    def test_approve_sets_status_and_writes_approval_record(self):
        sf = make_survey_feature(self.copy, self.engineer)
        response = self._action(sf.id, 'approve', comment='looks right')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'approved')
        sf.refresh_from_db()
        self.assertEqual(sf.survey_status, SurveyFeature.SurveyStatus.APPROVED)
        self.assertEqual(sf.review_notes, 'looks right')

        record = ApprovalRecord.objects.get(survey_feature=sf)
        self.assertEqual(record.decision, 'approved')
        self.assertEqual(record.comment, 'looks right')
        self.assertEqual(record.reviewer, self.planner)

    def test_reject_sets_status(self):
        sf = make_survey_feature(self.copy, self.engineer)
        response = self._action(sf.id, 'reject', comment='wrong depth')
        self.assertEqual(response.json()['status'], 'rejected')
        sf.refresh_from_db()
        self.assertEqual(sf.survey_status, SurveyFeature.SurveyStatus.REJECTED)

    def test_correction_is_not_a_rejection(self):
        """'correction' sends the change back; it must not read as rejected."""
        sf = make_survey_feature(self.copy, self.engineer)
        response = self._action(sf.id, 'correction', comment='please re-survey')

        self.assertEqual(response.json()['status'], 'needs_correction')
        sf.refresh_from_db()
        self.assertEqual(sf.survey_status, SurveyFeature.SurveyStatus.NEEDS_CORRECTION)
        self.assertEqual(
            ApprovalRecord.objects.get(survey_feature=sf).decision,
            'needs_correction',
        )

    def test_approved_removal_is_remembered_as_a_removal(self):
        """A removal must stay a removal after approval, or the ASV re-adds it."""
        sf = make_survey_feature(
            self.copy, self.engineer, status=SurveyFeature.SurveyStatus.REMOVED
        )
        self._action(sf.id, 'approve')
        sf.refresh_from_db()
        self.assertTrue(sf.is_removal)
        self.assertEqual(sf.survey_status, SurveyFeature.SurveyStatus.APPROVED)

    def test_resolved_change_is_locked_once_a_version_is_frozen(self):
        """Immutability: a frozen AS version must not be contradicted."""
        sf = make_survey_feature(
            self.copy, self.engineer, status=SurveyFeature.SurveyStatus.APPROVED
        )
        make_asv(self.ftth)

        response = self._action(sf.id, 'reject')
        self.assertEqual(response.status_code, 400)
        self.assertIn('already resolved', response.json()['detail'])

        sf.refresh_from_db()
        self.assertEqual(sf.survey_status, SurveyFeature.SurveyStatus.APPROVED)

    def test_a_new_review_cycle_is_allowed_after_a_version_is_frozen(self):
        """Only resolved changes are locked — a re-edit starts a new cycle."""
        sf = make_survey_feature(
            self.copy, self.engineer, status=SurveyFeature.SurveyStatus.MODIFIED
        )
        make_asv(self.ftth)

        response = self._action(sf.id, 'approve')
        self.assertEqual(response.status_code, 200)
        sf.refresh_from_db()
        self.assertEqual(sf.survey_status, SurveyFeature.SurveyStatus.APPROVED)


# ======================================================================
# POST /api/ftth/lld/projects/<pid>/approved-version/
# ======================================================================


class LldApprovedVersionViewTests(LldApiTestCase):
    def _create(self):
        return self.client.post(f'/api/ftth/lld/projects/{self.ftth.project_id}/approved-version/')

    def test_only_subadmin_can_freeze_a_version(self):
        self.auth_as(self.engineer)
        self.assertEqual(self._create().status_code, 403)

    def test_unresolved_changes_block_the_version(self):
        make_survey_feature(
            self.copy, self.engineer, status=SurveyFeature.SurveyStatus.PENDING_REVIEW
        )
        response = self._create()
        self.assertEqual(response.status_code, 400)
        self.assertIn('unresolved', response.json()['detail'])
        self.assertFalse(ApprovedSurveyVersion.objects.exists())

    def test_creates_first_version_from_resolved_changes(self):
        hld_feature = make_feature(self.copy)
        make_survey_feature(
            self.copy,
            self.engineer,
            status=SurveyFeature.SurveyStatus.APPROVED,
            original_hld_feature=hld_feature,
            original_geometry=LINE,
            survey_geometry=OTHER_LINE,
        )

        response = self._create()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['approved_survey_version'], 'AS-V01')

        # The frozen dataset is the HLD baseline with the approved edit applied.
        feature = body['features']['features'][0]
        self.assertEqual(feature['geometry'], OTHER_LINE)
        self.assertTrue(feature['properties']['approved'])

        asv = ApprovedSurveyVersion.objects.get()
        self.assertEqual(asv.version, 'AS-V01')
        self.assertEqual(asv.created_by, self.planner)

    def test_repeating_creation_returns_the_frozen_version_unchanged(self):
        make_survey_feature(self.copy, self.engineer, status=SurveyFeature.SurveyStatus.APPROVED)
        first = self._create().json()
        second = self._create().json()

        self.assertEqual(first['approved_survey_version'], second['approved_survey_version'])
        self.assertEqual(ApprovedSurveyVersion.objects.count(), 1)

    def test_reroute_without_an_hld_baseline_is_rejected(self):
        """A moved feature with no original path cannot be relayed by the engine."""
        make_survey_feature(
            self.copy,
            self.engineer,
            status=SurveyFeature.SurveyStatus.APPROVED,
            original_hld_feature=None,  # a field-created feature
            original_geometry=LINE,
            survey_geometry=OTHER_LINE,  # ...but it claims to be a reroute
        )

        response = self._create()
        self.assertEqual(response.status_code, 400)
        self.assertIn('original HLD baseline', response.json()['detail'])
        self.assertFalse(ApprovedSurveyVersion.objects.exists())

    def test_editing_after_a_freeze_creates_the_next_version(self):
        """A new cycle makes AS-V02 — the frozen V01 is never rewritten."""
        hld_feature = make_feature(self.copy)
        sf = make_survey_feature(
            self.copy,
            self.engineer,
            status=SurveyFeature.SurveyStatus.APPROVED,
            original_hld_feature=hld_feature,
            original_geometry=LINE,
            survey_geometry=LINE,
        )
        self._create()
        v1 = ApprovedSurveyVersion.objects.get(version='AS-V01')
        v1_dataset = json.dumps(v1.dataset, sort_keys=True)

        # The engineer edits again after the freeze → a new cycle.
        sf.survey_geometry = OTHER_LINE
        sf.survey_status = SurveyFeature.SurveyStatus.APPROVED
        sf.save()

        response = self._create()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['approved_survey_version'], 'AS-V02')
        self.assertEqual(ApprovedSurveyVersion.objects.count(), 2)

        v1.refresh_from_db()
        self.assertEqual(json.dumps(v1.dataset, sort_keys=True), v1_dataset)


# ======================================================================
# POST /api/ftth/lld/projects/<pid>/runs/
# ======================================================================


class LldRunViewTests(LldApiTestCase):
    def _run(self, **payload):
        return self.client.post(
            f'/api/ftth/lld/projects/{self.ftth.project_id}/runs/',
            payload,
            format='json',
        )

    def test_only_subadmin_can_run_lld(self):
        self.auth_as(self.engineer)
        self.assertEqual(self._run().status_code, 403)

    def test_requires_an_approved_survey_version(self):
        make_survey_feature(self.copy, self.engineer, status=SurveyFeature.SurveyStatus.APPROVED)
        response = self._run()
        self.assertEqual(response.status_code, 400)
        self.assertIn('Approved Survey Version', response.json()['detail'])

    def test_unresolved_changes_block_the_run(self):
        make_asv(self.ftth)
        make_survey_feature(
            self.copy, self.engineer, status=SurveyFeature.SurveyStatus.NEEDS_CORRECTION
        )
        response = self._run()
        self.assertEqual(response.status_code, 400)
        self.assertIn('unresolved', response.json()['detail'])
        self.assertFalse(LldRun.objects.exists())

    def test_rejects_an_unknown_mode(self):
        make_asv(self.ftth)
        response = self._run(mode='teleport')
        self.assertEqual(response.status_code, 400)

    def test_creates_a_verify_run_and_does_not_block_the_request(self):
        asv = make_asv(self.ftth)
        make_survey_feature(self.copy, self.engineer, status=SurveyFeature.SurveyStatus.APPROVED)

        with mock.patch('ftth_lld.views.threading.Thread') as thread:
            response = self._run()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['lld_version'], 'LLD-V01')
        self.assertEqual(body['mode'], LldRun.MODE_VERIFY)
        self.assertEqual(body['status'], LldRun.STATUS_RUNNING)

        run = LldRun.objects.get()
        self.assertEqual(run.approved_survey_version, asv)
        self.assertEqual(run.input_dataset_version, asv.version)
        self.assertEqual(run.run_by, self.planner)
        # The engine work happens off-request.
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs.get('daemon'))

    def test_version_labels_increment(self):
        asv = make_asv(self.ftth)
        make_survey_feature(self.copy, self.engineer, status=SurveyFeature.SurveyStatus.APPROVED)
        make_lld_run(self.ftth, version='LLD-V01', asv=asv)
        make_lld_run(self.ftth, version='LLD-V03', asv=asv)  # gap must not matter

        with mock.patch('ftth_lld.views.threading.Thread'):
            response = self._run(mode='replan')

        self.assertEqual(response.json()['lld_version'], 'LLD-V04')
        self.assertEqual(response.json()['mode'], LldRun.MODE_REPLAN)


# ======================================================================
# Run status / layers / download / versions
# ======================================================================


class LldRunStatusViewTests(LldApiTestCase):
    def test_unknown_run_returns_404(self):
        response = self.client.get(f'/api/ftth/lld/projects/{self.ftth.project_id}/runs/LLD-V09/')
        self.assertEqual(response.status_code, 404)

    def test_reports_progress_validation_and_layers(self):
        run = make_lld_run(
            self.ftth,
            status=LldRun.STATUS_COMPLETED,
            progress=100,
            validation={'issues': 0, 'checked': 12},
        )
        make_lld_layer(run, name='final_trenches', feature_count=4)

        body = self.client.get(
            f'/api/ftth/lld/projects/{self.ftth.project_id}/runs/LLD-V01/'
        ).json()

        self.assertEqual(body['status'], LldRun.STATUS_COMPLETED)
        self.assertEqual(body['progress'], 100)
        self.assertEqual(body['validation'], {'issues': 0, 'checked': 12})
        self.assertEqual(body['layers'], [{'name': 'final_trenches', 'feature_count': 4}])


class LldLayerViewTests(LldApiTestCase):
    def _url(self, layer, version='LLD-V01'):
        return f'/api/ftth/lld/projects/{self.ftth.project_id}/runs/{version}/layers/{layer}/'

    def test_unknown_run_returns_404(self):
        self.assertEqual(self.client.get(self._url('trenches', 'LLD-V42')).status_code, 404)

    def test_unknown_layer_returns_404(self):
        make_lld_run(self.ftth)
        self.assertEqual(self.client.get(self._url('nope')).status_code, 404)

    def test_serves_the_stored_layer_geojson(self):
        run = make_lld_run(self.ftth, status=LldRun.STATUS_COMPLETED)
        make_lld_layer(run, name='final_trenches')

        response = self.client.get(self._url('final_trenches'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['type'], 'FeatureCollection')
        self.assertEqual(len(response.json()['features']), 1)


class LldDownloadViewTests(LldApiTestCase):
    def _url(self, version='LLD-V01'):
        return f'/api/ftth/lld/projects/{self.ftth.project_id}/runs/{version}/download/'

    def test_unknown_run_returns_404(self):
        self.assertEqual(self.client.get(self._url('LLD-V77')).status_code, 404)

    def test_incomplete_run_cannot_be_downloaded(self):
        make_lld_run(self.ftth, status=LldRun.STATUS_RUNNING)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 400)

    def test_missing_engine_zip_returns_404(self):
        make_lld_run(self.ftth, status=LldRun.STATUS_COMPLETED)
        with mock.patch('ftth_lld.views.engine_lld_download', return_value=None):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_completed_run_returns_the_zip(self):
        make_lld_run(self.ftth, status=LldRun.STATUS_COMPLETED)
        with mock.patch('ftth_lld.views.engine_lld_download', return_value=b'PK\x03\x04lld'):
            response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'PK\x03\x04lld')
        self.assertEqual(response['Content-Type'], 'application/zip')
        self.assertIn('LLD-V01_lld.zip', response['Content-Disposition'])


class LldVersionsViewTests(LldApiTestCase):
    def test_empty_state_reports_no_versions(self):
        body = self.client.get(f'/api/ftth/lld/projects/{self.ftth.project_id}/versions/').json()
        self.assertEqual(body['runs'], [])
        self.assertIsNone(body['approved_survey_version'])
        self.assertEqual(body['version_chain']['hld']['id'], 'HLD-V1')

    def test_lists_the_run_history_with_its_provenance(self):
        asv = make_asv(self.ftth)
        run = make_lld_run(
            self.ftth,
            version='LLD-V02',
            asv=asv,
            status=LldRun.STATUS_COMPLETED,
            mode=LldRun.MODE_REPLAN,
        )
        make_lld_layer(run, name='final_trenches', feature_count=9)

        body = self.client.get(f'/api/ftth/lld/projects/{self.ftth.project_id}/versions/').json()

        self.assertEqual(body['approved_survey_version'], 'AS-V01')
        self.assertEqual(len(body['runs']), 1)
        entry = body['runs'][0]
        self.assertEqual(entry['lld_version'], 'LLD-V02')
        self.assertEqual(entry['mode'], LldRun.MODE_REPLAN)
        self.assertEqual(entry['approved_survey_version'], 'AS-V01')
        self.assertEqual(entry['layers'], [{'name': 'final_trenches', 'feature_count': 9}])


class LldRunsViewTests(LldApiTestCase):
    def test_lists_runs_across_projects(self):
        other = make_ftth_project(name='Other run')
        make_lld_run(self.ftth, version='LLD-V01', status=LldRun.STATUS_COMPLETED)
        make_lld_run(other, version='LLD-V01', status=LldRun.STATUS_FAILED)

        body = self.client.get('/api/ftth/lld/runs/').json()
        self.assertEqual(len(body['runs']), 2)
        self.assertEqual(
            {r['project_id'] for r in body['runs']}, {self.ftth.project_id, other.project_id}
        )


# ======================================================================
# GET /api/ftth/lld/projects/<pid>/versions/diff/  — cross-run diff (A24)
# ======================================================================


class LldVersionsDiffViewTests(LldApiTestCase):
    def _url(self, qs=''):
        return f'/api/ftth/lld/projects/{self.ftth.project_id}/versions/diff/{qs}'

    @staticmethod
    def _fc(n):
        return {
            'type': 'FeatureCollection',
            'features': [
                {'type': 'Feature', 'geometry': LINE, 'properties': {'i': i}} for i in range(n)
            ],
        }

    def test_diffs_two_completed_runs_layer_by_layer(self):
        older = make_lld_run(self.ftth, version='LLD-V05', status=LldRun.STATUS_COMPLETED)
        make_lld_layer(older, name='final_trenches', geojson=self._fc(2), feature_count=2)
        newer = make_lld_run(self.ftth, version='LLD-V06', status=LldRun.STATUS_COMPLETED)
        make_lld_layer(newer, name='final_trenches', geojson=self._fc(3), feature_count=3)
        make_lld_layer(newer, name='feeder_ducts', geojson=self._fc(5), feature_count=5)

        # The AI paragraph is a rewrite of the deterministic numbers — keep
        # the test offline and prove the field flows through.
        with mock.patch('permits.ai.provider.chat_completion', return_value='Two layers grew.'):
            body = self.client.get(self._url('?from=LLD-V05&to=LLD-V06')).json()

        self.assertEqual(body['from']['lld_version'], 'LLD-V05')
        self.assertEqual(body['to']['lld_version'], 'LLD-V06')

        by_name = {r['name']: r for r in body['layers']}
        self.assertEqual(by_name['final_trenches']['status'], 'changed')
        self.assertEqual(by_name['final_trenches']['delta_count'], 1)
        self.assertGreater(by_name['final_trenches']['delta_length_m'], 0)
        self.assertEqual(by_name['feeder_ducts']['status'], 'added')
        self.assertEqual(by_name['feeder_ducts']['delta_count'], 5)

        totals = body['totals']
        self.assertEqual(totals['from_features'], 2)
        self.assertEqual(totals['to_features'], 8)
        self.assertEqual(totals['delta_features'], 6)
        self.assertEqual(totals['layers_changed'], 1)
        self.assertEqual(totals['layers_added'], 1)
        self.assertIn('LLD-V05 → LLD-V06', body['summary'])
        self.assertEqual(body['ai_summary'], 'Two layers grew.')
        self.assertTrue(body['ai_disclaimer'])  # every AI note carries the footer

    def test_defaults_to_the_two_most_recent_completed_runs(self):
        make_lld_run(self.ftth, version='LLD-V05', status=LldRun.STATUS_COMPLETED)
        make_lld_run(self.ftth, version='LLD-V06', status=LldRun.STATUS_COMPLETED)
        # A failed run must not be picked as either side of the default pair.
        make_lld_run(self.ftth, version='LLD-V07', status=LldRun.STATUS_FAILED)

        body = self.client.get(self._url()).json()
        self.assertEqual(body['from']['lld_version'], 'LLD-V05')
        self.assertEqual(body['to']['lld_version'], 'LLD-V06')

    def test_identical_runs_summarise_as_identical(self):
        make_lld_run(self.ftth, version='LLD-V01', status=LldRun.STATUS_COMPLETED)
        make_lld_run(self.ftth, version='LLD-V02', status=LldRun.STATUS_COMPLETED)

        body = self.client.get(self._url()).json()
        self.assertIn('identical', body['summary'])
        self.assertEqual(body['layers'], [])
        self.assertNotIn('ai_summary', body)  # nothing changed → no AI note

    def test_unknown_version_is_a_404(self):
        make_lld_run(self.ftth, version='LLD-V01', status=LldRun.STATUS_COMPLETED)
        response = self.client.get(self._url('?from=LLD-V01&to=LLD-V99'))
        self.assertEqual(response.status_code, 404)
        self.assertIn('LLD-V99', response.json()['detail'])

    def test_fewer_than_two_completed_runs_is_a_400(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 400)
        self.assertIn('two completed LLD runs', response.json()['detail'])


# ======================================================================
# Tier-1 A5 — the Approval Queue carries risk scores
# ======================================================================


class LldProjectsRiskTests(LldApiTestCase):
    @skipUnlessDBFeature('supports_distinct_on_fields')
    def test_changes_are_sorted_by_risk_and_carry_their_band(self):
        # High risk: a feeder geometry reroute captured on a clean GPS fix.
        high = make_survey_feature(
            self.copy,
            self.engineer,
            status=SurveyFeature.SurveyStatus.MODIFIED,
            original_hld_feature=make_feature(self.copy),
            survey_geometry=OTHER_LINE,  # geometry moved → severity 4
            gps_quality='ok',
        )
        # Low risk: nothing material changed (same geometry and attributes),
        # created LAST so the default -updated_at ordering would show it first.
        make_survey_feature(
            self.copy,
            self.engineer,
            status=SurveyFeature.SurveyStatus.MODIFIED,
            original_hld_feature=make_feature(self.copy),
            survey_geometry=LINE,
            original_geometry=LINE,
            survey_attributes={'depth_mm': 600},
            original_attributes={'depth_mm': 600},
            gps_quality='reject',
        )

        body = self.client.get('/api/ftth/lld/projects/').json()
        block = next(p for p in body['projects'] if p['project_id'] == self.ftth.project_id)
        changes = block['changes']

        self.assertEqual(len(changes), 2)
        # Highest-risk first, regardless of insertion order.
        self.assertEqual(changes[0]['change_id'], str(high.id))
        self.assertGreater(changes[0]['risk']['score'], changes[1]['risk']['score'])
        self.assertIn(changes[0]['risk']['band'], ('high', 'critical'))
        self.assertEqual(changes[1]['risk']['band'], 'low')
        self.assertTrue(changes[0]['risk']['factors'])

        bands = block['risk_bands']
        self.assertEqual(sum(bands.values()), 2)
        self.assertEqual(bands['low'], 1)


# ======================================================================
# Team / overview / lineage
# ======================================================================


class ProjectMembersViewTests(LldApiTestCase):
    def _url(self):
        return f'/api/ftth/lld/projects/{self.ftth.project_id}/members/'

    def test_team_includes_planner_and_managed_members(self):
        self.ftth.assigned_engineer = self.engineer
        self.ftth.save()
        make_project_member(self.copy, self.planner, role='reviewer')

        body = self.client.get(self._url()).json()
        roles = [m['role'] for m in body['members']]
        self.assertIn('planner', roles)
        self.assertIn('engineer', roles)
        self.assertIn('reviewer', roles)

        managed = [m for m in body['members'] if m['managed']]
        self.assertEqual(len(managed), 1)
        self.assertIsNotNone(managed[0]['member_id'])

    def test_adding_a_member_requires_subadmin(self):
        self.auth_as(self.engineer)
        response = self.client.post(
            self._url(), {'user_id': str(self.planner.id), 'role': 'observer'}, format='json'
        )
        self.assertEqual(response.status_code, 403)

    def test_adding_a_member_without_a_survey_copy_is_a_400(self):
        bare = make_ftth_project(name='No copy')
        response = self.client.post(
            f'/api/ftth/lld/projects/{bare.project_id}/members/',
            {'user_id': str(self.engineer.id), 'role': 'observer'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_role_is_rejected(self):
        response = self.client.post(
            self._url(), {'user_id': str(self.engineer.id), 'role': 'wizard'}, format='json'
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid role', response.json()['detail'])

    def test_planner_adds_a_member_and_it_is_idempotent(self):
        payload = {'user_id': str(self.engineer.id), 'role': 'contractor'}
        first = self.client.post(self._url(), payload, format='json')
        self.assertEqual(first.status_code, 201)
        self.assertTrue(first.json()['created'])

        second = self.client.post(self._url(), payload, format='json')
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()['created'])


class ProjectMemberRemoveViewTests(LldApiTestCase):
    def test_removing_a_member_requires_subadmin(self):
        member = make_project_member(self.copy, self.engineer)
        self.auth_as(self.engineer)
        response = self.client.delete(
            f'/api/ftth/lld/projects/{self.ftth.project_id}/members/{member.id}/'
        )
        self.assertEqual(response.status_code, 403)

    def test_removes_the_member(self):
        member = make_project_member(self.copy, self.engineer)
        response = self.client.delete(
            f'/api/ftth/lld/projects/{self.ftth.project_id}/members/{member.id}/'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['deleted'], str(member.id))
        self.assertFalse(type(member).objects.filter(pk=member.id).exists())


class ProjectOverviewViewTests(LldApiTestCase):
    def test_overview_aggregates_layers_approvals_and_latest_run(self):
        make_feature(self.copy, layer_id='trench_layer')
        make_feature(self.copy, layer_id='trench_layer')
        make_survey_feature(self.copy, self.engineer, status=SurveyFeature.SurveyStatus.APPROVED)
        make_survey_feature(
            self.copy, self.engineer, status=SurveyFeature.SurveyStatus.PENDING_REVIEW
        )
        make_lld_run(self.ftth, status=LldRun.STATUS_COMPLETED, progress=100)

        body = self.client.get(f'/api/ftth/lld/projects/{self.ftth.project_id}/overview/').json()

        layer = next(l for l in body['layers'] if l['layer_id'] == 'trench_layer')
        self.assertEqual(layer['feature_count'], 2)
        self.assertEqual(layer['survey_changes'], 2)
        self.assertEqual(layer['approved_changes'], 1)

        self.assertEqual(body['approval_summary']['total'], 2)
        self.assertEqual(body['approval_summary']['approved'], 1)
        self.assertEqual(body['approval_summary']['pending_review'], 1)

        self.assertEqual(body['lld']['run'], 'LLD-V01')
        self.assertEqual(body['lld']['status'], LldRun.STATUS_COMPLETED)

    def test_overview_unknown_project_returns_404(self):
        response = self.client.get('/api/ftth/lld/projects/nope/overview/')
        self.assertEqual(response.status_code, 404)


class FeatureLineageViewTests(LldApiTestCase):
    def test_unknown_project_returns_404(self):
        response = self.client.get('/api/ftth/lld/projects/nope/features/f1/lineage/')
        self.assertEqual(response.status_code, 404)

    def test_lineage_traces_hld_survey_and_approval(self):
        hld_feature = make_feature(self.copy)
        sf = make_survey_feature(
            self.copy,
            self.engineer,
            original_hld_feature=hld_feature,
            original_geometry=LINE,
            survey_geometry=OTHER_LINE,
            status=SurveyFeature.SurveyStatus.APPROVED,
        )
        ApprovalRecord.objects.create(
            survey_feature=sf,
            decision='approved',
            comment='ok',
            reviewer=self.planner,
        )

        body = self.client.get(
            f'/api/ftth/lld/projects/{self.ftth.project_id}/features/{hld_feature.id}/lineage/'
        ).json()

        self.assertEqual(body['feature_id'], str(hld_feature.id))
        self.assertEqual(body['hld']['geometry'], LINE)
        self.assertEqual(body['survey']['change_id'], str(sf.id))
        self.assertEqual(body['approval']['status'], 'approved')
        self.assertEqual(len(body['approval']['history']), 1)
        self.assertEqual(
            body['approval']['history'][0]['reviewer'], self.planner.full_name or self.planner.email
        )

    def test_lineage_includes_the_lld_output_when_the_feature_survives_to_lld(self):
        hld_feature = make_feature(self.copy)
        run = make_lld_run(self.ftth, status=LldRun.STATUS_COMPLETED)
        make_lld_layer(
            run,
            name='final_trenches',
            geojson={
                'type': 'FeatureCollection',
                'features': [
                    {
                        'type': 'Feature',
                        'geometry': OTHER_LINE,
                        'properties': {
                            'feature_id': str(hld_feature.id),
                            'layer': 'final_trenches',
                        },
                    }
                ],
            },
        )

        body = self.client.get(
            f'/api/ftth/lld/projects/{self.ftth.project_id}/features/{hld_feature.id}/lineage/'
        ).json()

        self.assertEqual(body['lld']['run'], 'LLD-V01')
        self.assertEqual(body['lld']['final']['geometry'], OTHER_LINE)
        self.assertEqual(body['lld']['layers'], ['final_trenches'])


# ======================================================================
# Requirement: the survey states that block LLD are exactly these
# ======================================================================


class ResolvedStatusTests(LldApiTestCase):
    def test_only_approved_rejected_and_completed_resolve_a_change(self):
        """Pins the LLD blocking rule: a correction must keep LLD blocked."""
        for status in (
            SurveyFeature.SurveyStatus.APPROVED,
            SurveyFeature.SurveyStatus.REJECTED,
            SurveyFeature.SurveyStatus.COMPLETED,
        ):
            with self.subTest(status=status):
                sf = make_survey_feature(self.copy, self.engineer, status=status)
                self.assertTrue(
                    sf.survey_status
                    in {
                        SurveyFeature.SurveyStatus.APPROVED,
                        SurveyFeature.SurveyStatus.REJECTED,
                        SurveyFeature.SurveyStatus.COMPLETED,
                    }
                )

        # And the decision vocabulary maps the way the two endpoints share it.
        self.assertEqual(
            SurveyFeature.status_for_decision('approve'),
            SurveyFeature.SurveyStatus.APPROVED,
        )
        self.assertEqual(
            SurveyFeature.status_for_decision('reject'),
            SurveyFeature.SurveyStatus.REJECTED,
        )
        for spelling in ('correction', 'redo', 'request_correction'):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    SurveyFeature.status_for_decision(spelling),
                    SurveyFeature.SurveyStatus.NEEDS_CORRECTION,
                )
        self.assertIsNone(SurveyFeature.status_for_decision('banana'))
