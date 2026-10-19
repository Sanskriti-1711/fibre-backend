"""The GeoPackage rebuilt from the database is a GeoPackage a reader can open.

The deployed engine's disk is not durable: measured 2026-10-19, every `.gpkg`
of every live run answered 404 while the layers themselves were still in the
shared PostGIS. `ftth_hld.gpkg` writes the container from those rows, so a
package keeps its GeoPackages. These tests pin the header, the mandatory
tables, the geometry encoding and the attribute columns — the parts a reader
(QGIS, `ogrinfo`, fiona) checks before it will open the file at all.
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from ftth_hld import pipeline
from ftth_hld.gpkg import geojson_to_gpkg

POINT = {
    'type': 'FeatureCollection',
    'features': [
        {
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [13.373, 52.4485]},
            'properties': {'MFG_ID': 'MFG-1', 'CAPACITY': 288, 'NOTE': None},
        }
    ],
}


class GeoPackageWriterTests(SimpleTestCase):
    def _write(self, payload, layer_name='MFG') -> Path:
        data = geojson_to_gpkg(json.dumps(payload).encode(), layer_name)
        descriptor, name = tempfile.mkstemp(suffix='.gpkg')
        os.close(descriptor)  # Windows refuses to delete a file with an open handle
        path = Path(name)
        path.write_bytes(data)
        self.addCleanup(path.unlink)
        return path

    def test_the_file_is_an_sqlite_database_marked_as_a_geopackage(self):
        path = self._write(POINT)
        self.assertEqual(path.read_bytes()[:15], b'SQLite format 3')

        connection = sqlite3.connect(path)
        try:
            application_id = connection.execute('PRAGMA application_id').fetchone()[0]
            user_version = connection.execute('PRAGMA user_version').fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(application_id, 0x47504B47)  # "GPKG"
        self.assertGreaterEqual(user_version, 10200)

    def test_the_mandatory_tables_describe_the_layer(self):
        path = self._write(POINT)
        connection = sqlite3.connect(path)
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            contents = connection.execute(
                'SELECT table_name, data_type, srs_id FROM gpkg_contents'
            ).fetchone()
            geometry = connection.execute(
                'SELECT table_name, column_name, geometry_type_name, srs_id '
                'FROM gpkg_geometry_columns'
            ).fetchone()
        finally:
            connection.close()

        for table in ('gpkg_spatial_ref_sys', 'gpkg_contents', 'gpkg_geometry_columns', 'MFG'):
            self.assertIn(table, tables)
        self.assertEqual(contents, ('MFG', 'features', 4326))
        self.assertEqual(geometry, ('MFG', 'geom', 'POINT', 4326))

    def test_the_geometry_is_a_geopackage_blob_wrapping_little_endian_wkb(self):
        path = self._write(POINT)
        connection = sqlite3.connect(path)
        try:
            blob = connection.execute('SELECT geom FROM MFG').fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(blob[:2], b'GP')
        self.assertEqual(blob[2], 0)  # version
        self.assertEqual(blob[3], 0x01)  # little endian WKB, no envelope
        self.assertEqual(struct.unpack('<i', blob[4:8])[0], 4326)

        # WKB: byte order 1, type 1 (Point), then the two doubles.
        byte_order, geometry_type = struct.unpack('<BI', blob[8:13])
        self.assertEqual(byte_order, 1)
        self.assertEqual(geometry_type, 1)
        self.assertAlmostEqual(struct.unpack('<d', blob[13:21])[0], 13.373)
        self.assertAlmostEqual(struct.unpack('<d', blob[21:29])[0], 52.4485)

    def test_attribute_columns_keep_their_types_and_values(self):
        path = self._write(POINT)
        connection = sqlite3.connect(path)
        try:
            columns = {row[1]: row[2] for row in connection.execute('PRAGMA table_info(MFG)')}
            row = connection.execute('SELECT MFG_ID, CAPACITY, NOTE FROM MFG').fetchone()
        finally:
            connection.close()

        self.assertEqual(columns['MFG_ID'], 'TEXT')
        self.assertEqual(columns['CAPACITY'], 'INTEGER')
        self.assertEqual(row, ('MFG-1', 288, None))

    def test_lines_polygons_and_multipart_geometry_are_encoded(self):
        payload = {
            'type': 'FeatureCollection',
            'features': [
                {
                    'type': 'Feature',
                    'geometry': {
                        'type': 'LineString',
                        'coordinates': [[13.37, 52.44], [13.38, 52.45]],
                    },
                    'properties': {'NAME': 'trench'},
                },
                {
                    'type': 'Feature',
                    'geometry': {
                        'type': 'MultiPolygon',
                        'coordinates': [
                            [[[13.37, 52.44], [13.38, 52.44], [13.38, 52.45], [13.37, 52.44]]]
                        ],
                    },
                    'properties': {'NAME': 'area'},
                },
            ],
        }
        path = self._write(payload, 'Mixed')
        connection = sqlite3.connect(path)
        try:
            geometry_type = connection.execute(
                'SELECT geometry_type_name FROM gpkg_geometry_columns'
            ).fetchone()[0]
            types = [
                struct.unpack('<I', row[0][9:13])[0]
                for row in connection.execute('SELECT geom FROM Mixed ORDER BY fid')
            ]
        finally:
            connection.close()

        self.assertEqual(geometry_type, 'GEOMETRY')
        self.assertEqual(types, [2, 6])  # LineString, MultiPolygon

    def test_a_layer_with_no_usable_geometry_is_refused_loudly(self):
        with self.assertRaises(ValueError):
            geojson_to_gpkg(json.dumps({'type': 'FeatureCollection', 'features': []}), 'Empty')


class GeoPackageFallbackTests(SimpleTestCase):
    """`get_download_file` builds the GeoPackage only when nothing else has it."""

    def test_a_missing_engine_gpkg_is_rebuilt_from_the_database_rows(self):
        payload = json.dumps(POINT).encode()

        with (
            mock.patch.object(pipeline, '_engine_files_missing', return_value=True),
            mock.patch.object(pipeline, '_db_layer_geojson', return_value=payload) as db,
        ):
            data = pipeline.get_download_file('e' * 32, 'MFG.gpkg')

        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data[:15], b'SQLite format 3')
        db.assert_called_once_with('e' * 32, 'MFG.geojson')

    def test_a_layer_the_database_cannot_serve_stays_missing(self):
        with (
            mock.patch.object(pipeline, '_engine_files_missing', return_value=True),
            mock.patch.object(pipeline, '_db_layer_geojson', return_value=None),
        ):
            self.assertIsNone(pipeline.get_download_file('e' * 32, 'MFG.gpkg'))
