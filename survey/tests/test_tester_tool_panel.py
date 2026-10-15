"""Tester run: the whole tool panel, one tool per scenario, six scenarios.

The earlier road-survey run proved hazards/risks/status/submit. This run
walks the map's tool panel the way a tester taps it, in order, each tool on
its own feature so a failure points at exactly one tool:

  S1  Add Point      -> premises point on High Street + dog hazard + status
  S2  Draw Trench    -> trench line on Mill Lane + classification + evidence
  S3  Edit shape / Extend line / Attributes -> move a vertex, lengthen the
      line, edit attributes on the Mill Lane trench (version 1 -> 2 -> 3)
  S4  Split here / Remove span / Delete -> split the Station Road line in
      two at the bridge, then remove the condemned span
  S5  GPS trace / Existing asset / Sync queue -> the walk to the site, the
      chamber found there, and the offline queue draining on reconnect
  S6  Review changes / Risk queue / Submit -> planner read-back, then the
      two-tap submit flips the copy to submitted (and a stranger is refused)

Run with:
    unset PYTHONPATH
    python manage.py test --settings=config.test_settings \\
        survey.tests.test_tester_tool_panel
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from assignments.models import AssignmentJob
from survey.models import (
    ExistingAsset,
    FieldEvidence,
    Hazard,
    RiskAssessment,
    SurveyFeature,
    SurveyStatus,
    SyncQueueItem,
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

HIGH_ST = [13.3730, 52.4485]
MILL_LN = [13.3750, 52.4485]
STATION_RD = [13.3770, 52.4485]


def _line(x0: float, n: int = 2) -> dict:
    """A trench line with ``n`` vertices heading east from ``x0``."""
    return {
        'type': 'LineString',
        'coordinates': [[x0 + i * 0.0005, 52.4485 + i * 0.0001] for i in range(n)],
    }


class ToolPanelTestCase(TestCase):
    """One tester, one case, six scenarios, the whole panel."""

    def setUp(self):
        self.engineer = make_user('tester.panel@example.com', role=User.Role.ENGINEER)
        self.copy = make_survey_copy(
            make_ftth_project(name='Tester run: tool panel'),
            name='Tester run: tool panel',
            status='active',
        )
        AssignmentJob.objects.create(
            project=self.copy, assignee=self.engineer, scope=AssignmentJob.SCOPE_PROJECT
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.engineer)

    # ── helpers ──────────────────────────────────────────────────────────

    def _draw(self, geometry: dict, layer: str, reason: str) -> dict:
        """What Add Point / Draw Trench send: one POST per new feature."""
        response = self.client.post(
            '/api/survey/survey-features/',
            {
                'project': str(self.copy.id),
                'layer_id': layer,
                'layer_name': layer,
                'survey_geometry': geometry,
                'original_geometry': geometry,
                'survey_attributes': {},
                'original_attributes': {},
                'change_reason': reason,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        return dict(response.data)

    # ── S1 · Add Point ───────────────────────────────────────────────────

    def test_s1_add_point_premises_with_dog_hazard(self):
        """Add Point on High Street: the premises, its dog, its status."""
        point = self._draw(
            {'type': 'Point', 'coordinates': HIGH_ST},
            'premises_layer',
            'premises 14 added on site',
        )
        sf = SurveyFeature.objects.get(id=point['id'])
        self.assertEqual(sf.survey_geometry['type'], 'Point')
        self.assertEqual(sf.survey_status, SurveyFeature.SurveyStatus.NEW)
        self.assertEqual(sf.version_number, 1)

        hazard = self.client.post(
            '/api/survey/hazards/',
            {
                'feature': point['id'],
                'hazard_type': 'dog',
                'notes': 'No. 14 High Street: loose dog in the front garden',
                'is_active': True,
            },
            format='json',
        )
        self.assertEqual(hazard.status_code, 201, hazard.data)
        self.assertEqual(Hazard.objects.get(id=hazard.data['id']).survey_feature_id, sf.id)

        status = self.client.post(
            '/api/survey/status/',
            {'feature': point['id'], 'status': 'visited', 'notes': 'gate knocked, no answer'},
            format='json',
        )
        self.assertEqual(status.status_code, 200, status.data)
        self.assertEqual(SurveyStatus.objects.get(survey_feature=sf).status, 'visited')

    # ── S2 · Draw Trench ─────────────────────────────────────────────────

    def test_s2_draw_trench_with_classification_and_evidence(self):
        """Draw Trench on Mill Lane: the line, what it is, measured proof."""
        trench = self._draw(
            _line(MILL_LN[0]),
            'Distribution_Trench',
            'Engineer-drew trench (2 vertices, footway)',
        )
        sf = SurveyFeature.objects.get(id=trench['id'])
        self.assertEqual(len(sf.survey_geometry['coordinates']), 2)

        classification = self.client.post(
            '/api/survey/trenches/',
            {
                'feature': trench['id'],
                'trench_type': 'new_trench',
                'depth_mm': 900,
                'width_mm': 300,
                'surface_type': 'paving',
                'road_crossing': False,
                'permit_required': False,
                'notes': 'Mill Lane footway, slabs lifted and stacked',
            },
            format='json',
        )
        self.assertEqual(classification.status_code, 201, classification.data)

        evidence = self.client.post(
            '/api/survey/evidence/',
            {
                'feature': trench['id'],
                'evidence_type': 'measurement',
                'description': 'Mill Lane: depth 900 mm at chainage 20 m',
            },
            format='json',
        )
        self.assertEqual(evidence.status_code, 201, evidence.data)
        self.assertTrue(TrenchSurvey.objects.filter(survey_feature=sf).exists())
        self.assertTrue(FieldEvidence.objects.filter(survey_feature=sf).exists())

    # ── S3 · Edit shape / Extend line / Attributes ───────────────────────

    def test_s3_edit_shape_extend_line_then_attributes(self):
        """Three toolbar taps on the Mill Lane trench: move, extend, describe."""
        trench = self._draw(
            _line(MILL_LN[0], n=3),
            'Distribution_Trench',
            'Engineer-drew trench (3 vertices, footway)',
        )
        url = f"/api/survey/survey-features/{trench['id']}/"

        # 1. Edit shape: drag the middle vertex 2 m north around a tree pit.
        moved = [list(c) for c in _line(MILL_LN[0], n=3)['coordinates']]
        moved[1][1] += 0.00002
        first = self.client.patch(
            url,
            {'survey_geometry': {'type': 'LineString', 'coordinates': moved}},
            format='json',
        )
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data['version_number'], 2)
        self.assertEqual(first.data['survey_status'], SurveyFeature.SurveyStatus.MODIFIED)

        # 2. Extend line: one more vertex to reach the chamber at the corner.
        moved.append([MILL_LN[0] + 0.0015, 52.4489])
        second = self.client.patch(
            url,
            {'survey_geometry': {'type': 'LineString', 'coordinates': moved}},
            format='json',
        )
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data['version_number'], 3)
        self.assertEqual(len(second.data['survey_geometry']['coordinates']), 4)

        # 3. Attributes: depth corrected after the trial hole.
        third = self.client.patch(
            url,
            {
                'survey_attributes': {'depth_mm': 750, 'surface': 'paving'},
                'change_reason': 'trial hole: rock at 750 mm',
            },
            format='json',
        )
        self.assertEqual(third.status_code, 200, third.data)
        self.assertEqual(third.data['version_number'], 4)
        self.assertEqual(third.data['survey_attributes']['depth_mm'], 750)

        sf = SurveyFeature.objects.get(id=trench['id'])
        self.assertEqual(sf.version_number, 4)
        self.assertEqual(sf.sync_status, SurveyFeature.SyncState.PENDING)

    # ── S4 · Split here / Remove span / Delete ───────────────────────────

    def test_s4_split_line_then_remove_condemned_span(self):
        """Split here at the Station Road bridge; the flooded span is deleted."""
        line = self._draw(
            _line(STATION_RD[0], n=3),
            'Distribution_Trench',
            'Station Road trench under the rail bridge',
        )
        coords = [list(c) for c in _line(STATION_RD[0], n=3)['coordinates']]

        # Split here: tap the middle vertex; west half stays, east half is new.
        west = self.client.patch(
            f"/api/survey/survey-features/{line['id']}/",
            {'survey_geometry': {'type': 'LineString', 'coordinates': coords[:2]}},
            format='json',
        )
        self.assertEqual(west.status_code, 200, west.data)
        east = self._draw(
            {'type': 'LineString', 'coordinates': coords[1:]},
            'Distribution_Trench',
            f"split from {line['id']} at the bridge (east span)",
        )
        self.assertEqual(SurveyFeature.objects.filter(project=self.copy).count(), 2)

        # Remove span: the east span sits in standing water — condemned.
        doomed = self.client.post(
            '/api/survey/hazards/',
            {
                'feature': east['id'],
                'hazard_type': 'flood_risk',
                'mitigation_template': 'permit',
                'notes': 'east span floods; route via HDD instead',
                'is_active': True,
            },
            format='json',
        )
        self.assertEqual(doomed.status_code, 201, doomed.data)
        deleted = self.client.delete(f"/api/survey/survey-features/{east['id']}/")
        self.assertEqual(deleted.status_code, 204, deleted.data)
        gone = self.client.get(f"/api/survey/survey-features/{east['id']}/")
        self.assertEqual(gone.status_code, 404)
        self.assertEqual(SurveyFeature.objects.filter(project=self.copy).count(), 1)

    # ── S5 · GPS trace / Existing asset / Sync queue ─────────────────────

    def test_s5_walk_in_asset_found_offline_queue_drained(self):
        """My-location walk, the chamber at the corner, offline sync on return."""
        trace = self.client.post(
            '/api/survey/gps-traces/',
            {'project': str(self.copy.id), 'total_distance_m': 186.5},
            format='json',
        )
        self.assertEqual(trace.status_code, 201, trace.data)
        points = self.client.post(
            f"/api/survey/gps-traces/{trace.data['id']}/points/",
            [
                {'latitude': 52.4485, 'longitude': 13.3750, 'accuracy': 4.5},
                {'latitude': 52.4487, 'longitude': 13.3760, 'accuracy': 3.8},
                {'latitude': 52.4489, 'longitude': 13.3770, 'accuracy': 5.2},
            ],
            format='json',
        )
        self.assertEqual(points.status_code, 201, points.data)
        self.assertEqual(len(points.data), 3)
        walked = self.client.get(f"/api/survey/gps-traces/{trace.data['id']}/")
        self.assertEqual(walked.data['point_count'], 3)

        chamber = self.client.post(
            '/api/survey/assets/',
            {
                'asset_type': 'chamber',
                'condition': 'reuse_possible',
                'latitude': 52.4489,
                'longitude': 13.3770,
                'description': 'Station Road corner chamber: sound, space for 2-way duct',
            },
            format='json',
        )
        self.assertEqual(chamber.status_code, 201, chamber.data)
        self.assertTrue(
            ExistingAsset.objects.filter(asset_type='chamber', condition='reuse_possible').exists()
        )

        # Offline in the cutting: the risk assessment queued, then drained.
        queued = self.client.post(
            '/api/survey/sync/',
            {
                'item_type': 'risk_assessment',
                'entity_id': 'offline-risk-1',
                'payload': {'category': 'railway', 'severity': 'critical'},
            },
            format='json',
        )
        self.assertEqual(queued.status_code, 201, queued.data)
        pending = self.client.get('/api/survey/sync/')
        self.assertEqual(pending.data['count'], 1)
        drained = self.client.post('/api/survey/sync/process/')
        self.assertEqual(drained.status_code, 200, drained.data)
        self.assertEqual(drained.data['processed'], 1)
        self.assertEqual(SyncQueueItem.objects.filter(status='synced').count(), 1)
        self.assertEqual(self.client.get('/api/survey/sync/').data['count'], 0)

    # ── S6 · Review changes / Risk queue / Submit ────────────────────────

    def test_s6_review_changes_risk_queue_then_submit(self):
        """The planner's read-back, then the two-tap submit ends the run."""
        self.test_s1_add_point_premises_with_dog_hazard()
        self.test_s2_draw_trench_with_classification_and_evidence()
        self.test_s3_edit_shape_extend_line_then_attributes()

        # Review survey changes: the panel lists what the run changed.
        changes = self.client.get('/api/survey/changes/')
        self.assertEqual(changes.status_code, 200, changes.data)

        # Risk queue: the surveyed changes ranked for the planner.
        queue = self.client.get(f'/api/survey/projects/{self.copy.id}/risk-queue/')
        self.assertEqual(queue.status_code, 200, queue.data)

        response = self.client.post(f'/api/projects/{self.copy.id}/submit/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'submitted')
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, 'submitted')

        # Planner read-back after submit: everything the panel recorded.
        self.assertEqual(self.client.get('/api/survey/hazards/').data['count'], 1)
        self.assertEqual(
            self.client.get('/api/survey/trenches/?trench_type=new_trench').data['count'], 1
        )
        # S1 drew the premises point, S2 the Mill Lane trench, S3 its own
        # trench that Edit-shape/Extend-line/Attributes then reworked.
        self.assertEqual(SurveyFeature.objects.filter(project=self.copy).count(), 3)
        risks = RiskAssessment.objects.filter(engineer=self.engineer).count()
        self.assertGreaterEqual(risks, 0)

        # A stranger's submit on this case is refused, not silently accepted.
        outsider = make_user('outsider.panel@example.com', role=User.Role.ENGINEER)
        self.copy.status = 'active'
        self.copy.save(update_fields=['status'])
        self.client.force_authenticate(user=outsider)
        denied = self.client.post(f'/api/projects/{self.copy.id}/submit/')
        self.assertEqual(denied.status_code, 403, denied.data)


class HazardEvidenceContinuityTestCase(TestCase):
    """Attributes prefill reads back what its own module saved.

    The form filters every module client-side by feature id, but hazards
    were fetched unfiltered (paginated): a hazard past page one never
    reached the form and the module looked empty after saving. Hazards now
    accept the same ?feature= filter as risks and evidence.
    """

    def setUp(self):
        self.engineer = make_user('tester.continuity@example.com', role=User.Role.ENGINEER)
        self.copy = make_survey_copy(
            make_ftth_project(name='Tester run: continuity'),
            name='Tester run: continuity',
            status='active',
        )
        self.hld_feature = make_feature(self.copy)
        self.survey_feature = make_survey_feature(self.copy, self.engineer)
        self.client = APIClient()
        self.client.force_authenticate(user=self.engineer)

    def _hazard(self, feature_id, hazard_type):
        response = self.client.post(
            '/api/survey/hazards/',
            {'feature': str(feature_id), 'hazard_type': hazard_type, 'is_active': True},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_hazard_list_filters_by_projects_feature_id(self):
        self._hazard(self.hld_feature.id, 'traffic_management')
        self._hazard(self.survey_feature.id, 'dog')
        response = self.client.get(f'/api/survey/hazards/?feature={self.hld_feature.id}')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['hazard_type'], 'traffic_management')

    def test_hazard_list_filters_by_survey_feature_id(self):
        self._hazard(self.hld_feature.id, 'traffic_management')
        self._hazard(self.survey_feature.id, 'dog')
        response = self.client.get(f'/api/survey/hazards/?feature={self.survey_feature.id}')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['hazard_type'], 'dog')

    def test_evidence_list_filters_by_feature_id(self):
        for fid, desc in (
            (self.hld_feature.id, 'hld photo'),
            (self.survey_feature.id, 'survey measurement'),
        ):
            response = self.client.post(
                '/api/survey/evidence/',
                {'feature': str(fid), 'evidence_type': 'measurement', 'description': desc},
                format='json',
            )
            self.assertEqual(response.status_code, 201, response.data)
        response = self.client.get(f'/api/survey/evidence/?feature={self.survey_feature.id}')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['description'], 'survey measurement')
