"""Resolve each project trench's municipality (Gemeinde) from the OSM admin
boundary reference layer and stamp it onto the trench properties + permit
matrix rows.

Mirrors the fclass backfill pattern (``permits/analysis/road_class.py``): a
read-only spatial join against a persisted reference layer, idempotent, and
never raising — a missing layer records a gap instead of failing the run.
Manual overrides win: only blank matrix ``municipality`` values are filled.
"""

from __future__ import annotations

from django.db import connection


def attribute_municipality(project_id: str) -> dict:
    """Resolve and persist municipality for a project's HLD trenches.

    Returns a summary dict (``layer_missing`` / ``resolved`` / ``rows_updated``)
    so callers can record honest gaps — never an exception.
    """
    summary = {
        "project_id": project_id,
        "trenches": 0,
        "resolved": 0,
        "rows_updated": 0,
        "layer_missing": False,
    }

    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass('gis.osm_admin_boundary')")
        if cur.fetchone()[0] is None:
            summary["layer_missing"] = True
            return summary

        cur.execute(
            "SELECT count(*) FROM gis.trench_layer WHERE project_id = %s",
            [project_id],
        )
        summary["trenches"] = int(cur.fetchone()[0])
        if summary["trenches"] == 0:
            return summary

        # Midpoint of each trench -> the smallest containing admin polygon
        # (most specific admin level: Bezirk/Gemeinde over Kreis/state).
        # DISTINCT ON keeps exactly one municipality per trench; the bbox
        # ``&&`` prefilter uses the gist index before the exact contains.
        cur.execute(
            """
            SELECT DISTINCT ON (t.id) t.id, a.properties->>'name' AS muni
            FROM gis.trench_layer t
            JOIN gis.osm_admin_boundary a
              ON a.geom && ST_Expand(t.geom, 0.001)
             AND ST_Contains(a.geom, ST_PointOnSurface(t.geom))
            WHERE t.project_id = %s
            ORDER BY t.id, ST_Area(a.geom) ASC
            """,
            [project_id],
        )
        rows = cur.fetchall()
        if not rows:
            return summary
        summary["resolved"] = len(rows)

        # Persist onto trench properties (traceability, mirrors fclass).
        _update_trench_properties(cur, project_id, rows)

        # Fill blank municipality on the project's permit matrix rows
        # (route_section for trench-keyed rows is the trench id string).
        # Manual overrides win: only NULL/blank values are touched.
        # NB: psycopg2's execute_values allows exactly ONE ``%s`` (the VALUES
        # list) — other params are inlined as literals (project_id is an
        # internal UUID hex, safe to embed). cur.rowcount after execute_values
        # only reflects the last page, so measure the affected rows up front.
        _pid = f"'{project_id}'"
        cur.execute(
            "SELECT count(*) FROM business.ftth_permit_matrix pm "
            f"WHERE pm.project_id = {_pid} "
            "  AND NULLIF(pm.municipality, '') IS NULL "
            "  AND pm.route_section IN ("
            "      SELECT t.id::text FROM gis.trench_layer t "
            f"      WHERE t.project_id = {_pid})",
        )
        blank_before = int(cur.fetchone()[0])
        try:
            from psycopg2.extras import execute_values
            execute_values(
                cur,
                "UPDATE business.ftth_permit_matrix pm "
                "SET municipality = m.muni "
                "FROM (VALUES %s) AS m(route_id, muni) "
                f"WHERE pm.project_id = {_pid} "
                "  AND pm.route_section = m.route_id::text "
                "  AND NULLIF(pm.municipality, '') IS NULL",
                rows,
                template="(%s::bigint, %s)",
                page_size=500,
            )
        except ImportError:  # pragma: no cover - psycopg2 always present
            cur.executemany(
                "UPDATE business.ftth_permit_matrix pm "
                "SET municipality = %s "
                "WHERE pm.project_id = %s "
                "  AND pm.route_section = %s "
                "  AND NULLIF(pm.municipality, '') IS NULL",
                [(muni, project_id, str(rid)) for rid, muni in rows],
            )
        summary["rows_updated"] = blank_before
    return summary


def _update_trench_properties(cur, project_id: str, rows: list) -> None:
    """Stamp ``municipality`` into trench properties (jsonb), batched."""
    _pid = f"'{project_id}'"
    try:
        from psycopg2.extras import execute_values
        execute_values(
            cur,
            "UPDATE gis.trench_layer t "
            "SET properties = t.properties "
            "  || jsonb_build_object('municipality', COALESCE(m.muni, '')) "
            "FROM (VALUES %s) AS m(id, muni) "
            f"WHERE t.id = m.id AND t.project_id = {_pid}",
            rows,
            template="(%s::bigint, %s)",
            page_size=500,
        )
    except ImportError:  # pragma: no cover - psycopg2 always present
        cur.executemany(
            "UPDATE gis.trench_layer t "
            "SET properties = t.properties "
            "  || jsonb_build_object('municipality', COALESCE(%s, '')) "
            "WHERE t.id = %s AND t.project_id = %s",
            [(muni, rid, project_id) for rid, muni in rows],
        )
