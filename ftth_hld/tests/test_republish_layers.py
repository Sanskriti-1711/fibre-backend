"""Republishing must replace the stored design, not add to it.

`sync_project_layers` skips a layer whose NAME is already stored, which is what
a poll needs (a run gaining a layer must not rewrite thousands of features every
few seconds) and what left one project's map showing 810-duct geometry from the
previous design beside 129 ducts in the engine: its pole and aerial stages wrote
two hours after its publish, and every name it already had was never refreshed.

These tests pin the forced path — every engine layer re-persisted, engine-owned
layers the run no longer has removed, and the platform's derived layers left to
the chain that owns them.
"""

from __future__ import annotations

import json
from unittest import mock

from django.test import TestCase

from ftth_hld import pipeline
from ftth_hld.models import FtthLayer
from testutils.factories import make_ftth_project, make_hld_layer


def _geojson(count: int) -> bytes:
    return json.dumps(
        {
            'type': 'FeatureCollection',
            'features': [
                {
                    'type': 'Feature',
                    'geometry': {'type': 'Point', 'coordinates': [13.4, 52.4]},
                    'properties': {'fid': fid},
                }
                for fid in range(count)
            ],
        }
    ).encode()


class RepublishTests(TestCase):
    def setUp(self):
        self.ftth = make_ftth_project()

    def _republish(self, counts: dict):
        status = {'layers': [{'name': name, 'feature_count': n} for name, n in counts.items()]}
        with (
            mock.patch.object(pipeline, 'get_status', return_value=status),
            mock.patch.object(
                pipeline,
                'get_layer_geojson',
                side_effect=lambda _project_id, name: _geojson(counts[name]),
            ),
        ):
            return pipeline.republish_project_layers(self.ftth.project_id)

    def _count(self, name: str):
        return (
            FtthLayer.objects.filter(ftth_project=self.ftth, name=name)
            .values_list('feature_count', flat=True)
            .first()
        )

    def test_a_layer_whose_content_changed_is_replaced(self):
        make_hld_layer(self.ftth, name='ducts', feature_count=810)
        self._republish({'ducts': 129, 'objects': 285})
        self.assertEqual(self._count('ducts'), 129)

    def test_a_layer_the_run_no_longer_has_is_removed(self):
        """A re-run that drops the couplers must not leave them on the map."""
        make_hld_layer(self.ftth, name='coupleurs', feature_count=288)
        result = self._republish({'ducts': 129})
        self.assertIn('coupleurs', result['removed'])
        self.assertIsNone(self._count('coupleurs'))

    def test_a_layer_the_run_gained_is_published(self):
        make_hld_layer(self.ftth, name='ducts', feature_count=810)
        self._republish({'ducts': 129, 'poles': 235, 'aerial_spans': 302})
        self.assertEqual(self._count('poles'), 235)
        self.assertEqual(self._count('aerial_spans'), 302)

    def test_a_derived_layer_is_not_pruned(self):
        """`trench_sections` has no engine counterpart — the chain owns it."""
        make_hld_layer(self.ftth, name='trench_sections', feature_count=467)
        result = self._republish({'ducts': 129})
        self.assertNotIn('trench_sections', result['removed'])
        self.assertEqual(self._count('trench_sections'), 467)

    def test_persisted_features_keep_their_feature_ids(self):
        make_hld_layer(self.ftth, name='ducts', feature_count=810)
        self._republish({'ducts': 2})
        row = FtthLayer.objects.get(ftth_project=self.ftth, name='ducts')
        ids = [f['properties'].get('feature_id') for f in row.geojson['features']]
        self.assertEqual(ids, ['ducts-001', 'ducts-002'])


class PollingSyncTests(TestCase):
    """The poll path must stay additive — the engine clears gis mid-run."""

    def setUp(self):
        self.ftth = make_ftth_project()

    def test_the_default_sync_leaves_stored_layers_alone(self):
        make_hld_layer(self.ftth, name='ducts', feature_count=810)
        status = {'layers': [{'name': 'ducts', 'feature_count': 129}]}
        with (
            mock.patch.object(pipeline, 'get_status', return_value=status),
            mock.patch.object(pipeline, 'get_layer_geojson', return_value=_geojson(129)),
        ):
            counts = pipeline.sync_project_layers(self.ftth.project_id)
        self.assertEqual(counts, {})
        row = FtthLayer.objects.get(ftth_project=self.ftth, name='ducts')
        self.assertEqual(row.feature_count, 810)

    def test_the_default_sync_still_adds_a_missing_layer(self):
        status = {'layers': [{'name': 'poles', 'feature_count': 235}]}
        with (
            mock.patch.object(pipeline, 'get_status', return_value=status),
            mock.patch.object(pipeline, 'get_layer_geojson', return_value=_geojson(235)),
        ):
            counts = pipeline.sync_project_layers(self.ftth.project_id)
        self.assertEqual(counts, {'poles': 235})
