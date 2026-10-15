"""Tester run: take up one survey case and walk it area by area.

Behaves like a field tester with the app in hand. One test case (a survey
copy with three road segments in different areas) is taken up, and each
survey tool is driven on a different area in turn -- hazards on High
Street, trench classification + evidence on Mill Lane, hazards + risks on
Station Road -- with a status update everywhere. The run ends by submitting
the survey and checking the copy really flipped to ``submitted``.

Run with:
    unset PYTHONPATH
    python manage.py test --settings=config.test_settings \\
        survey.tests.test_tester_road_survey
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from assignments.models import AssignmentJob
from survey.models import FieldEvidence, Hazard, RiskAssessment, SurveyStatus, TrenchSurvey
from testutils.factories import make_feature, make_ftth_project, make_survey_copy, make_user
from users.models import User


def _road_geometry(x0: float) -> dict:
    """A short road segment; each area gets its own stretch of street."""
    return {
        'type': 'LineString',
        'coordinates': [[x0, 52.4485], [x0 + 0.001, 52.4487]],
    }


class TesterRoadSurveyTestCase(TestCase):
    """One tester, one case, three roads, every tool used once."""

    def setUp(self):
        self.engineer = make_user('tester.roads@example.com', role=User.Role.ENGINEER)
        self.copy = make_survey_copy(
            make_ftth_project(name='Tester run: three roads'),
            name='Tester run: three roads',
            status='active',
        )
        # Take up the case: project-scope assignment for this engineer.
        AssignmentJob.objects.create(
            project=self.copy, assignee=self.engineer, scope=AssignmentJob.SCOPE_PROJECT
        )
        # One road segment per area.
        self.high_street = make_feature(
            self.copy,
            geometry=_road_geometry(13.3730),
            properties={'fid': 1, 'road': 'High Street', 'area': 'town centre'},
        )
        self.mill_lane = make_feature(
            self.copy,
            geometry=_road_geometry(13.3750),
            properties={'fid': 2, 'road': 'Mill Lane', 'area': 'residential'},
        )
        self.station_road = make_feature(
            self.copy,
            geometry=_road_geometry(13.3770),
            properties={'fid': 3, 'road': 'Station Road', 'area': 'railway bridge'},
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.engineer)

    # ── Area 1 · High Street (arterial) — the hazard tool ─────────────────

    def test_area1_high_street_hazard_and_traffic_risk(self):
        """Live carriageway 1.2 m from the trench line: traffic hazard + risk."""
        hazard = self.client.post(
            '/api/survey/hazards/',
            {
                'feature': str(self.high_street.id),
                'hazard_type': 'traffic_management',
                'mitigation_template': 'traffic_lights',
                'notes': 'High Street arterial: live carriageway 1.2 m from trench line',
                'is_active': True,
            },
            format='json',
        )
        self.assertEqual(hazard.status_code, 201, hazard.data)

        risk = self.client.post(
            '/api/survey/risks/',
            {
                'feature': str(self.high_street.id),
                'category': 'traffic',
                'severity': 'high',
                'probability': 'likely',
                'mitigation': 'two-way lights off-peak, bus gate marshalled',
                'notes': 'High Street carries the 42/43 bus route all day',
                'status': 'open',
            },
            format='json',
        )
        self.assertEqual(risk.status_code, 201, risk.data)

        status = self.client.post(
            '/api/survey/status/',
            {'feature': str(self.high_street.id), 'status': 'visited'},
            format='json',
        )
        self.assertEqual(status.status_code, 200, status.data)

        self.assertTrue(
            Hazard.objects.filter(
                feature=self.high_street, hazard_type='traffic_management'
            ).exists()
        )
        self.assertTrue(
            RiskAssessment.objects.filter(
                feature=self.high_street, category='traffic', severity='high'
            ).exists()
        )

    # ── Area 2 · Mill Lane (residential footway) — trench + evidence tools ─

    def test_area2_mill_lane_trench_classification_and_evidence(self):
        """Footway build: classify the trench, pin a depth measurement to it."""
        trench = self.client.post(
            '/api/survey/trenches/',
            {
                'feature': str(self.mill_lane.id),
                'trench_type': 'new_trench',
                'depth_mm': 900,
                'width_mm': 300,
                'surface_type': 'paving',
                'road_crossing': False,
                'permit_required': False,
                'notes': 'Mill Lane: 112 m of footway, slabs lifted and stacked',
            },
            format='json',
        )
        self.assertEqual(trench.status_code, 201, trench.data)

        evidence = self.client.post(
            '/api/survey/evidence/',
            {
                'feature': str(self.mill_lane.id),
                'evidence_type': 'measurement',
                'description': 'Mill Lane: depth 900 mm at chainage 20 m',
            },
            format='json',
        )
        self.assertEqual(evidence.status_code, 201, evidence.data)

        status = self.client.post(
            '/api/survey/status/',
            {'feature': str(self.mill_lane.id), 'status': 'verified'},
            format='json',
        )
        self.assertEqual(status.status_code, 200, status.data)

        self.assertTrue(
            TrenchSurvey.objects.filter(
                feature=self.mill_lane, trench_type='new_trench', surface_type='paving'
            ).exists()
        )
        self.assertTrue(
            FieldEvidence.objects.filter(
                feature=self.mill_lane, evidence_type='measurement'
            ).exists()
        )

    # ── Area 3 · Station Road (rail bridge + flood plain) — hazards+risks ──

    def test_area3_station_road_hazards_and_railway_risk(self):
        """Railway bridge parapet + flood plain: excavation hazard, rail risk."""
        flood = self.client.post(
            '/api/survey/hazards/',
            {
                'feature': str(self.station_road.id),
                'hazard_type': 'flood_risk',
                'mitigation_template': 'permit',
                'notes': 'Station Road dips under the rail bridge; standing water after rain',
                'is_active': True,
            },
            format='json',
        )
        self.assertEqual(flood.status_code, 201, flood.data)

        dig = self.client.post(
            '/api/survey/hazards/',
            {
                'feature': str(self.station_road.id),
                'hazard_type': 'excavation',
                'mitigation_template': 'cat_scan',
                'notes': 'Station Road: CAT scan first, unmapped services by the bridge footing',
                'is_active': True,
            },
            format='json',
        )
        self.assertEqual(dig.status_code, 201, dig.data)

        risk = self.client.post(
            '/api/survey/risks/',
            {
                'feature': str(self.station_road.id),
                'category': 'railway',
                'severity': 'critical',
                'probability': 'possible',
                'mitigation': 'Network Rail approval, no work during possessions overrun',
                'notes': 'Station Road passes under the live railway bridge',
                'status': 'escalated',
            },
            format='json',
        )
        self.assertEqual(risk.status_code, 201, risk.data)

        status = self.client.post(
            '/api/survey/status/',
            {'feature': str(self.station_road.id), 'status': 'needs_review'},
            format='json',
        )
        self.assertEqual(status.status_code, 200, status.data)

        self.assertEqual(Hazard.objects.filter(feature=self.station_road).count(), 2)
        rail = RiskAssessment.objects.get(feature=self.station_road, category='railway')
        self.assertEqual(rail.severity, 'critical')
        self.assertEqual(rail.status, 'escalated')

    # ── Submit after all three areas are surveyed ──────────────────────────

    def test_submit_after_all_areas_surveyed(self):
        """The whole run in order: three areas, then submit flips the copy."""
        self.test_area1_high_street_hazard_and_traffic_risk()
        self.test_area2_mill_lane_trench_classification_and_evidence()
        self.test_area3_station_road_hazards_and_railway_risk()

        # Every road carries a status before submit.
        self.assertEqual(SurveyStatus.objects.filter(feature__project=self.copy).count(), 3)

        response = self.client.post(f'/api/projects/{self.copy.id}/submit/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'submitted')

        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, 'submitted')

        # The planner sees what the tester recorded: hazards, risks, trenches.
        hazards = self.client.get('/api/survey/hazards/')
        self.assertEqual(hazards.data['count'], 3)
        risks = self.client.get('/api/survey/risks/')
        self.assertEqual(risks.data['count'], 2)
        trenches = self.client.get('/api/survey/trenches/?trench_type=new_trench')
        self.assertEqual(trenches.data['count'], 1)

        # A stranger's submit on this case is refused, not silently accepted.
        outsider = make_user('outsider@example.com', role=User.Role.ENGINEER)
        self.copy.status = 'active'
        self.copy.save(update_fields=['status'])
        self.client.force_authenticate(user=outsider)
        denied = self.client.post(f'/api/projects/{self.copy.id}/submit/')
        self.assertEqual(denied.status_code, 403, denied.data)
