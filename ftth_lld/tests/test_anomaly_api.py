"""A16 attribute-anomaly detection through the LLD review + freeze endpoints.

The rules live in ``survey.anomaly``; these tests pin where they surface:
the review payload, the per-project queue summary, and the Approved Survey
Version gate.

Run with:
    python manage.py test --settings=config.test_settings ftth_lld.tests.test_anomaly_api
"""

from __future__ import annotations

from ftth_lld.tests.test_api import LldApiTestCase
from survey.models import SurveyFeature
from testutils.factories import make_survey_feature


class ReviewPayloadAnomalyTests(LldApiTestCase):
    def test_review_payload_carries_the_anomalies(self):
        sf = make_survey_feature(
            self.copy,
            self.engineer,
            survey_attributes={'SURFACE': 'Footway', 'trench_type': 'HDD'},
        )
        resp = self.client.get(f'/api/ftth/lld/projects/{self.ftth.project_id}/review/')
        self.assertEqual(resp.status_code, 200)
        changes = resp.json()['changes']
        entry = next(c for c in changes if c['change_id'] == str(sf.id))
        self.assertEqual([a['rule'] for a in entry['anomalies']], ['surface_vs_construction'])
        self.assertEqual(entry['anomalies'][0]['severity'], 'error')

    def test_a_clean_change_reports_no_anomalies(self):
        sf = make_survey_feature(
            self.copy,
            self.engineer,
            survey_attributes={
                'SURFACE': 'Asphalt',
                'REINSTATE': 'Road',
                'trench_type': 'HDD',
                'fclass': 'residential',
            },
        )
        resp = self.client.get(f'/api/ftth/lld/projects/{self.ftth.project_id}/review/')
        entry = next(c for c in resp.json()['changes'] if c['change_id'] == str(sf.id))
        self.assertEqual(entry['anomalies'], [])

    def test_project_queue_summarises_anomaly_severities(self):
        make_survey_feature(
            self.copy,
            self.engineer,
            survey_attributes={'SURFACE': 'Footway', 'trench_type': 'HDD'},
        )
        make_survey_feature(
            self.copy,
            self.engineer,
            survey_attributes={'SURFACE': 'Asphalt', 'fclass': 'footway'},
        )
        resp = self.client.get('/api/ftth/lld/projects/')
        self.assertEqual(resp.status_code, 200)
        project = next(
            p for p in resp.json()['projects'] if p['project_id'] == self.ftth.project_id
        )
        self.assertEqual(project['anomalies'], {'error': 1, 'warn': 1, 'flagged': 2})


class ApprovedVersionGateTests(LldApiTestCase):
    def _approve(self, sf):
        sf.survey_status = SurveyFeature.SurveyStatus.APPROVED
        sf.save(update_fields=['survey_status', 'updated_at'])

    def test_a_contradiction_blocks_the_freeze(self):
        sf = make_survey_feature(
            self.copy,
            self.engineer,
            survey_attributes={'SURFACE': 'Footway', 'trench_type': 'HDD'},
        )
        self._approve(sf)
        resp = self.client.post(f'/api/ftth/lld/projects/{self.ftth.project_id}/approved-version/')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('attribute contradiction', resp.json()['detail'].lower())
        self.assertFalse(self.ftth.approved_survey_versions.exists())

    def test_a_suspicion_does_not_block_the_freeze(self):
        sf = make_survey_feature(
            self.copy,
            self.engineer,
            survey_attributes={'SURFACE': 'Asphalt', 'fclass': 'footway'},
        )
        self._approve(sf)
        resp = self.client.post(f'/api/ftth/lld/projects/{self.ftth.project_id}/approved-version/')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self.ftth.approved_survey_versions.exists())

    def test_rejected_changes_never_block_a_freeze(self):
        make_survey_feature(
            self.copy,
            self.engineer,
            status=SurveyFeature.SurveyStatus.REJECTED,
            survey_attributes={'SURFACE': 'Footway', 'trench_type': 'HDD'},
        )
        resp = self.client.post(f'/api/ftth/lld/projects/{self.ftth.project_id}/approved-version/')
        self.assertEqual(resp.status_code, 200)
