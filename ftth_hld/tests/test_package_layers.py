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
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase

from ftth_hld import pipeline
from ftth_hld.config import (
    DESIGN_GEOJSON_FILES,
    DESIGN_PACKAGE_FILES,
    DISPLAYED_LAYER_FILES,
    SURVEY_GEOJSON_FILES,
    SURVEY_PACKAGE_FILES,
)
from testutils.factories import make_feature, make_ftth_project, make_survey_copy


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


class PackageReportTests(TestCase):
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

        self.assertEqual([item['name'] for item in report['substituted']], ['aerial_spans.geojson'])
        self.assertIn('Aerial_Spans.gpkg', report['substituted'][0]['reason'])
        self.assertIn('Aerial_Spans.gpkg', zf.namelist())
        self.assertNotIn('aerial_spans.geojson', zf.namelist())
        self.assertFalse(report['complete'])

    def test_nothing_fetchable_is_still_an_error(self):
        with self.assertRaises(FileNotFoundError):
            self._build(lambda _p, _n: None, _status())


class ConsecutiveMissCarryoverTests(SimpleTestCase):
    """A few absent layers must not strip the NEXT package's GeoPackages.

    The write-off counter is CONSECUTIVE 404s and the GeoJSON pass runs after
    the GeoPackages, so a build whose only gaps were its last few GeoJSONs
    ended with the engine written off for ENGINE_MISS_COOLDOWN. Measured on the
    live engine 2026-10-18: a run missing 3 GeoJSONs produced a 50-file design
    package, and the request right after it produced 19 files with no
    GeoPackage at all, from the same engine that still had every .gpkg.
    """

    PROJECT = 'f' * 32

    def setUp(self):
        pipeline._ENGINE_FILE_MISSES.clear()
        self.addCleanup(pipeline._ENGINE_FILE_MISSES.clear)

    def _build(self, asked):
        def fake_get(url, *args, **kwargs):
            asked.append(url)
            if url.endswith('/Objects.gpkg'):
                return SimpleNamespace(status_code=200, content=b'gpkg-bytes')
            return SimpleNamespace(status_code=404, content=b'')

        status = _status('Objects.gpkg')
        with (
            mock.patch.object(pipeline.requests, 'get', side_effect=fake_get),
            mock.patch.object(pipeline, '_db_layer_geojson', return_value=None),
            mock.patch.object(pipeline, 'get_status', return_value=status),
            mock.patch.object(pipeline, '_read_status', return_value=status),
        ):
            return pipeline._build_package_zip(
                self.PROJECT,
                ['Objects.gpkg'],
                {
                    'Gone_A.geojson': 'gone_a.geojson',
                    'Gone_B.geojson': 'gone_b.geojson',
                    'Gone_C.geojson': 'gone_c.geojson',
                },
                'design',
            )

    def test_the_next_build_asks_the_engine_for_its_geopackage_again(self):
        asked: list[str] = []

        self._build(asked)
        first_build = len(asked)
        # The first build ends on its absent layers, which is what wrote the
        # engine off for the old carry-over to poison the next request.
        self.assertEqual(
            pipeline._ENGINE_FILE_MISSES.get(self.PROJECT, (0, 0))[0], pipeline.ENGINE_MISS_LIMIT
        )

        self._build(asked)

        second_build = asked[first_build:]
        self.assertTrue(
            [u for u in second_build if u.endswith('/Objects.gpkg')],
            'the second build must fetch the GeoPackage from the engine, not write it off',
        )

    def test_the_second_build_still_carries_the_geopackage(self):
        self._build([])
        blob = self._build([])

        self.assertIn('Objects.gpkg', zipfile.ZipFile(io.BytesIO(blob)).namelist())


class DisplayedLayerContractTests(SimpleTestCase):
    """Every layer a package can carry is one the results map displays."""

    def test_every_curated_package_file_maps_to_a_displayed_layer(self):
        allowed = {stem.lower() for stems in DISPLAYED_LAYER_FILES.values() for stem in stems}
        packaged = (
            list(DESIGN_PACKAGE_FILES)
            + list(SURVEY_PACKAGE_FILES)
            + list(DESIGN_GEOJSON_FILES)
            + list(SURVEY_GEOJSON_FILES)
        )
        for name in packaged:
            if name.lower().endswith(('.gpkg', '.geojson')):
                self.assertIn(Path(name).stem.lower(), allowed, f'{name} backs no displayed layer')


class DisplayedLayerFilterTests(SimpleTestCase):
    """Packages carry the files behind the displayed layers, nothing more.

    Measured 2026-10-18 on run 6d0bb6f…: the results map displayed 14 layers
    while the design package carried 26. Pseudo_HH, Served_Premises,
    Tangent_Crossings, Trench_Nodes and the per-tier trench publications were
    layers the platform itself never shows.
    """

    PROJECT = 'e' * 32

    LAYERS = [
        {'name': 'objects', 'count': 1},
        {'name': 'mfg', 'count': 1},
        {'name': 'mfg_service_areas', 'count': 1},
        {'name': 'trenches', 'count': 1},
        {'name': 'cables', 'count': 2},
        {'name': 'ducts', 'count': 3},
        {'name': 'aerial_spans', 'count': 1},
    ]

    DOWNLOADS = [
        # Displayed layers (and their files).
        'Objects.gpkg',
        'Objects.geojson',
        'MFG.gpkg',
        'MFG.geojson',
        'MFG_Service_Areas.gpkg',
        'MFG_Service_Areas.geojson',
        'Final_Trenches.gpkg',
        'Final_Trenches.geojson',
        'Feeder_Cable.gpkg',
        'Distribution_Cable.gpkg',
        'Feeder_Ducts.gpkg',
        'Distribution_Ducts.gpkg',
        'Drop_Ducts.gpkg',
        'Aerial_Spans.gpkg',
        'Aerial_Spans.geojson',
        # Intermediates the platform never publishes as layers.
        'Pseudo_HH.gpkg',
        'Pseudo_HH.geojson',
        'Served_Premises.gpkg',
        'Tangent_Crossings.geojson',
        'Trench_Nodes.gpkg',
        'Distribution_Trench.gpkg',
        'Distribution_Trench.geojson',
        'Garden_Trench.gpkg',
    ]

    EXTRAS = (
        'Pseudo_HH.gpkg',
        'Pseudo_HH.geojson',
        'Served_Premises.gpkg',
        'Tangent_Crossings.geojson',
        'Trench_Nodes.gpkg',
        'Distribution_Trench.gpkg',
        'distribution_trench.geojson',
        'Garden_Trench.gpkg',
    )

    def _status(self, layers=None):
        return {
            'layers': self.LAYERS if layers is None else layers,
            'downloads': [{'name': name} for name in self.DOWNLOADS],
        }

    def _entries(self, kind='design', layers=None):
        def download(_project_id, name):
            return b'gpkg-bytes' if name.endswith('.gpkg') else _feature_collection(1)

        status = self._status(layers)
        with (
            mock.patch.object(pipeline, 'get_status', return_value=status),
            mock.patch.object(pipeline, '_read_status', return_value=status),
            mock.patch.object(pipeline, 'get_download_file', side_effect=download),
            mock.patch('ftth_hld.boq.render_boq_xlsx', return_value=b'xlsx-bytes'),
        ):
            build = (
                pipeline.generate_survey_package
                if kind == 'survey'
                else pipeline.generate_design_package
            )
            blob = build(self.PROJECT)
        return zipfile.ZipFile(io.BytesIO(blob)).namelist()

    def test_the_design_package_drops_undisplayed_layers(self):
        entries = self._entries('design')

        for extra in self.EXTRAS:
            self.assertNotIn(extra, entries, f'{extra} is not a displayed layer')
        for wanted in (
            'Objects.gpkg',
            'Final_Trenches.gpkg',
            'Feeder_Cable.gpkg',
            'Distribution_Ducts.gpkg',
            'aerial_spans.geojson',
        ):
            self.assertIn(wanted, entries)

    def test_the_survey_package_drops_undisplayed_layers(self):
        entries = self._entries('survey')

        for extra in self.EXTRAS:
            self.assertNotIn(extra, entries, f'{extra} is not a displayed layer')
        self.assertIn('Objects.gpkg', entries)
        self.assertIn('objects.geojson', entries)

    def test_the_survey_package_carries_the_mfg_and_its_service_area(self):
        """The field package must include MFG — the cabinet and what it serves.

        Both GeoJSON files are in ``DESIGN_GEOJSON_FILES``, and the survey
        package builder skips any GeoJSON either map names (it expects the
        map to add it back reprojected). While ``SURVEY_GEOJSON_FILES`` did
        not name them, they were skipped and never added, so the rebuilt
        survey package for `73774a84…` had 15 entries and no MFG
        (measured 2026-10-19).
        """
        for kind in ('survey', 'design'):
            entries = self._entries(kind)
            self.assertIn('MFG.gpkg', entries, kind)
            self.assertIn('mfg.geojson', entries, kind)
            self.assertIn('mfg_service_areas.geojson', entries, kind)

    def test_a_run_with_no_published_layers_keeps_its_package_whole(self):
        """No layer list means no knowledge -- except for the excluded layers.

        The display filter cannot run without the run's own list, so the
        package keeps every candidate. The excluded artefacts do not survive
        that: they are never deliverables, whether the list is known or not
        (see ``FieldSurveyLayerRuleTests``).
        """
        entries = self._entries('design', layers=[])

        self.assertIn('Objects.gpkg', entries)
        self.assertNotIn('Pseudo_HH.gpkg', entries)
        self.assertNotIn('Trench_Nodes.gpkg', entries)

    def test_a_published_layer_with_no_mapping_keeps_the_package_whole(self):
        entries = self._entries('design', layers=[{'name': 'brand_new_layer'}])

        self.assertNotIn('Pseudo_HH.gpkg', entries)
        self.assertIn('Objects.gpkg', entries)


class FieldSurveyLayerRuleTests(SimpleTestCase):
    """The field side gets `objects`, never `Pseudo_HH`.

    `Pseudo_HH` is the footway end of a garden leg -- a design artefact of the
    trench network with no address and no household -- while `Objects` is one
    point per premise with its address and household count. A Survey copy built
    before the rule carries both: project `24cd3573…` holds 23 layers including
    `Pseudo_HH` (224) beside `objects` (285).

    Two gaps are pinned here. A run that PUBLISHES ``pseudo_hh`` used to arrive
    at ``_displayed_layer_stems`` as an unknown name, which returned None and
    left the package whole -- so the artefact shipped. And a run whose published
    list omitted ``objects`` dropped the premises layer the package is keyed on.
    """

    PROJECT = 'f' * 32

    DOWNLOADS = [
        'Objects.gpkg',
        'Objects.geojson',
        'Pseudo_HH.gpkg',
        'Pseudo_HH.geojson',
        'Final_Trenches.gpkg',
    ]

    def _entries(self, kind, layers):
        def download(_project_id, name):
            return b'gpkg-bytes' if name.endswith('.gpkg') else _feature_collection(1)

        status = {'layers': layers, 'downloads': [{'name': n} for n in self.DOWNLOADS]}
        with (
            mock.patch.object(pipeline, 'get_status', return_value=status),
            mock.patch.object(pipeline, '_read_status', return_value=status),
            mock.patch.object(pipeline, 'get_download_file', side_effect=download),
            mock.patch('ftth_hld.boq.render_boq_xlsx', return_value=b'xlsx-bytes'),
        ):
            build = (
                pipeline.generate_survey_package
                if kind == 'survey'
                else pipeline.generate_design_package
            )
            blob = build(self.PROJECT)
        return zipfile.ZipFile(io.BytesIO(blob)).namelist()

    def test_a_run_that_publishes_pseudo_hh_does_not_ship_it(self):
        layers = [
            {'name': 'objects', 'count': 285},
            {'name': 'pseudo_hh', 'count': 224},
            {'name': 'trenches', 'count': 1},
        ]

        for kind in ('survey', 'design'):
            entries = self._entries(kind, layers)
            self.assertNotIn('Pseudo_HH.gpkg', entries, kind)
            self.assertNotIn('Pseudo_HH.geojson', entries, kind)
            self.assertIn('Objects.gpkg', entries, kind)

    def test_a_run_that_publishes_a_per_tier_trench_does_not_ship_it(self):
        """One trench layer in the survey package: ``Final_Trenches``.

        The designer also writes ``Feeder_Trench`` / ``Distribution_Trench`` /
        ``Garden_Trench``: the same paths, segmented by tier, and the cabling
        stages' input rather than the published design layer. A run that names
        them must not get them bundled, and ``Final_Trenches`` must still be
        there -- dropping the mirrors is not dropping the trench.
        """
        layers = [
            {'name': 'objects', 'count': 285},
            {'name': 'trenches', 'count': 387},
            {'name': 'Distribution_Trench', 'count': 265},
            {'name': 'Garden_Trench', 'count': 224},
            {'name': 'Feeder_Trench', 'count': 58},
        ]

        for kind in ('survey', 'design'):
            entries = self._entries(kind, layers)
            for mirror in ('Distribution_Trench', 'Garden_Trench', 'Feeder_Trench'):
                self.assertNotIn(f'{mirror}.gpkg', entries, kind)
                self.assertNotIn(f'{mirror}.geojson', entries, kind)
                self.assertNotIn(f'{mirror.lower()}.geojson', entries, kind)
            self.assertIn('Final_Trenches.gpkg', entries, kind)
            self.assertIn('final_trenches.geojson', entries, kind)

    def test_a_run_that_publishes_trench_nodes_does_not_ship_them(self):
        """Trench joints are not a layer the crew records against.

        Every node is realised by a chamber, which is the published layer; the
        package must not arrive with a `Trench_Nodes` layer beside it.
        """
        layers = [
            {'name': 'objects', 'count': 285},
            {'name': 'trenches', 'count': 387},
            {'name': 'trench_nodes', 'count': 123},
            {'name': 'chambers', 'count': 73},
        ]

        for kind in ('survey', 'design'):
            entries = self._entries(kind, layers)
            self.assertNotIn('Trench_Nodes.gpkg', entries, kind)
            self.assertNotIn('Trench_Nodes.geojson', entries, kind)
            self.assertNotIn('trench_nodes.geojson', entries, kind)
            self.assertIn('Objects.gpkg', entries, kind)
            self.assertIn('Final_Trenches.gpkg', entries, kind)

    def test_the_object_layer_is_packaged_even_when_the_run_does_not_list_it(self):
        entries = self._entries('survey', [{'name': 'trenches', 'count': 1}])

        self.assertIn('Objects.gpkg', entries)
        self.assertIn('objects.geojson', entries)

    def test_the_rule_names_the_excluded_and_always_packaged_layers(self):
        """The app and the backend must name the same layer.

        The app's list lives in TS (`lib/utils/layer-ids.ts`) and cannot be
        imported here, so this pins what the backend side exports -- the value
        the assignment import and the package filter both read.
        """
        from projects.layer_rules import (
            FIELD_SURVEY_ALWAYS_PACKAGED_LAYERS,
            FIELD_SURVEY_EXCLUDED_LAYERS,
        )

        self.assertIn('pseudo_hh', FIELD_SURVEY_EXCLUDED_LAYERS)
        for mirror in ('feeder_trench', 'distribution_trench', 'garden_trench', 'trench_nodes'):
            self.assertIn(mirror, FIELD_SURVEY_EXCLUDED_LAYERS)
        # The one trench layer the field survey does use is not excluded.
        self.assertNotIn('final_trenches', FIELD_SURVEY_EXCLUDED_LAYERS)
        self.assertIn('objects', FIELD_SURVEY_ALWAYS_PACKAGED_LAYERS)


class PackageFromCopyTests(TestCase):
    """A run whose files are gone everywhere is rebuilt from its Survey copy.

    Run `24cd3573…` (measured 2026-10-19): the engine answers 404 for every
    file, every `gis.*` table has 0 rows for it, and its Survey copy is active
    with 2,800 features. The copy was imported from this run's package, so it
    is a faithful source — and the alternative is a 404 on the crew's own
    project.
    """

    def test_the_package_is_rebuilt_from_the_copy_with_geojson_and_gpkg(self):
        from projects.models import ImportSession

        run_id = 'f' * 32
        copy = make_survey_copy(make_ftth_project())
        make_feature(copy, layer_id='objects', layer_name='objects')
        make_feature(copy, layer_id='final_trenches', layer_name='final_trenches')
        # A row the field survey never serves must not come back through this path.
        make_feature(copy, layer_id='Trench_Nodes', layer_name='Trench_Nodes')
        ImportSession.objects.create(
            project=copy,
            original_filename=f'{run_id}_survey_package.zip',
            stored_file_path='imports/x.zip',
        )

        report: dict = {}
        with (
            mock.patch.object(pipeline, 'get_status', return_value={}),
            mock.patch.object(
                pipeline, '_build_package_zip', side_effect=FileNotFoundError('no files')
            ),
        ):
            blob = pipeline.generate_survey_package(run_id, report)

        names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
        self.assertIn('objects.geojson', names)
        self.assertIn('final_trenches.geojson', names)
        self.assertIn('Objects.gpkg', names)
        self.assertIn('Final_Trenches.gpkg', names)
        self.assertNotIn('trench_nodes.geojson', names)
        self.assertEqual(report['source'], f'survey copy {copy.id}')
        self.assertTrue(report['complete'])

    def test_no_copy_keeps_the_original_not_found_error(self):
        with (
            mock.patch.object(pipeline, 'get_status', return_value={}),
            mock.patch.object(
                pipeline, '_build_package_zip', side_effect=FileNotFoundError('no files')
            ),
            self.assertRaises(FileNotFoundError),
        ):
            pipeline.generate_survey_package('a' * 32, {})
