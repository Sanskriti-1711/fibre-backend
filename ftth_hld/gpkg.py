"""Write a GeoPackage from a layer the shared database can still serve.

The engine publishes every run twice: as GeoPackage files on its own disk, and
as rows in the shared PostGIS database. The disk is not durable — a container
restart takes it, and the platform's free tier restarts often — while the rows
stay. Measured on the deployed stack 2026-10-19: `ftth-planning.onrender.com`
answers **404 for every `.gpkg` of all three live runs** (`MFG.gpkg`,
`Objects.gpkg`, `Final_Trenches.gpkg`), and both download packages therefore
come out with GeoJSON only.

`pipeline._db_layer_geojson` already rebuilds the GeoJSON from those rows, so
the geometry is not lost; what was missing is a container. This module writes
the GeoPackage itself, in the standard library, so a package keeps its
GeoPackages without GDAL in the backend image (which is `python:3.11-slim`).

It is a deliberately small writer: one feature table, WGS84, no envelope, no
rtree, no extensions. That is a valid GeoPackage (OGC 12-128r19) and QGIS,
`ogrinfo` and `fiona` all open it; the features, their attribute columns and
the layer name are what the package's reader uses.

Usage::

    gpkg_bytes = geojson_to_gpkg(geojson_bytes, 'MFG')
"""

from __future__ import annotations

import json
import re
import sqlite3
import struct
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


class GeoPackageError(ValueError):
    """A GeoJSON payload that cannot become a GeoPackage."""


class EmptyLayerError(GeoPackageError):
    """The layer carries no feature with a geometry to write."""


class NotAFeatureCollectionError(TypeError):
    """The payload is not a GeoJSON object."""


class BadCoordinateError(ValueError):
    """A coordinate is not a [lon, lat] pair."""


# OGC GeoPackage: `application_id` is the ASCII "GPKG" and `user_version`
# carries the spec version (1.3.0 here, which every current reader accepts).
_APPLICATION_ID = 0x47504B47
_USER_VERSION = 10300
_SRS_ID = 4326

# WKB geometry type codes (ISO 13249 / OGC 06-103r4).
_WKB_POINT = 1
_WKB_LINESTRING = 2
_WKB_POLYGON = 3
_WKB_MULTIPOINT = 4
_WKB_MULTILINESTRING = 5
_WKB_MULTIPOLYGON = 6
_WKB_GEOMETRYCOLLECTION = 7

_GEOMETRY_NAMES = {
    _WKB_POINT: 'POINT',
    _WKB_LINESTRING: 'LINESTRING',
    _WKB_POLYGON: 'POLYGON',
    _WKB_MULTIPOINT: 'MULTIPOINT',
    _WKB_MULTILINESTRING: 'MULTILINESTRING',
    _WKB_MULTIPOLYGON: 'MULTIPOLYGON',
    _WKB_GEOMETRYCOLLECTION: 'GEOMETRYCOLLECTION',
}

_WGS84_DEFINITION = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
)


# ---------------------------------------------------------------------------
# WKB
# ---------------------------------------------------------------------------


def _xy(point: Any) -> tuple[float, float]:
    """The first two ordinates. The published layers are 2D WGS84."""
    if not isinstance(point, list | tuple) or len(point) < 2:
        raise BadCoordinateError()
    return float(point[0]), float(point[1])


def _wkb_point(coords: Any) -> bytes:
    lon, lat = _xy(coords)
    return struct.pack('<BIdd', 1, _WKB_POINT, lon, lat)


def _wkb_linestring(coords: Any) -> bytes:
    points = [_xy(point) for point in coords]
    body = b''.join(struct.pack('<dd', lon, lat) for lon, lat in points)
    return struct.pack('<BII', 1, _WKB_LINESTRING, len(points)) + body


def _wkb_polygon(coords: Any) -> bytes:
    rings = [[_xy(point) for point in ring] for ring in coords]
    body = b''
    for ring in rings:
        body += struct.pack('<I', len(ring))
        body += b''.join(struct.pack('<dd', lon, lat) for lon, lat in ring)
    return struct.pack('<BII', 1, _WKB_POLYGON, len(rings)) + body


def _wkb_multi(geometry_type: int, parts: Any, writer) -> bytes:
    bodies = b''.join(writer(part) for part in parts)
    return struct.pack('<BII', 1, geometry_type, len(parts)) + bodies


def _wkb(geometry: dict) -> tuple[bytes, int] | None:
    """WKB bytes and the geometry type code, or None for an empty geometry."""
    if not isinstance(geometry, dict):
        return None
    kind = geometry.get('type')
    coords = geometry.get('coordinates')
    try:
        if kind == 'Point':
            if not coords:
                return None
            return _wkb_point(coords), _WKB_POINT
        if kind == 'MultiPoint':
            if not coords:
                return None
            return _wkb_multi(_WKB_MULTIPOINT, coords, _wkb_point), _WKB_MULTIPOINT
        if kind == 'LineString':
            if not coords:
                return None
            return _wkb_linestring(coords), _WKB_LINESTRING
        if kind == 'MultiLineString':
            if not coords:
                return None
            return (
                _wkb_multi(_WKB_MULTILINESTRING, coords, _wkb_linestring),
                _WKB_MULTILINESTRING,
            )
        if kind == 'Polygon':
            if not coords:
                return None
            return _wkb_polygon(coords), _WKB_POLYGON
        if kind == 'MultiPolygon':
            if not coords:
                return None
            return _wkb_multi(_WKB_MULTIPOLYGON, coords, _wkb_polygon), _WKB_MULTIPOLYGON
        if kind == 'GeometryCollection':
            parts = list(geometry.get('geometries') or [])
            if not parts:
                return None
            encoded = [_wkb(part) for part in parts]
            bodies = b''.join(item[0] for item in encoded if item)
            return (
                struct.pack('<BII', 1, _WKB_GEOMETRYCOLLECTION, len(encoded)) + bodies,
                _WKB_GEOMETRYCOLLECTION,
            )
    except (TypeError, ValueError):
        return None
    return None


def _gpkg_blob(geometry: dict) -> bytes | None:
    """The GeoPackage geometry BLOB: header + WKB, no envelope.

    Header (OGC 12-128r19 §2.1.3): magic ``GP``, version 0, flags, srs_id.
    The flags byte is little-endian WKB (bit 0) with envelope indicator 0
    (bits 1-3) and neither empty (bit 4) nor extended (bit 5) set.
    """
    encoded = _wkb(geometry)
    if encoded is None:
        return None
    wkb, _type_code = encoded
    return b'GP' + bytes([0, 0x01]) + struct.pack('<i', _SRS_ID) + wkb


# ---------------------------------------------------------------------------
# Attribute columns
# ---------------------------------------------------------------------------


def _column_names(keys: list[str]) -> dict[str, str]:
    """Map each GeoJSON property key to a safe, unique SQL column name."""
    used = {'fid', 'geom'}
    mapping: dict[str, str] = {}
    for raw in keys:
        name = re.sub(r'[^A-Za-z0-9_]', '_', str(raw)).strip('_') or 'field'
        if name[0].isdigit():
            name = f'f_{name}'
        candidate = name
        suffix = 2
        while candidate.lower() in used:
            candidate = f'{name}_{suffix}'
            suffix += 1
        used.add(candidate.lower())
        mapping[raw] = candidate
    return mapping


def _column_type(values: list[Any]) -> str:
    """SQLite affinity that holds every value of one property."""
    present = [value for value in values if value is not None]
    if not present:
        return 'TEXT'
    if all(isinstance(value, bool) for value in present):
        return 'INTEGER'
    if all(isinstance(value, int) and not isinstance(value, bool) for value in present):
        return 'INTEGER'
    if all(isinstance(value, int | float) and not isinstance(value, bool) for value in present):
        return 'REAL'
    return 'TEXT'


def _stored_value(value: Any) -> Any:
    """Booleans as 0/1, containers as JSON text — SQLite has only these."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str | int | float) or value is None:
        return value
    return str(value)


# ---------------------------------------------------------------------------
# GeoPackage
# ---------------------------------------------------------------------------

_SCHEMA = (
    """
    CREATE TABLE gpkg_spatial_ref_sys (
        srs_name TEXT NOT NULL,
        srs_id INTEGER NOT NULL PRIMARY KEY,
        organization TEXT NOT NULL,
        organization_coordsys_id INTEGER NOT NULL,
        definition TEXT NOT NULL,
        description TEXT
    )
    """,
    """
    CREATE TABLE gpkg_contents (
        table_name TEXT NOT NULL PRIMARY KEY,
        data_type TEXT NOT NULL,
        identifier TEXT UNIQUE,
        description TEXT DEFAULT '',
        last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        min_x DOUBLE,
        min_y DOUBLE,
        max_x DOUBLE,
        max_y DOUBLE,
        srs_id INTEGER,
        FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
    )
    """,
    """
    CREATE TABLE gpkg_geometry_columns (
        table_name TEXT NOT NULL,
        column_name TEXT NOT NULL,
        geometry_type_name TEXT NOT NULL,
        srs_id INTEGER NOT NULL,
        z TINYINT NOT NULL,
        m TINYINT NOT NULL,
        CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
        CONSTRAINT uk_gc_table_name UNIQUE (table_name)
    )
    """,
)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _bounds(features: list[dict]) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []

    def walk(value: Any) -> None:
        if isinstance(value, list | tuple):
            if len(value) >= 2 and all(isinstance(item, int | float) for item in value[:2]):
                xs.append(float(value[0]))
                ys.append(float(value[1]))
                return
            for item in value:
                walk(item)

    for feature in features:
        walk((feature.get('geometry') or {}).get('coordinates'))
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def geojson_to_gpkg(geojson: bytes | str | dict, layer_name: str) -> bytes:
    """Write one GeoJSON FeatureCollection as a one-table GeoPackage.

    ``layer_name`` is the table name (and the identifier) the reader sees, e.g.
    ``MFG``. Features without a usable geometry are skipped, exactly as they
    are by the GeoJSON fallback that feeds this.
    """
    if isinstance(geojson, bytes | bytearray):
        data = json.loads(geojson.decode('utf-8'))
    elif isinstance(geojson, str):
        data = json.loads(geojson)
    else:
        data = geojson
    if not isinstance(data, dict):
        raise NotAFeatureCollectionError()

    features = [
        feature
        for feature in data.get('features') or []
        if isinstance(feature, dict) and isinstance(feature.get('geometry'), dict)
    ]
    rows: list[dict[str, Any]] = []
    for feature in features:
        blob = _gpkg_blob(feature['geometry'])
        if blob is None:
            continue
        props = feature.get('properties')
        rows.append(
            {
                'geometry': feature['geometry'],
                'blob': blob,
                'properties': props if isinstance(props, dict) else {},
            }
        )
    if not rows:
        raise EmptyLayerError()

    keys: list[str] = []
    for row in rows:
        for key in row['properties']:
            if key not in keys:
                keys.append(key)
    columns = _column_names(keys)
    types = {key: _column_type([row['properties'].get(key) for row in rows]) for key in keys}

    geometry_codes = {encoded[1] for row in rows if (encoded := _wkb(row['geometry'])) is not None}
    geometry_name = (
        _GEOMETRY_NAMES[next(iter(geometry_codes))] if len(geometry_codes) == 1 else 'GEOMETRY'
    )

    table = _quote(layer_name)
    column_sql = ', '.join(f'{_quote(columns[key])} {types[key]}' for key in keys)
    create_table = (
        f'CREATE TABLE {table} (fid INTEGER PRIMARY KEY AUTOINCREMENT, '
        f'geom BLOB{", " + column_sql if column_sql else ""})'
    )

    now = datetime.now(UTC)
    last_change = now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'
    bbox = _bounds(rows)

    with NamedTemporaryFile(suffix='.gpkg', delete=False) as handle:
        path = Path(handle.name)

    try:
        connection = sqlite3.connect(path)
        try:
            connection.execute(f'PRAGMA application_id = {_APPLICATION_ID}')
            connection.execute(f'PRAGMA user_version = {_USER_VERSION}')
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.executemany(
                'INSERT INTO gpkg_spatial_ref_sys '
                '(srs_name, srs_id, organization, organization_coordsys_id, definition, description) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                [
                    ('Undefined cartesian', -1, 'NONE', -1, 'undefined', 'undefined cartesian'),
                    ('Undefined geographic', 0, 'NONE', 0, 'undefined', 'undefined geographic'),
                    ('WGS 84', _SRS_ID, 'EPSG', _SRS_ID, _WGS84_DEFINITION, 'WGS 84'),
                ],
            )
            connection.execute(
                'INSERT INTO gpkg_contents '
                '(table_name, data_type, identifier, description, last_change, '
                'min_x, min_y, max_x, max_y, srs_id) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    layer_name,
                    'features',
                    layer_name,
                    '',
                    last_change,
                    *(bbox if bbox else (None, None, None, None)),
                    _SRS_ID,
                ),
            )
            connection.execute(
                'INSERT INTO gpkg_geometry_columns '
                '(table_name, column_name, geometry_type_name, srs_id, z, m) '
                'VALUES (?, ?, ?, ?, 0, 0)',
                (layer_name, 'geom', geometry_name, _SRS_ID),
            )
            connection.execute(create_table)
            placeholders = ', '.join('?' for _ in range(1 + len(keys)))
            insert = (
                f'INSERT INTO {table} (geom'
                + ''.join(f', {_quote(columns[key])}' for key in keys)
                + f') VALUES ({placeholders})'
            )
            connection.executemany(
                insert,
                [
                    (
                        row['blob'],
                        *[_stored_value(row['properties'].get(key)) for key in keys],
                    )
                    for row in rows
                ],
            )
            connection.commit()
        finally:
            connection.close()
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)
