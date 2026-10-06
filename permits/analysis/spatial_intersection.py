"""Spatial analysis helpers over the PostGIS ``gis`` schema.

The HLD/LLD output layers live in ``gis.*`` (each row carries a ``project_id``
and a ``geom``). Reference/constraint layers (OSM railway, waterway,
environmental zones) are added as ``gis.osm_*`` read-only tables. Rules run
``ST_Intersects`` between a project's route features and the reference layers.
"""

from __future__ import annotations

from django.db import connection


def _table_exists(schema: str, table: str) -> bool:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            [schema, table],
        )
        return cur.fetchone() is not None


def gis_table_exists(table: str) -> bool:
    """Check whether ``gis.<table>`` exists (reference layers are optional)."""
    return _table_exists("gis", table)


def _has_column(schema: str, table: str, column: str) -> bool:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
            [schema, table, column],
        )
        return cur.fetchone() is not None


def reference_scope_clause(reference_table: str, alias: str = "ref") -> tuple:
    """(sql fragment, extra params) scoping a reference layer to the shared
    load plus this project's own load.

    ``gis.osm_*`` carries a nullable ``project_id``: NULL is the shared/curated
    load that serves every project inside its bbox, a value is the load made
    for one project's own area (``load_osm_reference_layers --project-id``,
    which the post-HLD chain runs). Filtering on it matters because the rows of
    other cities are in the same table — without the clause a project would be
    judged against whichever city was loaded last.

    Returns ``("", [])`` when the table predates project scoping, so the query
    stays valid against an older install.
    """
    if not _has_column("gis", reference_table, "project_id"):
        return "", []
    return f" AND ({alias}.project_id IS NULL OR {alias}.project_id = %s)", []


def project_feature_count(table: str, project_id: str) -> int:
    """Number of rows for a project in a ``gis`` output table."""
    with connection.cursor() as cur:
        cur.execute(
            f'SELECT count(*) FROM gis."{table}" WHERE project_id = %s',
            [project_id],
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def intersections_with(
    route_table: str,
    project_id: str,
    reference_table: str,
    limit: int = 300,
) -> list[dict]:
    """Route features of a project that intersect a reference layer.

    Returns a list of ``{route_id, ref_id, ref_type, crossing_lng, crossing_lat}``.
    Uses the geometry column discovered on the route table.
    """
    if not gis_table_exists(reference_table):
        return []

    geom_col = _geometry_column(route_table)
    ref_geom = _geometry_column(reference_table)
    if not geom_col or not ref_geom:
        return []

    scope_sql, _ = reference_scope_clause(reference_table)
    # The scope clause takes the project id in the JOIN condition, ahead of the
    # WHERE's — so the params follow the SQL order, not the caller's.
    scope_params = [project_id] if scope_sql else []

    sql = f"""
        SELECT r.id::text,
               r.fid::text AS route_id,
               ref.id::text AS ref_id,
               ref.properties->>'type' AS ref_type,
               ST_X(ST_Centroid(ST_Intersection(r.{geom_col}, ref.{ref_geom}))) AS x,
               ST_Y(ST_Centroid(ST_Intersection(r.{geom_col}, ref.{ref_geom}))) AS y
        FROM gis."{route_table}" r
        JOIN gis."{reference_table}" ref
          ON ST_Intersects(r.{geom_col}, ref.{ref_geom}){scope_sql}
        WHERE r.project_id = %s
        LIMIT %s
    """
    with connection.cursor() as cur:
        cur.execute(sql, scope_params + [project_id, limit])
        return [
            {
                # route_id is the per-project fid, matching the ``id`` the
                # layer GeoJSON endpoints serve to the map frontends.
                "route_id": row[1] or row[0],
                "ref_id": row[2],
                "ref_type": row[3],
                "crossing_lng": float(row[4]) if row[4] is not None else None,
                "crossing_lat": float(row[5]) if row[5] is not None else None,
            }
            for row in cur.fetchall()
        ]


def route_features_with(
    route_table: str,
    project_id: str,
    prop_key: str,
    limit: int = 300,
) -> list[dict]:
    """Route features of a project carrying a specific jsonb property value.

    Used by attribute rules (e.g. SURFACE on final_trenches). The route table
    may live in ``gis`` (HLD outputs) or be derived from the LLD layer store.
    """
    if not gis_table_exists(route_table):
        return []
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT id::text,
                   properties->>'id' AS route_id,
                   properties->>'{prop_key}' AS prop_val
            FROM gis."{route_table}"
            WHERE project_id = %s AND properties->>'{prop_key}' IS NOT NULL
            LIMIT %s
            """,
            [project_id, limit],
        )
        return [
            {"route_id": row[1] or row[0], "prop_key": prop_key, "prop_val": row[2]}
            for row in cur.fetchall()
        ]


def _geometry_column(table: str) -> str | None:
    # PostGIS geometry columns report data_type='USER-DEFINED' with
    # udt_name='geometry' — filtering on data_type='geometry' silently
    # disabled every spatial rule.
    with connection.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'gis' AND table_name = %s "
            "AND udt_name = 'geometry' LIMIT 1",
            [table],
        )
        row = cur.fetchone()
        return row[0] if row else None
