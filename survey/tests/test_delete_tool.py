"""Delete tool: a removal recorded by the app must survive the write.

The map's Delete tool is a logical delete for HLD-backed features: it PATCHes
``survey_status='removed'`` (and the upsert endpoint creates the marker when no
survey copy exists yet). The view used to overwrite that status with the
automatic ``new -> modified`` transition whenever geometry or attributes came
along in the same request, so the removal quietly became an edit and the
planner never saw it as a removal.
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from survey.models import SurveyFeature
from testutils.factories import (
    make_feature,
    make_ftth_project,
    make_survey_copy,
    make_survey_feature,
    make_user,
)
from users.models import User

LINE = {
    'type': 'LineString',
    'coordinates': [[13.3750, 52.4485], [13.3760, 52.4486]],
}


class DeleteToolStatusTestCase(TestCase):
    def setUp(self):
        self.engineer = make_user('tester.delete@example.com', role=User.Role.ENGINEER)
        self.copy = make_survey_copy(
            make_ftth_project(name='Tester run: delete tool'),
            name='Tester run: delete tool',
        )
        self.hld_feature = make_feature(self.copy)
        self.client = APIClient()
        self.client.force_authenticate(user=self.engineer)

    def test_patch_status_removed_wins_over_geometry_transition(self):
        sf = make_survey_feature(
            self.copy,
            self.engineer,
            original_hld_feature=self.hld_feature,
            status=SurveyFeature.SurveyStatus.NEW,
        )
        response = self.client.patch(
            f'/api/survey/survey-features/{sf.id}/',
            {
                'survey_status': 'removed',
                'survey_geometry': LINE,
                'change_reason': 'feature removed from survey',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['survey_status'], 'removed')
        sf.refresh_from_db()
        self.assertEqual(sf.survey_status, SurveyFeature.SurveyStatus.REMOVED)
        self.assertEqual(sf.version_number, 2)
        self.assertEqual(sf.sync_status, SurveyFeature.SyncState.PENDING)

    def test_patch_without_status_still_transitions_to_modified(self):
        sf = make_survey_feature(
            self.copy,
            self.engineer,
            original_hld_feature=self.hld_feature,
            status=SurveyFeature.SurveyStatus.NEW,
        )
        response = self.client.patch(
            f'/api/survey/survey-features/{sf.id}/',
            {'survey_geometry': LINE},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['survey_status'], 'modified')

    def test_upsert_creates_removal_marker(self):
        response = self.client.post(
            '/api/survey/survey-features/upsert/',
            {
                'original_hld_feature': str(self.hld_feature.id),
                'project': str(self.copy.id),
                'layer_id': 'trench_layer',
                'layer_name': 'Feeder_Trench',
                'survey_geometry': LINE,
                'survey_attributes': {},
                'original_geometry': LINE,
                'original_attributes': {},
                'change_reason': 'feature removed from survey',
                'survey_status': 'removed',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['survey_status'], 'removed')
        sf = SurveyFeature.objects.get(id=response.data['id'])
        self.assertEqual(sf.survey_status, SurveyFeature.SurveyStatus.REMOVED)
        self.assertEqual(sf.original_hld_feature_id, self.hld_feature.id)

    def test_upsert_keeps_removed_status_on_a_later_save(self):
        sf = make_survey_feature(self.copy, self.engineer, original_hld_feature=self.hld_feature)
        response = self.client.post(
            '/api/survey/survey-features/upsert/',
            {
                'original_hld_feature': str(self.hld_feature.id),
                'project': str(self.copy.id),
                'layer_id': 'trench_layer',
                'layer_name': 'Feeder_Trench',
                'survey_geometry': LINE,
                'change_reason': 'feature removed from survey',
                'survey_status': 'removed',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['survey_status'], 'removed')
        sf.refresh_from_db()
        self.assertEqual(sf.survey_status, SurveyFeature.SurveyStatus.REMOVED)

    def test_removed_row_is_kept_and_a_hard_delete_drops_it(self):
        sf = make_survey_feature(self.copy, self.engineer, original_hld_feature=self.hld_feature)
        self.client.patch(
            f'/api/survey/survey-features/{sf.id}/',
            {'survey_status': 'removed'},
            format='json',
        )
        self.assertTrue(SurveyFeature.objects.filter(id=sf.id).exists())

        # Engineer-created features have no design row to keep: hard delete.
        local = make_survey_feature(self.copy, self.engineer)
        deleted = self.client.delete(f'/api/survey/survey-features/{local.id}/')
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(SurveyFeature.objects.filter(id=local.id).exists())
        self.assertEqual(
            self.client.get(f'/api/survey/survey-features/{local.id}/').status_code, 404
        )
