"""Load OSM reference layers into the ``gis.osm_*`` read-only tables.

The permit engine's spatial rules intersect a project's route layers with
these reference tables (railway crossing, waterway crossing, environmental
review). The tables are global (not project-scoped) so one Berlin-wide load
serves every project in the city.

Run::

    python manage.py load_osm_reference_layers            # Berlin default bbox
    python manage.py load_osm_reference_layers --bbox 13.29 52.38 13.46 52.48
    python manage.py load_osm_reference_layers --force     # reload even if populated

Idempotent: creates the tables if missing, truncates and reloads the
requested categories. Uses the Overpass API (https://overpass-api.de).
"""

from __future__ import annotations

import json
import urllib.request

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

# Default: Berlin administrative extent (covers the Mariendorf projects).
DEFAULT_BBOX = (13.088, 52.338, 13.761, 52.675)

# Try mirrors in order — the public endpoints are flaky and which one
# responds varies over time.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OVERPASS_TIMEOUT = 180

# Each entry: (table, overpass query body, property used for ref_type)
# Environmental is split into three categories so ENVIRONMENTAL_* rules fire
# per zone type with a precise ref_type (landuse habitat / legally protected
# area / individual tree for root-protection review).
REFERENCE_LAYERS = {
    "osm_railway": (
        'way["railway"~"^(rail|tram|subway|light_rail|narrow_gauge|monorail)$"]',
        "railway",
    ),
    "osm_waterway": (
        'way["waterway"~"^(river|stream|canal|ditch|drain|riverbank)$"]',
        "waterway",
    ),
    "osm_landuse": (
        'way["landuse"~"^(forest|meadow|grass|allotments|recreation_ground|orchard|vineyard|cemetery)$"];'
        'way["natural"~"^(wood|wetland|heath|scrub|grassland|moor|fell)$"]',
        "type",
    ),
    "osm_protected_area": (
        'way["boundary"="protected_area"];'
        'way["leisure"~"^(nature_reserve|park)$"];'
        'way["landuse"="nature_reserve"]',
        "type",
    ),
    "osm_tree": (
        'node["natural"="tree"];'
        'node["landuse"="tree"]',
        "type",
    ),
    "osm_admin_boundary": (
        # Gemeinde/Bezirk polygons (admin_level 6-9: Kreis, Verbandsgemeinde,
        # Gemeinde, Ortsteil/Bezirk). ``out geom`` on relations returns the
        # merged member geometry as a closed ring → Polygon per relation.
        'rel["boundary"="administrative"]["admin_level"~"^(6|7|8|9)$"]',
        "name",
    ),
}

DDL = (
    'CREATE SCHEMA IF NOT EXISTS gis;'
    'CREATE TABLE IF NOT EXISTS gis."{table}" ('
    '  id BIGSERIAL PRIMARY KEY,'
    '  geom GEOMETRY(Geometry, 4326),'
    '  properties JSONB NOT NULL DEFAULT \'{{}}\'::jsonb,'
    '  created_at TIMESTAMPTZ NOT NULL DEFAULT now())'
)


def _count(table: str) -> int:
    with connection.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM gis."{table}"')
        return int(cur.fetchone()[0])


import re
import time


def _element_props(el: dict) -> dict:
    tags = el.get("tags") or {}
    props: dict = {}
    for k in ("name", "railway", "waterway", "landuse", "leisure", "boundary", "natural", "admin_level"):
        if tags.get(k):
            props[k] = tags[k]
    props["type"] = (
        tags.get("railway") or tags.get("waterway") or tags.get("landuse")
        or tags.get("leisure") or tags.get("boundary") or tags.get("natural")
        or "unknown"
    )
    return props


def _to_geojson(el: dict) -> dict | None:
    """Convert an Overpass ``out geom`` element to a GeoJSON geometry.

    Handles nodes (single point — e.g. individual trees), closed ways
    (Polygon) and open ways (LineString). Relations (e.g. admin boundaries)
    carry their geometry on the members (``out geom`` puts a ``geometry``
    array on each member way), so the outer-role member rings are
    concatenated into a closed Polygon ring.
    """
    if el.get("type") == "node":
        lon, lat = el.get("lon"), el.get("lat")
        if lon is None or lat is None:
            return None
        return {"type": "Point", "coordinates": [lon, lat]}

    if el.get("type") == "relation":
        rings = []
        for m in el.get("members") or []:
            if m.get("type") != "way" or not m.get("geometry"):
                continue
            if m.get("role") not in (None, "", "outer"):
                continue  # inner holes are not needed for containment checks
            ring = [[g["lon"], g["lat"]] for g in m["geometry"]]
            if len(ring) >= 2:
                rings.append(ring)
        if not rings:
            return None
        # Concatenate member rings into a single closed outer ring.
        outer = rings[0][:-1] if rings[0][0] == rings[0][-1] else rings[0]
        for ring in rings[1:]:
            seg = ring[:-1] if ring[0] == ring[-1] else ring
            outer.extend(seg)
        if outer[0] != outer[-1]:
            outer.append(outer[0])
        if len(outer) < 4:
            return None
        return {"type": "Polygon", "coordinates": [outer]}

    geom = el.get("geometry")
    if not geom or len(geom) < 2:
        return None
    coords = [[g["lon"], g["lat"]] for g in geom]
    closed = coords[0] == coords[-1] and len(coords) > 3
    if closed:
        return {"type": "Polygon", "coordinates": [coords]}
    return {"type": "LineString", "coordinates": coords}


def _matches(table: str, tags: dict) -> bool:
    if table == "osm_railway":
        return bool(tags.get("railway"))
    if table == "osm_waterway":
        return bool(tags.get("waterway"))
    if table == "osm_landuse":
        return bool(tags.get("landuse") or tags.get("natural"))
    if table == "osm_protected_area":
        return bool(
            tags.get("boundary") == "protected_area"
            or tags.get("leisure") in ("nature_reserve", "park")
            or tags.get("landuse") == "nature_reserve"
        )
    if table == "osm_tree":
        return tags.get("natural") == "tree" or tags.get("landuse") == "tree"
    if table == "osm_admin_boundary":
        return tags.get("boundary") == "administrative"
    return False


def _fetch_layer(bbox: tuple, body: str) -> list[dict]:
    """Fetch one layer's elements from Overpass (with retries).

    The bbox is applied to EACH statement in the body, placed immediately
    after the element keyword — ``rel(bbox)[tags]`` not ``rel[tags](bbox)``.
    Tag-first scoping forces Overpass to scan relations planet-wide (504 on
    admin boundaries); bbox-first lets it use the spatial index. Node/way
    results are identical either way (verified for the tree layer).
    """
    west, south, east, north = bbox
    bbox_arg = f"({south},{west},{north},{east})"
    statements = [s.strip() for s in body.split(";") if s.strip()]
    scoped = ";".join(
        re.sub(r"^(node|way|rel)", rf"\1{bbox_arg}", s) for s in statements
    )
    query = (
        f"[out:json][timeout:90];"
        f"({scoped};);"
        "out geom;"
    ).encode("utf-8")
    last_exc = None
    for url in OVERPASS_URLS:
        req = urllib.request.Request(
            url,
            data=query,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "fiber-ftth-permits/1.0 (permits load command)",
            },
        )
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                return payload.get("elements", [])
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(3 * (attempt + 1))
    raise CommandError(f"Overpass request failed on all mirrors: {last_exc}") from last_exc


def load_categories(bbox: tuple, categories: list[str], force: bool = False) -> dict:
    """Load the requested reference categories. Returns per-table counts.

    Each layer is fetched and inserted independently so one slow layer never
    blocks the others (Overpass rate-limits and times out per query).
    """
    with connection.cursor() as cur:
        for table in categories:
            cur.execute(DDL.format(table=table))
            if force or _count(table) == 0:
                cur.execute(f'TRUNCATE gis."{table}" RESTART IDENTITY')

    counts: dict[str, int] = {}
    for table, (body, _prop_key) in REFERENCE_LAYERS.items():
        if table not in categories:
            continue
        elements = _fetch_layer(bbox, body)
        rows = []
        for el in elements:
            if not _matches(table, el.get("tags") or {}):
                continue
            geometry = _to_geojson(el)
            if not geometry:
                continue
            rows.append((json.dumps(geometry), json.dumps(_element_props(el))))
        with connection.cursor() as cur:
            if rows:
                cur.executemany(
                    f'INSERT INTO gis."{table}" (geom, properties) '
                    "VALUES (ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s::jsonb)",
                    rows,
                )
        counts[table] = len(rows)
    return counts


class Command(BaseCommand):
    help = "Load OSM reference layers (railway/waterway/environmental/tree/admin boundary) into gis.osm_* tables."

    def add_arguments(self, parser):
        parser.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"),
                            help="Bounding box (default: Berlin extent)")
        parser.add_argument("--layer", choices=list(REFERENCE_LAYERS.keys()),
                            help="Load only this layer")
        parser.add_argument("--force", action="store_true",
                            help="Truncate and reload even if a table already has rows")

    def handle(self, *args, **opts):
        bbox = tuple(opts["bbox"]) if opts["bbox"] else DEFAULT_BBOX
        categories = [opts["layer"]] if opts["layer"] else list(REFERENCE_LAYERS.keys())
        self.stdout.write(f"Fetching OSM reference data for bbox {bbox} ...")
        counts = load_categories(bbox, categories, force=opts["force"])
        for table, n in counts.items():
            self.stdout.write(self.style.SUCCESS(f"  {table}: {n} features loaded"))
        self.stdout.write(self.style.SUCCESS("Done."))
