"""Build the section-level trench table — one row per buildable civil section.

The HLD trench layer publishes one feature per construction sub-category
(Open Cut / Garden / HDD), each holding many continuous runs as geometry
parts.  That is right for the map but too coarse for permits, street tables
and the BOQ reference: a 4 km grouped feature cannot carry a single
``fclass``, a single street name or a permit row per street.

This module expands those grouped features into **sections** (one per
geometry part), attributes each section to the road it runs along (same 40 m
snap as the fclass backfill, but per section instead of per feature) and
persists the result as the ``trench_sections`` layer in the HLD layer store,
source ``gis.trench_layer``.  Section identity is ``<gis_id>#<SECTION_ID>``
so a section always traces back to the feature it came from — the frontend
matches permit rows on the part before the ``#``, and municipality/variation
joins use the same split.

Idempotent and never raising: a missing roads file (or a project with no
trench rows) records a gap and leaves the geometry-only sections in place, so
street tables degrade to "Unnamed street" rather than failing the run.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.db import connection

from .road_class import SNAP_METERS, _load_tmp_roads, _roads_geojson, project_roads_file
from .sections import (
    SLIVER_M,
    coord_key,
    feature_parts,
    line_length_m,
    part_fingerprint,
    part_midpoint,
)

SECTION_LAYER = "trench_sections"
MID_TABLE = "_trench_section_mid"


def _trench_rows(project_id: str) -> list[tuple]:
    """(id, properties, geometry-geojson) for a project's trench features."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT id, properties, ST_AsGeoJSON(geom) "
            "FROM gis.trench_layer WHERE project_id = %s AND geom IS NOT NULL "
            "ORDER BY id",
            [project_id],
        )
        return cur.fetchall()


def _as_props(raw) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return dict(raw or {})


def _props_street_map(props: dict) -> dict:
    """Per-section street map written by the engine's enrichment pass.

    Keys are section mid vertices ("lon,lat", 5 dp) — identical to
    ``sections.coord_key`` — and values carry the street / road class. The
    engine writes short keys (``n``/``f``); normalise them here.
    """
    raw = props.get("SECTION_STREETS") or props.get("section_streets")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            out[str(key)] = {"street_name": value.strip(), "fclass": "", "highway": ""}
            continue
        if not isinstance(value, dict):
            continue
        out[str(key)] = {
            "street_name": str(value.get("n") or value.get("street_name") or "").strip(),
            "fclass": str(value.get("f") or value.get("fclass") or "").strip(),
            "highway": str(value.get("h") or value.get("highway") or "").strip(),
        }
    return out


def _street_by_midpoint(midpoints: list[tuple[str, float, float]]) -> dict[str, dict]:
    """Nearest named road for each section midpoint (one spatial join)."""
    if not midpoints:
        return {}
    with connection.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS gis.{MID_TABLE}")
        cur.execute(
            f"CREATE TABLE gis.{MID_TABLE} ("
            "  mk TEXT PRIMARY KEY,"
            "  geom GEOMETRY(Point, 4326))"
        )
        try:
            from psycopg2.extras import execute_values

            execute_values(
                cur,
                f"INSERT INTO gis.{MID_TABLE} (mk, geom) VALUES %s",
                midpoints,
                template="(%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))",
                page_size=500,
            )
        except ImportError:  # pragma: no cover - psycopg2 always present
            cur.executemany(
                f"INSERT INTO gis.{MID_TABLE} (mk, geom) "
                "VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))",
                midpoints,
            )
        cur.execute(
            f"CREATE INDEX idx_{MID_TABLE}_geom ON gis.{MID_TABLE} USING GIST (geom)"
        )
        cur.execute(
            f"""
            SELECT m.mk, r.name, r.fclass, r.highway
            FROM gis.{MID_TABLE} m
            CROSS JOIN LATERAL (
                SELECT rr.name, rr.fclass, rr.highway
                FROM gis._roads_tmp rr
                WHERE rr.geom && ST_Expand(m.geom, 0.0015)
                  AND ST_DWithin(m.geom::geography, rr.geom::geography, %s)
                ORDER BY ST_Distance(m.geom::geography, rr.geom::geography)
                LIMIT 1
            ) r
            """,
            [SNAP_METERS],
        )
        out = {
            str(mk): {
                "street_name": (name or "").strip(),
                "fclass": (fclass or "").strip(),
                "highway": (highway or "").strip(),
            }
            for mk, name, fclass, highway in cur.fetchall()
        }
        cur.execute(f"DROP TABLE IF EXISTS gis.{MID_TABLE}")
    return out


def build_trench_sections(project_id: str, roads_path: Path | None = None) -> dict:
    """Expand the grouped trench layer into street-attributed sections.

    Returns ``{sections, slivers, attributed, streets, roads, no_roads,
    persisted}``.  Safe to call repeatedly; it replaces the project's
    ``trench_sections`` layer with a fresh build.
    """
    summary = {
        "project_id": project_id,
        "rows": 0,
        "sections": 0,
        "slivers": 0,
        "attributed": 0,
        "streets": 0,
        "roads": 0,
        "no_roads": False,
        "persisted": False,
    }

    trench_rows = _trench_rows(project_id)
    summary["rows"] = len(trench_rows)
    if not trench_rows:
        return summary

    # ── expand every feature into its parts ─────────────────────────────
    expanded: list[dict] = []
    midpoints: list[tuple[str, float, float]] = []
    street_map: dict[str, dict] = {}
    seen: set[tuple] = set()
    for gid, raw_props, geom_json in trench_rows:
        props = _as_props(raw_props)
        try:
            geom = json.loads(geom_json) if geom_json else None
        except (TypeError, ValueError):
            geom = None
        if not geom:
            continue
        # Street attribution is written per section by the ENGINE's enrichment
        # pass, which is where GDAL and the project's roads layer live.  The
        # backend has no GDAL, so read the map instead of re-joining roads.
        per_feature = _props_street_map(props)
        for part in feature_parts({"geometry": geom}):
            fp = part_fingerprint(part)
            if len(fp) < 2 or fp in seen:
                continue
            seen.add(fp)
            length = line_length_m(part)
            if length <= 0:
                continue
            mid = part_midpoint(part)
            key = coord_key(*mid) if mid else None
            info = per_feature.get(key) if key else None
            if info:
                street_map[key] = info
            expanded.append(
                {"gid": gid, "props": props, "part": part, "length": length,
                 "key": key, "info": info or {}}
            )

    # ── fallback: no engine attribution on this run — join roads here ─────
    # Only attempted when the trench layer carries no SECTION_STREETS at all
    # (e.g. a run produced before the enrichment pass existed) and the backend
    # happens to have GDAL.  Both cases are expected to be rare.
    if not street_map:
        if roads_path is None:
            roads_path = project_roads_file(project_id)
        if roads_path is None or not roads_path.exists():
            summary["no_roads"] = True
        else:
            try:
                features = _roads_geojson(roads_path)
                if features:
                    summary["roads"] = _load_tmp_roads(project_id, features)
            except Exception:  # noqa: BLE001 - no GDAL on the backend
                features = []
                summary["roads"] = 0
            if midpoints and summary["roads"]:
                try:
                    street_map = _street_by_midpoint(midpoints)
                except Exception:  # noqa: BLE001 - attribution is never fatal
                    street_map = {}
            for item in expanded:
                item["info"] = street_map.get(item["key"] or "", {}) or item["info"]

    kind_counts: dict[str, int] = {}
    section_features: list[dict] = []
    streets_seen: set[str] = set()

    for item in expanded:
        props = item["props"]
        kind = str(
            props.get("trench_type") or props.get("CONSTRUCT") or props.get("USAGE_TYPE")
            or "Open Cut"
        )
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        section_id = "%s-%04d" % (kind.replace(" ", "-"), kind_counts[kind])
        info = item.get("info") or street_map.get(item["key"] or "", {}) or {}
        if info.get("street_name"):
            streets_seen.add(info["street_name"])
        out = {k: v for k, v in props.items() if not str(k).startswith("_")}
        out.update(
            {
                "PARENT_FEATURE_ID": str(item["gid"]),
                "SECTION_ID": section_id,
                "SECTION_LEN_M": round(item["length"], 2),
                "length_m": round(item["length"], 2),
                "SLIVER": 1 if item["length"] < SLIVER_M else 0,
                "street_name": info.get("street_name", ""),
                "fclass": info.get("fclass", ""),
                "highway": info.get("highway", ""),
            }
        )
        if out["SLIVER"]:
            summary["slivers"] += 1
        if info.get("fclass"):
            summary["attributed"] += 1
        section_features.append(
            {
                "type": "Feature",
                "id": "%s#%s" % (item["gid"], section_id),
                "properties": {
                    "feature_id": "%s#%s" % (item["gid"], section_id),
                    **out,
                },
                "geometry": {"type": "LineString", "coordinates": item["part"]},
            }
        )

    summary["sections"] = len(section_features)
    summary["streets"] = len(streets_seen)

    # ── persist the section layer (the summary/permits read this) ────────
    try:
        from ftth_hld.models import FtthLayer
        from ftth_hld.pipeline import persist_layer

        fc = {"type": "FeatureCollection", "features": section_features}
        persist_layer(project_id, SECTION_LAYER, fc)
        summary["persisted"] = True
    except Exception:  # noqa: BLE001 - persistence is best-effort
        summary["persisted"] = False

    # NB: the section streets are NOT written back to gis.trench_layer. The
    # engine's enrichment pass owns SECTION_STREETS / STREET_NAME / N_SECTIONS
    # on that table; re-writing them here would clobber the compact engine map
    # with a longer backend copy for no benefit.
    return summary


def sections_are_fresh(project_id: str) -> bool:
    """True when the persisted section layer matches the current trench layer.

    Cheap guard for the completion hook: rebuild only when the trench layer
    has changed since the section layer was written.
    """
    from ftth_hld.models import FtthLayer

    project = FtthLayer.objects.filter(
        ftth_project__project_id=project_id, name="trenches"
    ).only("updated_at").first()
    sections = FtthLayer.objects.filter(
        ftth_project__project_id=project_id, name=SECTION_LAYER
    ).only("updated_at", "feature_count").first()
    if project is None or sections is None:
        return False
    if not sections.feature_count:
        return False
    return sections.updated_at >= project.updated_at
