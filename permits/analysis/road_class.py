"""Attribute OSM road classification (``fclass``) onto HLD trench segments.

The HLD engine routes trenches along the roads input, but the persisted
``gis.trench_layer`` features don't carry the road class. The permit engine's
``ROAD_AUTHORITY_001`` rule reads ``properties->>'fclass'`` on trench
segments to resolve the responsible road authority (Straßenbaulastträger).

This module reads a project's roads input file (GeoJSON / shapefile zip /
GPKG — any GDAL-readable vector) and, for each trench segment, persists the
road class of the nearest road within a tolerance into the trench's
``properties`` jsonb. Idempotent: re-running only fills missing classes.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from django.conf import settings
from django.db import connection

try:
    from osgeo import ogr
except ImportError:  # pragma: no cover - GDAL optional
    ogr = None

# Search tolerance for matching a trench to a road (metres). Trenches run
# along roads, so 40 m comfortably captures the adjacent carriageway without
# grabbing unrelated parallel roads.
SNAP_METERS = 40.0

TMP_TABLE = '_roads_tmp'


def project_roads_file(project_id: str) -> Path | None:
    """Locate the roads input file for an HLD project on disk."""
    input_dir = Path(settings.MEDIA_ROOT) / 'ftth_outputs' / project_id / 'inputs'
    if not input_dir.exists():
        return None
    # Preference: explicit roads_filename if it exists, else any roads file.
    candidates = sorted(input_dir.glob('*'))
    for path in candidates:
        if path.name.lower().startswith('roads'):
            return path
    for path in candidates:
        if path.suffix.lower() in {'.geojson', '.json', '.gpkg', '.zip', '.shp'}:
            return path
    return None


def _roads_geojson(path: Path) -> list[dict]:
    """Read any GDAL-readable roads vector into a list of feature dicts."""
    if ogr is None:
        raise RuntimeError('GDAL (osgeo) is required to read the roads file.')
    candidates = [str(path)]
    # Zip shapefile bundles open via /vsizip/.
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as zf:
            shp = next((n for n in zf.namelist() if n.lower().endswith('.shp')), None)
        if shp:
            candidates.insert(0, f'/vsizip/{path.as_posix()}/{shp}')
    features: list[dict] = []
    for candidate in candidates:
        ds = ogr.Open(candidate)
        if ds is None:
            continue
        try:
            for layer_idx in range(ds.GetLayerCount()):
                layer = ds.GetLayerByIndex(layer_idx)
                for feat in layer:
                    geom = feat.GetGeometryRef()
                    if geom is None:
                        continue
                    props = {}
                    for fld in range(feat.GetFieldCount()):
                        props[feat.GetFieldDefnRef(fld).GetName()] = feat.GetField(fld)
                    features.append(
                        {
                            'geometry': json.loads(geom.ExportToJson()),
                            'properties': props,
                        }
                    )
        finally:
            ds = None
        if features:
            break
    return features


def _load_tmp_roads(project_id: str, features: list[dict]) -> int:
    """Load roads features into a temp table for the spatial join."""
    with connection.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS gis.{TMP_TABLE}')
        cur.execute(
            f'CREATE TABLE gis.{TMP_TABLE} ('
            '  id BIGSERIAL PRIMARY KEY,'
            '  fclass TEXT, highway TEXT, name TEXT,'
            '  geom GEOMETRY(Geometry, 4326))'
        )
        rows = []
        for feat in features:
            props = feat.get('properties') or {}
            fclass = props.get('fclass') or props.get('highway') or props.get('class')
            highway = props.get('highway')
            name = props.get('name')
            geom = feat.get('geometry')
            if not geom:
                continue
            rows.append((fclass, highway, name, json.dumps(geom)))
        if rows:
            # executemany over a remote connection is 50k network round trips;
            # batched execute_values is ~100x faster for bulk geometry loads.
            try:
                from psycopg2.extras import execute_values

                execute_values(
                    cur,
                    f'INSERT INTO gis.{TMP_TABLE} (fclass, highway, name, geom) ' 'VALUES %s',
                    rows,
                    template=('(%s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))'),
                    page_size=500,
                )
            except ImportError:  # pragma: no cover - psycopg2 always present
                cur.executemany(
                    f'INSERT INTO gis.{TMP_TABLE} (fclass, highway, name, geom) '
                    'VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))',
                    rows,
                )
        cur.execute(
            f'CREATE INDEX IF NOT EXISTS idx_{TMP_TABLE}_geom ON gis.{TMP_TABLE} USING GIST (geom)'
        )
        return len(rows)


def attribute_road_class(project_id: str, roads_path: Path | None = None) -> dict:
    """Persist ``fclass``/``highway`` onto a project's trench segments.

    Returns a summary dict. Safe to call on projects with no roads file or no
    trench rows — it no-ops and reports zeros.
    """
    summary = {'project_id': project_id, 'trenches': 0, 'attributed': 0, 'roads': 0}

    with connection.cursor() as cur:
        cur.execute(
            'SELECT count(*) FROM gis.trench_layer WHERE project_id = %s',
            [project_id],
        )
        summary['trenches'] = int(cur.fetchone()[0])
    if summary['trenches'] == 0:
        return summary

    if roads_path is None:
        roads_path = project_roads_file(project_id)
    if roads_path is None or not roads_path.exists():
        return summary

    features = _roads_geojson(roads_path)
    if not features:
        return summary
    summary['roads'] = _load_tmp_roads(project_id, features)

    # Two-step attribution. PostgreSQL forbids referencing an UPDATE target
    # inside a LATERAL subquery, so first materialise the nearest-road mapping
    # into a temp table (a plain SELECT, where LATERAL is legal), then apply
    # it. The bbox ``&&`` prefilter (gist index on the roads temp table)
    # narrows candidates before the true-metre distance check — a plain
    # geography scan over 50k roads per trench is far too slow. ~0.0015° ≈
    # 100–165 m at Berlin latitude, safely wider than the 40 m snap so the
    # exact ST_DWithin still decides.
    map_table = TMP_TABLE + '_map'
    with connection.cursor() as cur:
        cur.execute(
            f"""
            DROP TABLE IF EXISTS gis.{map_table};
            CREATE TABLE gis.{map_table} AS
            SELECT t.id, r.fclass, r.highway, r.name
            FROM gis.trench_layer t
            CROSS JOIN LATERAL (
                SELECT r.fclass, r.highway, r.name
                FROM gis.{TMP_TABLE} r
                WHERE r.geom && ST_Expand(t.geom, 0.0015)
                  AND ST_DWithin(t.geom::geography, r.geom::geography, %s)
                ORDER BY ST_Distance(t.geom::geography, r.geom::geography)
                LIMIT 1
            ) r
            WHERE t.project_id = %s
              AND ((t.properties->>'fclass') IS NULL
                   OR (t.properties->>'street_name') IS NULL)
            """,
            [SNAP_METERS, project_id],
        )
        cur.execute(
            f"""
            UPDATE gis.trench_layer t
            SET properties = t.properties
                || jsonb_build_object(
                       'fclass', COALESCE(m.fclass, ''),
                       'highway', COALESCE(m.highway, ''),
                       'street_name', COALESCE(m.name, '')
                   )
            FROM gis.{map_table} m
            WHERE t.id = m.id
            """
        )
        summary['attributed'] = cur.rowcount
        cur.execute(f'DROP TABLE IF EXISTS gis.{map_table}')
        cur.execute(f'DROP TABLE IF EXISTS gis.{TMP_TABLE}')
    return summary


def ensure_road_class(project_id: str) -> dict:
    """Best-effort wrapper used by the HLD completion hook — never raises."""
    try:
        return attribute_road_class(project_id)
    except Exception as exc:  # noqa: BLE001
        return {'project_id': project_id, 'error': str(exc)}
