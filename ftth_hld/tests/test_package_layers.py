"""Both packages must be assembled from the run's CURRENT files.

The design and survey packages are built from the engine's ``downloads`` list,
so that list decides which layers a field crew or a design engineer receives.
It was read from ``_read_status`` — the disk cache of the last status poll,
which nothing invalidates — so a package built after a run finished late was
assembled from the list that predated the run's last layers.  One project's
pole and aerial stages wrote two hours after its publish, and ``Poles.gpkg``
plus all three ``Aerial_*`` layers were missing from both packages while their
files sat on disk next to the cache.

These tests pin the source of truth: the engine answers, the cache is only the
fallback for an engine that cannot be reached.
"""

from __future__ import annotations

import io
import json
import zipfile
from unittest import mock

from django.test import SimpleTestCase

from ftth_hld import pipeline


def _feature_collection(count: int = 1) -> bytes:
    """A small WGS84 FeatureCollection, as the engine serves one."""
    return json.dumps(
        {
            'type': 'FeatureCollection',
            'crs': {'type': 'name', 'properties': {'name': 'EPSG:4326'}},
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


def _status(*names: str) -> dict:
    return {'downloads': [{'name': name} for name in names]}


class DeliverableNameTests(SimpleTestCase):
    PROJECT = 'a' * 32

    def test_the_engine_answers_and_the_stale_cache_is_not_trusted(self):
        with (
            mock.patch.object(pipeline, 'get_status', return_value=_status('Poles.gpkg')),
            mock.patch.object(pipeline, '_read_status', return_value=_status('Objects.gpkg')),
        ):
            self.assertEqual(pipeline._engine_deliverable_names(self.PROJECT), ['Poles.gpkg'])

    def test_raw_inputs_are_not_deliverables(self):
        with mock.patch.object(
            pipeline,
            'get_status',
            return_value=_status('Poles.gpkg', 'inputs/Main_DataSet.xlsx',
                                 'brownfield/bf_ducts.geojson'),
        ):
            self.assertEqual(pipeline._engine_deliverable_names(self.PROJECT), ['Poles.gpkg'])

    def test_known_geojson_layers_come_in_reprojected_via_the_map(self):
        """A mapped layer is added from DESIGN/SURVEY_GEOJSON_FILES, not raw."""
        with mock.patch.object(
            pipeline,
            'get_status',
            return_value=_status('Aerial_Spans.gpkg', 'Aerial_Spans.geojson',
                                 'Poles.geojson', 'Poles.gpkg'),
        ):
            names = pipeline._engine_deliverable_names(self.PROJECT)
        self.assertIn('Poles.gpkg', names)
        # Poles/Aerial are in both maps, so their raw GeoJSON is not repeated.
        self.assertNotIn('Poles.geojson', names)
        self.assertNotIn('Aerial_Spans.geojson', names)

    def test_a_duplicate_name_is_reported_once(self):
        with mock.patch.object(
            pipeline, 'get_status', return_value=_status('Poles.gpkg', 'Poles.gpkg')
        ):
            self.assertEqual(pipeline._engine_deliverable_names(self.PROJECT), ['Poles.gpkg'])


class PackageContentsTests(SimpleTestCase):
    """The package a user downloads carries the layers the run produced."""

    PROJECT = 'b' * 32

    def _entries(self, blob: bytes) -> list:
        return sorted(zipfile.ZipFile(io.BytesIO(blob)).namelist())

    def test_a_layer_the_cache_never_saw_reaches_the_design_package(self):
        fresh = _status('Aerial_Spans.gpkg', 'Aerial_Drops.gpkg', 'Poles.gpkg',
                        'Aerial_Spans.geojson', 'Poles.geojson')
        stale = _status('Objects.gpkg')

        def fake_download(_project_id: str, name: str):
            if name.endswith('.geojson'):
                return _feature_collection(2)
            return b'gpkg-bytes'

        with (
            mock.patch.object(pipeline, 'get_status', return_value=fresh),
            mock.patch.object(pipeline, '_read_status', return_value=stale),
            mock.patch.object(pipeline, 'get_download_file', side_effect=fake_download),
        ):
            entries = self._entries(pipeline.generate_design_package(self.PROJECT))

        self.assertIn('Aerial_Spans.gpkg', entries)
        self.assertIn('Aerial_Drops.gpkg', entries)
        self.assertIn('Poles.gpkg', entries)
        # Known layers arrive reprojected under their lower-case zip name.
        self.assertIn('aerial_spans.geojson', entries)
        self.assertIn('poles.geojson', entries)

    def test_the_survey_package_carries_the_aerial_chain(self):
        fresh = _status('Aerial_Spans.gpkg', 'Aerial_Cable.gpkg', 'Aerial_Spans.geojson')

        with (
            mock.patch.object(pipeline, 'get_status', return_value=fresh),
            mock.patch.object(pipeline, '_read_status', return_value=_status()),
            mock.patch.object(pipeline, 'get_download_file',
                              side_effect=lambda _p, n: b'gpkg-bytes' if n.endswith('.gpkg')
                              else _feature_collection(1)),
        ):
            entries = self._entries(pipeline.generate_survey_package(self.PROJECT))

        self.assertIn('Aerial_Spans.gpkg', entries)
        self.assertIn('Aerial_Cable.gpkg', entries)
        self.assertIn('aerial_spans.geojson', entries)

    def test_the_curated_list_is_the_fallback_for_an_unreachable_engine(self):
        with (
            mock.patch.object(pipeline, 'get_status', return_value={'downloads': []}),
            mock.patch.object(
                pipeline,
                'get_download_file',
                side_effect=lambda _p, n: b'gpkg-bytes' if n.endswith('.gpkg')
                else _feature_collection(1),
            ),
        ):
            entries = self._entries(pipeline.generate_design_package(self.PROJECT))
        self.assertIn('Objects.gpkg', entries)
