"""Regression tests for the mypy-driven hardening of the HLD pipeline.

Pins the runtime behaviour behind the recent type fixes so they cannot
silently regress:

* ``pipeline._read_status`` — corrupt or wrongly-shaped cache files read as
  missing (None) instead of raising or leaking a non-dict.
* Engine-response wrappers — ``resp.json()`` payloads come back in the
  declared container type (``dict`` vs ``list``).
* ``boq.reused_metres`` — a construction value with no rule key contributes
  nothing instead of poisoning the totals.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from ftth_hld import boq, pipeline


def _resp(status_code: int, payload):
    response = mock.Mock()
    response.status_code = status_code
    response.json = mock.Mock(return_value=payload)
    return response


class ReadStatusTests(SimpleTestCase):
    def _cached(self, project_id: str, raw: str | None) -> Path:
        tmp = Path(self._tmp.name) / project_id
        tmp.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            (tmp / 'status.json').write_text(raw)
        return tmp

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(pipeline, 'HOST_OUTPUTS_DIR', Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_valid_dict_is_returned(self):
        self._cached('p1', json.dumps({'project_id': 'p1', 'state': 'done'}))
        self.assertEqual(pipeline._read_status('p1'), {'project_id': 'p1', 'state': 'done'})

    def test_corrupt_json_reads_as_missing(self):
        self._cached('p2', '{not json')
        self.assertIsNone(pipeline._read_status('p2'))

    def test_non_dict_json_reads_as_missing(self):
        self._cached('p3', json.dumps([1, 2, 3]))
        self.assertIsNone(pipeline._read_status('p3'))

    def test_missing_file_reads_as_missing(self):
        self._cached('p4', None)
        self.assertIsNone(pipeline._read_status('p4'))


class EngineWrapperTypeTests(SimpleTestCase):
    def test_list_projects_returns_a_list(self):
        payload = [{'project_id': 'a'}, {'project_id': 'b'}]
        with mock.patch.object(pipeline, 'requests') as mock_requests:
            mock_requests.get.return_value = _resp(200, payload)
            result = pipeline.list_projects()
        self.assertIsInstance(result, list)
        self.assertEqual(result, payload)

    def test_suggest_places_returns_a_dict(self):
        payload = {'query': 'ber', 'places': [{'name': 'Berlin'}]}
        with mock.patch.object(pipeline, 'requests') as mock_requests:
            mock_requests.get.return_value = _resp(200, payload)
            result = pipeline.suggest_places('ber')
        self.assertIsInstance(result, dict)
        self.assertEqual(result, payload)

    def test_surface_ai_review_returns_dict_on_200_and_none_on_404(self):
        payload = {'project_id': 'p1', 'findings': []}
        with mock.patch.object(pipeline, 'requests') as mock_requests:
            mock_requests.get.return_value = _resp(200, payload)
            self.assertEqual(pipeline.get_surface_ai_review('p1'), payload)
            mock_requests.get.return_value = _resp(404, {'detail': 'nope'})
            self.assertIsNone(pipeline.get_surface_ai_review('p1'))


class ReusedMetresNoneSkipTests(SimpleTestCase):
    def _totals(self, features):
        class Layer:
            def __init__(self, feats):
                self.geojson = {'type': 'FeatureCollection', 'features': feats}

        layers = {
            'trenches': Layer(features),
            'ducts': Layer([]),
            'cables': Layer([]),
        }
        with mock.patch.object(
            boq, '_get_layer', side_effect=lambda _project, name: layers.get(name)
        ):
            return boq.reused_metres('test-project')

    @staticmethod
    def _trench(construct, reuse_m=12.0):
        return {
            'type': 'Feature',
            'geometry': None,
            'properties': {
                'construct': construct,
                'INFRA_STATUS': 'existing',
                'REUSE_LEN_M': reuse_m,
            },
        }

    def test_known_construct_counts_reused_metres(self):
        totals = self._totals([self._trench('Open Cut')])
        self.assertEqual(totals, {'2.1': 12.0})

    def test_unknown_construct_contributes_nothing(self):
        totals = self._totals([self._trench('MicroTrench')])
        self.assertEqual(totals, {})
        self.assertNotIn(None, totals)
