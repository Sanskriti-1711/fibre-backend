"""Street-level grouping of permit matrix rows (``permit_group``).

Segment-level rows stay in the matrix (the HLD map colours per segment and
the survey/variation hooks key on route sections); this module assigns a
street (road name) group key so the tracker and the permit-package forms can
present **one permit per street** instead of one per trench segment.

* ROAD_AUTHORITY rows (layer ``trench_layer``) — street comes from the trench
  properties (``street_name``, stamped by the fclass/street backfill).
* TRAFFIC rows (layer ``final_trenches``) — nearest named road from the LLD
  feature geometry (same 40 m snap as the fclass backfill).
* UTILITY_REUSE stays per asset (informational — feeds the conflict report),
  so its ``permit_group`` is left blank.

Idempotent: only fills blank ``permit_group`` values. Never raises — a
missing roads file records ``no_roads`` for the caller to log as a gap.
"""

from __future__ import annotations

from django.db import connection

from .road_class import _load_tmp_roads, _roads_geojson, project_roads_file


def assign_groups(project_id: str, roads_path=None) -> dict:
    """Assign street group keys to a project's permit matrix rows."""
    summary = {
        "project_id": project_id,
        "trench_rows": 0,
        "lld_rows": 0,
        "no_roads": False,
    }

    # 1. trench-layer rows (ROAD_AUTHORITY + spatial hits on trench_layer):
    #    street name already persisted on the trench properties; unnamed
    #    sections fall back to a class group ("residential (unnamed)") so
    #    segments not on a named road still club together.
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE business.ftth_permit_matrix pm
            SET permit_group = COALESCE(
                    NULLIF(t.properties->>'street_name', ''),
                    NULLIF(concat(t.properties->>'fclass', ' (unnamed)'), ' (unnamed)'),
                    '')
            FROM gis.trench_layer t
            WHERE pm.project_id = %s
              AND pm.layer = 'trench_layer'
              AND pm.route_section = t.id::text
              AND NULLIF(pm.permit_group, '') IS NULL
            """,
            [project_id],
        )
        summary["trench_rows"] = cur.rowcount

    # 2. final_trenches rows (TRAFFIC_001): nearest named road from the LLD
    #    layer geometry (same spatial join + snap as the fclass backfill).
    if roads_path is None:
        roads_path = project_roads_file(project_id)
    if roads_path is None or not roads_path.exists():
        summary["no_roads"] = True
        return summary
    features = _roads_geojson(roads_path)
    if not features:
        summary["no_roads"] = True
        return summary

    with connection.cursor() as cur:
        _load_tmp_roads(project_id, features)
        cur.execute(
            """
            UPDATE business.ftth_permit_matrix pm
            SET permit_group = COALESCE(
                    NULLIF(m.street, ''),
                    NULLIF(concat(m.surface, ' (unnamed)'), ' (unnamed)'),
                    '')
            FROM (
                SELECT DISTINCT ON (ft.fid) ft.fid, r.name AS street, ft.surface
                FROM (
                    SELECT e.value->'properties'->>'feature_id' AS fid,
                           e.value->'properties'->>'SURFACE' AS surface,
                           ST_SetSRID(ST_GeomFromGeoJSON((e.value->'geometry')::text), 4326) AS geom
                    FROM business.ftth_lld_layers l,
                         jsonb_array_elements(l.geojson->'features') e
                    WHERE l.name = 'final_trenches'
                      AND l.lld_run_id = (
                          SELECT id FROM business.ftth_lld_runs
                          WHERE ftth_project_id = %s
                          ORDER BY run_date DESC LIMIT 1)
                ) ft
                CROSS JOIN LATERAL (
                    SELECT rr.name
                    FROM gis._roads_tmp rr
                    WHERE rr.geom && ST_Expand(ft.geom, 0.0015)
                      AND ST_DWithin(ft.geom::geography, rr.geom::geography, 40)
                    ORDER BY ST_Distance(ft.geom::geography, rr.geom::geography)
                    LIMIT 1
                ) r
                WHERE ft.geom IS NOT NULL
            ) m
            WHERE pm.project_id = %s
              AND pm.layer = 'final_trenches'
              AND pm.route_section = m.fid
              AND NULLIF(pm.permit_group, '') IS NULL
              -- UTILITY_REUSE stays per asset (informational — conflict report)
              AND NOT EXISTS (
                  SELECT 1 FROM business.ftth_permit_rules r
                  WHERE r.id = pm.rule_id AND r.rule_id = 'UTILITY_REUSE_001')
            """,
            [project_id, project_id],
        )
        summary["lld_rows"] = cur.rowcount
    return summary
