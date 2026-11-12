"""Re-syncing a supervision project from its (re-)exported LLD run.

INTEGRATION_PLAN.md §3 Stage 4.4: an explicit re-sync must refresh the planned
baseline of work nobody has touched yet, and re-binding to a newer run must be
an explicit, recorded act — never a silent re-baseline over approved work.
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from ftth_lld.models import LldRun
from projects.models import Feature, Project, StageEvent
from testutils.factories import make_ftth_project, make_lld_layer, make_lld_run, make_user
from users.models import User

LINE = {
    'type': 'LineString',
    'coordinates': [
        [13.373030306946523, 52.44855475307314],
        [13.373090289114723, 52.44858710395022],
    ],
}


def layer_geojson(depth=600, feature_id='1'):
    return {
        'type': 'FeatureCollection',
        'features': [
            {
                'type': 'Feature',
                'geometry': LINE,
                'properties': {
                    'layer': 'final_trenches',
                    'feature_id': feature_id,
                    'depth_mm': depth,
                },
            }
        ],
    }


class LldResyncTests(TestCase):
    def setUp(self):
        self.user = make_user('resync@example.com', role=User.Role.SUBADMIN)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.ftth = make_ftth_project()
        self.run = make_lld_run(self.ftth, version='LLD-V01', status=LldRun.STATUS_COMPLETED)
        make_lld_layer(self.run, name='final_trenches', geojson=layer_geojson())
        self.project = Project.objects.create(
            name='Supervision of LLD-V01',
            source_lld_run_id=f'{self.ftth.project_id}:LLD-V01',
            business_meta={'origin': 'lld_run'},
        )
        self.url = f'/api/projects/{self.project.id}/import/from-lld/'

    def _first_import(self):
        response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['features_created'], 1)

    def test_a_re_export_refreshes_pending_features(self):
        self._first_import()

        # The same version exported again with a corrected planned depth.
        layer = self.run.layers.get()
        layer.geojson = layer_geojson(depth=900)
        layer.save()

        response = self.client.post(self.url, {}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['features_updated'], 1)
        self.assertEqual(response.data['features_created'], 0)
        feature = Feature.objects.get(project=self.project)
        self.assertEqual(feature.properties['depth_mm'], 900)
        self.assertTrue(
            StageEvent.objects.filter(project=self.project, event='supervision_resynced').exists()
        )

    def test_a_re_export_never_touches_work_already_acted_on(self):
        self._first_import()
        feature = Feature.objects.get(project=self.project)
        Feature.objects.filter(pk=feature.pk).update(status=Feature.STATUS_APPROVED)

        layer = self.run.layers.get()
        layer.geojson = layer_geojson(depth=900)
        layer.save()

        response = self.client.post(self.url, {}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['features_updated'], 0)
        self.assertEqual(response.data['features_kept'], 1)
        feature.refresh_from_db()
        self.assertEqual(feature.status, Feature.STATUS_APPROVED)
        self.assertEqual(feature.properties['depth_mm'], 600)

    def test_rebinding_to_a_newer_completed_run_is_explicit_and_visible(self):
        newer = make_lld_run(self.ftth, version='LLD-V02', status=LldRun.STATUS_COMPLETED)
        make_lld_layer(newer, name='final_trenches', geojson=layer_geojson(feature_id='2'))

        response = self.client.post(
            self.url,
            {'source_lld_run_id': f'{self.ftth.project_id}:LLD-V02'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['rebound'])
        self.project.refresh_from_db()
        self.assertEqual(self.project.source_lld_run_id, f'{self.ftth.project_id}:LLD-V02')
        self.assertTrue(
            Feature.objects.filter(project=self.project, properties__feature_id='2').exists()
        )
        event = StageEvent.objects.get(project=self.project, event='supervision_resynced')
        self.assertEqual(event.entity_id, 'LLD-V02')
        self.assertEqual(event.metadata['rebound_from'], f'{self.ftth.project_id}:LLD-V01')

    def test_rebinding_to_an_incomplete_run_is_refused(self):
        make_lld_run(self.ftth, version='LLD-V09', status=LldRun.STATUS_RUNNING)

        response = self.client.post(
            self.url,
            {'source_lld_run_id': f'{self.ftth.project_id}:LLD-V09'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.project.refresh_from_db()
        self.assertEqual(self.project.source_lld_run_id, f'{self.ftth.project_id}:LLD-V01')

    def test_a_gpkg_project_cannot_be_re_synced(self):
        gpkg_project = Project.objects.create(name='Contractor upload')
        response = self.client.post(
            f'/api/projects/{gpkg_project.id}/import/from-lld/', {}, format='json'
        )

        self.assertEqual(response.status_code, 400)
