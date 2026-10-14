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
            return_value=_status(
                'Poles.gpkg', 'inputs/Main_DataSet.xlsx', 'brownfield/bf_ducts.geojson'
            ),
        ):
            self.assertEqual(pipeline._engine_deliverable_names(self.PROJECT), ['Poles.gpkg'])

    def test_known_geojson_layers_come_in_reprojected_via_the_map(self):
        """A mapped layer is added from DESIGN/SURVEY_GEOJSON_FILES, not raw."""
        with mock.patch.object(
            pipeline,
            'get_status',
            return_value=_status(
                'Aerial_Spans.gpkg', 'Aerial_Spans.geojson', 'Poles.geojson', 'Poles.gpkg'
            ),
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

    def test_a_scratch_variant_is_not_a_deliverable(self):
        """The trench designer's per-variant runs are not layers the crew gets.

        The engine lists ``design_all/``, ``design_chain/``, ``design_crossings/``,
        ``design_defaults/``, ``design_score/``, ``design_score_compare/`` and
        ``brownfield_readback_check/`` beside the real outputs, each holding its
        own copy of ``Aerial_Drops`` and ``Final_Trenches``. Bundled, they gave
        the field engineer SIX copies of those two layers -- under the same
        layer names as the real ones -- and 2,640 of the survey package's 4,804
        rows. A deliverable is a file in the run's output root.
        """
        with mock.patch.object(
            pipeline,
            'get_status',
            return_value=_status(
                'Poles.gpkg',
                'design_all/Aerial_Drops.geojson',
                'design_all/Final_Trenches.gpkg',
                'design_chain/Final_Trenches.gpkg',
                'brownfield_readback_check/bf_feeder_trench.geojson',
            ),
        ):
            self.assertEqual(pipeline._engine_deliverable_names(self.PROJECT), ['Poles.gpkg'])


class EngineMissTests(SimpleTestCase):
    """A package build must not pay for the engine's missing files over and over.

    A run's output files live on the disk of the machine that ran the pipeline,
    but the engine keeps *listing* them for a run it restored from the shared
    database row. Measured against the live engine on 2026-10-13 for project
    24cd3573f4884a578c1b98a80bbd4040: 160 files listed, 160 requests, 160 x 404,
    ~54s of dead time inside a survey assignment that the hosting platform then
    killed with a 502. The verdict is bounded and temporary (see
    ENGINE_MISS_COOLDOWN) because the files can come back.
    """

    PROJECT = 'd' * 32

    def setUp(self):
        pipeline._ENGINE_FILE_MISSES.clear()
        self.addCleanup(pipeline._ENGINE_FILE_MISSES.clear)

    def _missing_response(self):
        class _Response:
            status_code = 404
            content = b''

        return _Response()

    def test_the_engine_is_asked_a_bounded_number_of_times(self):
        asked = []

        def fake_get(url, *args, **kwargs):
            asked.append(url)
            return self._missing_response()

        with (
            mock.patch.object(pipeline.requests, 'get', side_effect=fake_get),
            mock.patch.object(pipeline, '_db_layer_geojson', return_value=None),
        ):
            for i in range(40):
                self.assertIsNone(pipeline.get_download_file(self.PROJECT, f'Layer{i}.gpkg'))

        self.assertEqual(len(asked), pipeline.ENGINE_MISS_LIMIT)

    def test_fewer_misses_than_the_limit_do_not_write_the_engine_off(self):
        """One absent optional layer is a normal run, not a missing disk."""
        asked = []

        def fake_get(url, *args, **kwargs):
            asked.append(url)
            if url.endswith('Pseudo_HH.gpkg'):
                return self._missing_response()
            response = self._missing_response()
            response.status_code = 200
            response.content = b'gpkg-bytes'
            return response

        with (
            mock.patch.object(pipeline.requests, 'get', side_effect=fake_get),
            mock.patch.object(pipeline, '_db_layer_geojson', return_value=None),
        ):
            self.assertIsNone(pipeline.get_download_file(self.PROJECT, 'Pseudo_HH.gpkg'))
            self.assertEqual(pipeline.get_download_file(self.PROJECT, 'Poles.gpkg'), b'gpkg-bytes')

        self.assertEqual(len(asked), 2)
        self.assertEqual(pipeline._ENGINE_FILE_MISSES, {})

    def test_the_verdict_expires_and_a_file_found_then_clears_it(self):
        asked = []
        now = [1000.0]

        def fake_get(url, *args, **kwargs):
            asked.append(url)
            if url.endswith(('Poles.gpkg', 'Objects.gpkg')):
                response = self._missing_response()
                response.status_code = 200
                response.content = b'gpkg-bytes'
                return response
            return self._missing_response()

        with (
            mock.patch.object(pipeline.requests, 'get', side_effect=fake_get),
            mock.patch.object(pipeline, '_db_layer_geojson', return_value=None),
            mock.patch.object(pipeline.time, 'monotonic', side_effect=lambda: now[0]),
        ):
            for i in range(pipeline.ENGINE_MISS_LIMIT):
                pipeline.get_download_file(self.PROJECT, f'Layer{i}.gpkg')
            self.assertEqual(len(asked), pipeline.ENGINE_MISS_LIMIT)

            # Written off: the rest of this build costs no engine round trip.
            pipeline.get_download_file(self.PROJECT, 'Layer4.gpkg')
            self.assertEqual(len(asked), pipeline.ENGINE_MISS_LIMIT)

            # ... until the cooldown lapses and the engine is tried again.
            now[0] += pipeline.ENGINE_MISS_COOLDOWN + 1
            self.assertEqual(pipeline.get_download_file(self.PROJECT, 'Poles.gpkg'), b'gpkg-bytes')
            self.assertEqual(len(asked), pipeline.ENGINE_MISS_LIMIT + 1)
            self.assertEqual(pipeline._ENGINE_FILE_MISSES, {})

            # A file it does have clears the verdict, so a disk that comes back
            # is used from then on.
            self.assertEqual(
                pipeline.get_download_file(self.PROJECT, 'Objects.gpkg'), b'gpkg-bytes'
            )
            self.assertEqual(len(asked), pipeline.ENGINE_MISS_LIMIT + 2)


class PackageContentsTests(SimpleTestCase):
    """The package a user downloads carries the layers the run produced."""

    PROJECT = 'b' * 32

    def _entries(self, blob: bytes) -> list:
        return sorted(zipfile.ZipFile(io.BytesIO(blob)).namelist())

    def test_a_layer_the_cache_never_saw_reaches_the_design_package(self):
        fresh = _status(
            'Aerial_Spans.gpkg',
            'Aerial_Drops.gpkg',
            'Poles.gpkg',
            'Aerial_Spans.geojson',
            'Poles.geojson',
        )
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
            mock.patch.object(
                pipeline,
                'get_download_file',
                side_effect=lambda _p, n: b'gpkg-bytes'
                if n.endswith('.gpkg')
                else _feature_collection(1),
            ),
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
                side_effect=lambda _p, n: b'gpkg-bytes'
                if n.endswith('.gpkg')
                else _feature_collection(1),
            ),
        ):
            entries = self._entries(pipeline.generate_design_package(self.PROJECT))
        self.assertIn('Objects.gpkg', entries)


class PackageReportTests(SimpleTestCase):
    """A package says what it could not carry, instead of dropping it silently.

    Measured on the live service 2026-10-14: the engine's output files for these
    runs were gone, which left the survey ZIP with 16 GeoJSON layers and **not
    one of its 17 GeoPackages** — a valid zip, served with 200, that said
    nothing about the gap. Every package now carries PACKAGE_REPORT.txt, fills
    the caller's `report`, and logs the shortfall at WARNING; only "nothing at
    all could be bundled" is still an error.
    """

    PROJECT = 'c' * 32

    def _build(self, download, status):
        report: dict = {}
        with (
            mock.patch.object(pipeline, 'get_status', return_value=status),
            mock.patch.object(pipeline, '_read_status', return_value=status),
            mock.patch.object(pipeline, 'get_download_file', side_effect=download),
        ):
            blob = pipeline.generate_survey_package(self.PROJECT, report)
        return zipfile.ZipFile(io.BytesIO(blob)), report

    def test_a_missing_geopackage_is_named_in_the_archive_and_the_report(self):
        def download(_project_id, name):
            return None if name.endswith('.gpkg') else _feature_collection(1)

        zf, report = self._build(download, _status('Objects.gpkg', 'Objects.geojson'))

        text = zf.read(pipeline.PACKAGE_REPORT_NAME).decode('utf-8')
        self.assertIn('THIS PACKAGE IS INCOMPLETE', text)
        self.assertIn('Objects.gpkg', text)
        self.assertEqual([item['name'] for item in report['missing']], ['Objects.gpkg'])
        self.assertFalse(report['complete'])
        self.assertIn('objects.geojson', report['bundled'])

    def test_a_whole_package_reports_itself_complete(self):
        def download(_project_id, name):
            return b'gpkg-bytes' if name.endswith('.gpkg') else _feature_collection(1)

        zf, report = self._build(download, _status('Objects.gpkg', 'Objects.geojson'))

        text = zf.read(pipeline.PACKAGE_REPORT_NAME).decode('utf-8')
        self.assertIn('Every file in the package contract is present.', text)
        self.assertNotIn('THIS PACKAGE IS INCOMPLETE', text)
        self.assertEqual(report['missing'], [])
        self.assertEqual(report['substituted'], [])
        self.assertTrue(report['complete'])

    def test_a_geojson_replaced_by_its_geopackage_is_a_substitution(self):
        """The layer is present, under another name — not a silent absence.

        Only this layer's GeoPackage is on the engine, so its GeoJSON is
        replaced rather than reported as absent.
        """

        def download(_project_id, name):
            return b'gpkg-bytes' if name == 'Aerial_Spans.gpkg' else None

        zf, report = self._build(download, _status('Aerial_Spans.gpkg'))

        self.assertEqual(
            [item['name'] for item in report['substituted']], ['aerial_spans.geojson']
        )
        self.assertIn('Aerial_Spans.gpkg', report['substituted'][0]['reason'])
        self.assertIn('Aerial_Spans.gpkg', zf.namelist())
        self.assertNotIn('aerial_spans.geojson', zf.namelist())
        self.assertFalse(report['complete'])

    def test_nothing_fetchable_is_still_an_error(self):
        with self.assertRaises(FileNotFoundError):
            self._build(lambda _p, _n: None, _status())
