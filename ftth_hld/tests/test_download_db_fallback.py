"""A download must not vanish because the engine cannot serve its files.

Every run publishes its layers into the shared PostGIS database, but the
engine's own output **files** live on the engine's disk — a different machine
from the one serving the API. When a run was executed elsewhere (or a container
restarted without its volume) every ``/ftth/hld/download/...`` returns 404 while
the project's map stays perfectly intact, and both download packages came out
all but empty.

``get_download_file`` now falls back to the ``gis.*`` table backing the layer.
These tests pin the fallback's contract without needing a live PostGIS: the
cursor is stubbed, so what is asserted is the SQL target, the FeatureCollection
it builds and the CRS it declares.
"""

from __future__ import annotations

import json
from unittest import mock

from django.test import SimpleTestCase

from ftth_hld import pipeline
from ftth_hld.config import (
    DESIGN_GEOJSON_FILES,
    DESIGN_PACKAGE_FILES,
    LAYER_STEM_GIS_TABLES,
    SURVEY_GEOJSON_FILES,
    SURVEY_PACKAGE_FILES,
)


class _FakeCursor:
    """Records the SQL it is given and replays canned rows."""

    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((' '.join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_cursor(rows):
    """Patch the pipeline's cursor accessor and return the fake cursor.

    The accessor is patched rather than ``django.db.connection.cursor``: the
    latter is what ``SimpleTestCase`` hangs its own block on, and replacing it
    directly makes Django's test teardown raise.
    """
    cursor = _FakeCursor(rows)
    return mock.patch(
        'ftth_hld.pipeline._db_cursor',
        return_value=cursor,
    ), cursor


class DbLayerFallbackTests(SimpleTestCase):
    PROJECT = 'a' * 32

    def test_a_missing_engine_file_is_rebuilt_from_the_layer_table(self):
        rows = [
            ('{"type":"Point","coordinates":[13.36,52.44]}', {'PDP_ID': 'P-1'}, 4326),
            ('{"type":"Point","coordinates":[13.37,52.45]}', '{"PDP_ID": "P-2"}', 4326),
        ]
        patcher, cursor = _patch_cursor(rows)
        with patcher:
            payload = pipeline._db_layer_geojson(self.PROJECT, 'PDPs.geojson')

        self.assertIsNotNone(payload, 'the layer must be served from the database')
        assert payload is not None
        data = json.loads(payload)
        self.assertEqual(data['type'], 'FeatureCollection')
        self.assertEqual(len(data['features']), 2)
        # The gis geometry is stored in the project CRS and the export has no
        # `crs` member, so one must be declared or the WGS84 reprojection that
        # packages apply would use the wrong source.
        self.assertEqual(
            data['crs']['properties']['name'],
            'EPSG:4326',
        )
        # A string `properties` column is parsed, not passed through as a str.
        self.assertEqual(data['features'][1]['properties'], {'PDP_ID': 'P-2'})
        # And the query targets the table the stem maps to, for this project.
        sql, params = cursor.executed[0]
        self.assertIn('FROM gis."pdps"', sql)
        self.assertEqual(params, [self.PROJECT])

    def test_every_mapped_stem_targets_a_gis_table(self):
        """The map must only name tables the engine actually creates."""
        patcher, cursor = _patch_cursor([])
        for stem, table in LAYER_STEM_GIS_TABLES.items():
            with patcher:
                pipeline._db_layer_geojson(self.PROJECT, f'{stem}.geojson')
            if cursor.executed:
                sql = cursor.executed[-1][0]
                self.assertIn(f'FROM gis."{table}"', sql, stem)

    def test_a_merged_sub_layer_is_filtered_to_itself(self):
        """A feeder file must not arrive as every duct in the design.

        Feeder / distribution / drop ducts share `gis.duct_layer`; serving the
        whole table under a feeder-only name would overstate the deliverable
        (810 rows where the engine's own file holds 288).
        """
        patcher, cursor = _patch_cursor([])
        with patcher:
            pipeline._db_layer_geojson(self.PROJECT, 'Feeder_Ducts.geojson')
        sql, params = cursor.executed[0]
        self.assertIn('FROM gis."duct_layer"', sql)
        self.assertIn("properties->>'sublayer' = %s", sql)
        self.assertEqual(params, [self.PROJECT, 'Feeder_Ducts'])

    def test_a_whole_layer_is_not_filtered(self):
        patcher, cursor = _patch_cursor([])
        with patcher:
            pipeline._db_layer_geojson(self.PROJECT, 'Final_Trenches.geojson')
        sql, params = cursor.executed[0]
        self.assertNotIn('sublayer', sql)
        self.assertEqual(params, [self.PROJECT])

    def test_a_non_layer_file_is_never_served(self):
        """Only mapped layer stems may reach the SQL — nothing caller-supplied."""
        for name in (
            'Unknown_Layer.geojson',
            'Objects.gpkg',
            'inputs/Main_DataSet.xlsx',
            '../secret.geojson',
            'surface_ai_review.json',
        ):
            self.assertIsNone(
                pipeline._db_layer_geojson(self.PROJECT, name),
                f'{name} must not be served from the database',
            )

    def test_an_empty_table_yields_nothing_rather_than_an_empty_layer(self):
        patcher, _ = _patch_cursor([])
        with patcher:
            self.assertIsNone(pipeline._db_layer_geojson(self.PROJECT, 'Poles.geojson'))

    def test_a_geometry_that_cannot_be_parsed_is_skipped(self):
        rows = [
            ('not-json', {'A': 1}, 4326),
            ('{"type":"Point","coordinates":[13.36,52.44]}', {}, 4326),
        ]
        patcher, _ = _patch_cursor(rows)
        with patcher:
            payload = pipeline._db_layer_geojson(self.PROJECT, 'Chambers.geojson')
        assert payload is not None
        data = json.loads(payload)
        self.assertEqual(len(data['features']), 1)

    def test_a_failing_query_does_not_break_the_download(self):
        """A missing table must degrade to "no file", never raise."""
        with mock.patch('ftth_hld.pipeline._db_cursor', side_effect=RuntimeError('boom')):
            self.assertIsNone(pipeline._db_layer_geojson(self.PROJECT, 'Objects.geojson'))

    def test_the_brownfield_points_are_filtered_by_geometry(self):
        """`Existing_Infrastructure` (lines) and `..._Points` share one table.

        Serving the whole table under the points name would deliver the design's
        linework in a points layer (108 rows where the engine's own file holds
        11), and vice versa.
        """
        patcher, cursor = _patch_cursor([])
        with patcher:
            pipeline._db_layer_geojson(self.PROJECT, 'Existing_Infrastructure_Points.geojson')
        sql, params = cursor.executed[0]
        self.assertIn('FROM gis."brownfield"', sql)
        self.assertIn('GeometryType(geom) IN', sql)
        self.assertIn("'MULTIPOINT'", sql)
        self.assertNotIn('MULTILINESTRING', sql)
        self.assertEqual(params, [self.PROJECT])

    def test_the_brownfield_lines_keep_only_line_geometry(self):
        patcher, cursor = _patch_cursor([])
        with patcher:
            pipeline._db_layer_geojson(self.PROJECT, 'Existing_Infrastructure.geojson')
        sql, _ = cursor.executed[0]
        self.assertIn('GeometryType(geom) IN', sql)
        self.assertIn("'MULTILINESTRING'", sql)
        self.assertNotIn('MULTIPOINT', sql)

    def test_every_geojson_a_package_ships_has_a_database_backing(self):
        """The point of the fallback: a layer in a package is never unreachable.

        Both packages ship an explicit GeoJSON list, and every one of those
        stems must map to a `gis.*` table — otherwise a run whose engine-side
        files are gone produces a package that silently drops that layer. This
        is the check that would have caught `Existing_Infrastructure_Points`,
        `Aerial_Spans` and `Aerial_Cable` missing from the download path.
        """
        for package, files in (
            ('survey geojson', SURVEY_GEOJSON_FILES),
            ('design geojson', DESIGN_GEOJSON_FILES),
        ):
            for stem in files:
                self.assertIn(
                    stem.rsplit('.', 1)[0],
                    LAYER_STEM_GIS_TABLES,
                    f'{package}: {stem} has no gis table to fall back to',
                )

    def test_the_aerial_components_ship_in_both_packages(self):
        """An aerial leg is part of the network: spans and the cable on them.

        The aerial legs were already in the packages, but the overhead spans and
        the cable they carry — what the pole stage actually builds — were not,
        so a package described an aerial route without the span that carries it.
        """
        for files in (SURVEY_PACKAGE_FILES, DESIGN_PACKAGE_FILES):
            self.assertIn('Aerial_Spans.gpkg', files)
            self.assertIn('Aerial_Cable.gpkg', files)
            self.assertIn('Aerial_Drops.gpkg', files)
        for geojson_files in (SURVEY_GEOJSON_FILES, DESIGN_GEOJSON_FILES):
            self.assertIn('Aerial_Spans.geojson', geojson_files)
            self.assertIn('Aerial_Cable.geojson', geojson_files)
            self.assertIn('Aerial_Drops.geojson', geojson_files)

    def test_the_brownfield_regions_ship_in_both_packages(self):
        """Existing infrastructure — both its linework and its points."""
        for files in (SURVEY_PACKAGE_FILES, DESIGN_PACKAGE_FILES):
            self.assertIn('Existing_Infrastructure.gpkg', files)
            self.assertIn('Existing_Infrastructure_Points.gpkg', files)
        for geojson_files in (SURVEY_GEOJSON_FILES, DESIGN_GEOJSON_FILES):
            self.assertIn('Existing_Infrastructure.geojson', geojson_files)
            self.assertIn('Existing_Infrastructure_Points.geojson', geojson_files)
