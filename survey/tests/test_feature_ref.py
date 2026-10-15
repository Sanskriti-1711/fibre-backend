"""Survey facts keyed on either side of the HLD/survey split.

The app posts whichever id it is holding in a single ``feature`` field: an
imported package feature carries a ``projects.Feature`` uuid, while a feature
the engineer adds on site exists only as a ``survey.SurveyFeature``. Only the
projects side had a column, so every hazard, risk, trench classification, asset
check, evidence record and status update written against a newly added feature
was refused with ``Invalid pk ... - object does not exist`` (HTTP 400) -- the
app could only show a generic failure toast. That is how a tester run recorded
three hazards, three risks and a status change that never reached the database.

Run with:
    python manage.py test --settings=config.test_settings survey.tests.test_feature_ref
"""

from __future__ import annotations

import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from survey.models import (
    FieldEvidence,
    Hazard,
    RiskAssessment,
    SurveyStatus,
    TrenchSurvey,
)
from testutils.factories import (
    make_feature,
    make_ftth_project,
    make_survey_copy,
    make_survey_feature,
    make_user,
)
from users.models import User


class FeatureRefTestCase(TestCase):
    """One engineer, one survey copy, one feature of each kind."""

    def setUp(self):
        self.engineer = make_user('eng.feature-ref@example.com', role=User.Role.ENGINEER)
        self.copy = make_survey_copy(make_ftth_project(name='Feature ref'))
        self.hld_feature = make_feature(self.copy)
        self.survey_feature = make_survey_feature(self.copy, self.engineer)
        self.client = APIClient()
        self.client.force_authenticate(user=self.engineer)

    # ── the id the app holds for a feature created on site ────────────────

    def test_hazard_accepts_a_survey_feature_id(self):
        response = self.client.post(
            '/api/survey/hazards/',
            {
                'feature': str(self.survey_feature.id),
                'hazard_type': 'excavation',
                'mitigation_template': 'temp_barriers',
                'notes': 'footway cut, barriers before breaking ground',
                'is_active': True,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        hazard = Hazard.objects.get(id=response.data['id'])
        self.assertEqual(hazard.survey_feature_id, self.survey_feature.id)
        self.assertIsNone(hazard.feature_id)
        # The app reads facts back by comparing against the id it sent.
        self.assertEqual(response.data['feature'], str(self.survey_feature.id))

    def test_risk_accepts_a_survey_feature_id(self):
        response = self.client.post(
            '/api/survey/risks/',
            {
                'feature': str(self.survey_feature.id),
                'category': 'traffic',
                'severity': 'high',
                'probability': 'likely',
                'mitigation': 'two-way lights',
                'notes': 'bus route along the carriageway',
                'status': 'open',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        risk = RiskAssessment.objects.get(id=response.data['id'])
        self.assertEqual(risk.survey_feature_id, self.survey_feature.id)
        self.assertIsNone(risk.feature_id)
        self.assertEqual(response.data['feature'], str(self.survey_feature.id))

    def test_trench_classification_accepts_a_survey_feature_id(self):
        response = self.client.post(
            '/api/survey/trenches/',
            {
                'feature': str(self.survey_feature.id),
                'trench_type': 'new_trench',
                'depth_mm': 900,
                'width_mm': 300,
                'surface_type': 'paving',
                'road_crossing': False,
                'permit_required': True,
                'notes': '112 m of footway along the road',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        trench = TrenchSurvey.objects.get(id=response.data['id'])
        self.assertEqual(trench.survey_feature_id, self.survey_feature.id)
        self.assertIsNone(trench.feature_id)

    def test_evidence_accepts_a_survey_feature_id(self):
        response = self.client.post(
            '/api/survey/evidence/',
            {
                'feature': str(self.survey_feature.id),
                'evidence_type': 'measurement',
                'description': 'depth 900 mm at chainage 20 m',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        evidence = FieldEvidence.objects.get(id=response.data['id'])
        self.assertEqual(evidence.survey_feature_id, self.survey_feature.id)
        self.assertIsNone(evidence.feature_id)

    def test_status_saved_on_a_survey_feature_is_listed_by_feature(self):
        """Reading back by the id that was written must find the row."""
        self.client.post(
            '/api/survey/status/',
            {'feature': str(self.survey_feature.id), 'status': 'visited'},
            format='json',
        )
        response = self.client.get(f'/api/survey/status/?feature={self.survey_feature.id}')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['status'], 'visited')

    def test_hazard_on_a_survey_feature_reads_back_under_the_sent_id(self):
        """The app matches records with ``record.feature === featureId``."""
        self.client.post(
            '/api/survey/hazards/',
            {
                'feature': str(self.survey_feature.id),
                'hazard_type': 'dog',
                'is_active': True,
            },
            format='json',
        )
        response = self.client.get('/api/survey/hazards/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['hazard_type'], 'dog')
        # Rendered as strings over HTTP, whichever field holds the row.
        self.assertEqual(str(response.data['results'][0]['feature']), str(self.survey_feature.id))
        self.assertEqual(
            str(response.data['results'][0]['survey_feature']), str(self.survey_feature.id)
        )

    def test_status_accepts_a_survey_feature_id_and_updates_in_place(self):
        """Two posts for one survey feature must leave one status row."""
        first = self.client.post(
            '/api/survey/status/',
            {'feature': str(self.survey_feature.id), 'status': 'visited', 'notes': 'on site'},
            format='json',
        )
        self.assertEqual(first.status_code, 200, first.data)
        second = self.client.post(
            '/api/survey/status/',
            {'feature': str(self.survey_feature.id), 'status': 'completed', 'notes': 'done'},
            format='json',
        )
        self.assertEqual(second.status_code, 200, second.data)
        rows = SurveyStatus.objects.filter(survey_feature=self.survey_feature)
        self.assertEqual(rows.count(), 1)
        row = rows.first()
        assert row is not None
        self.assertEqual(row.status, 'completed')
        self.assertEqual(second.data['feature'], str(self.survey_feature.id))

    # ── the imported-feature path is unchanged ────────────────────────────

    def test_hazard_still_accepts_a_projects_feature_id(self):
        response = self.client.post(
            '/api/survey/hazards/',
            {
                'feature': str(self.hld_feature.id),
                'hazard_type': 'traffic_management',
                'mitigation_template': 'traffic_lights',
                'notes': 'live carriageway 1.2 m from the trench line',
                'is_active': True,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        hazard = Hazard.objects.get(id=response.data['id'])
        self.assertEqual(hazard.feature_id, self.hld_feature.id)
        self.assertIsNone(hazard.survey_feature_id)
        # A PrimaryKeyRelatedField hands back the UUID itself; over HTTP it is
        # rendered as the same string the app sent.
        self.assertEqual(str(response.data['feature']), str(self.hld_feature.id))

    def test_status_still_accepts_a_projects_feature_id(self):
        response = self.client.post(
            '/api/survey/status/',
            {'feature': str(self.hld_feature.id), 'status': 'visited'},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        status_row = SurveyStatus.objects.get(feature=self.hld_feature)
        self.assertEqual(status_row.status, 'visited')
        self.assertIsNone(status_row.survey_feature_id)

    # ── an id that exists in neither table is still an error ──────────────

    def test_unknown_feature_id_is_rejected(self):
        response = self.client.post(
            '/api/survey/hazards/',
            {
                'feature': str(uuid.uuid4()),
                'hazard_type': 'excavation',
                'is_active': True,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('feature', response.data)
