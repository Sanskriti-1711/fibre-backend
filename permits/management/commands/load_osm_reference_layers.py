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

# kumi.systems mirror is more reliable for the larger environmental queries
# (the main overpass-api.de endpoint 504s on them).
OVERPASS_URL = "https://overpass.kumi.systems/api/interpreter"
OVERPASS_TIMEOUT = 180

# Each entry: (table, overpass query body, property used for ref_type)
REFERENCE_LAYERS = {
    "osm_railway": (
        'way["railway"~"^(rail|tram|subway|light_rail|narrow_gauge|monorail)$"]',
        "railway",
    ),
    "osm_waterway": (
        'way["waterway"~"^(river|stream|canal|ditch|drain|riverbank)$"]',
        "waterway",
    ),
    "osm_environmental": (
        'way["landuse"~"^(forest|meadow|nature_reserve|grass|allotments|recreation_ground)$"];'
        'way["leisure"~"^(nature_reserve|park)$"];'
        'way["boundary"="protected_area"];'
        'way["natural"~"^(wood|wetland|heath|scrub|grassland)$"]',
        "type",
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


import time


def _element_props(el: dict) -> dict:
    tags = el.get("tags") or {}
    props: dict = {}
    for k in ("name", "railway", "waterway", "landuse", "leisure", "boundary", "natural"):
        if tags.get(k):
            props[k] = tags[k]
    props["type"] = (
        tags.get("railway") or tags.get("waterway") or tags.get("landuse")
        or tags.get("leisure") or tags.get("boundary") or tags.get("natural")
        or "unknown"
    )
    return props


def _to_geojson(el: dict) -> dict | None:
    """Convert an Overpass ``out geom`` way to a GeoJSON geometry."""
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
    return bool(tags.get("landuse") or tags.get("leisure")
                 or tags.get("boundary") or tags.get("natural"))


def _fetch_layer(bbox: tuple, body: str) -> list[dict]:
    """Fetch one layer's elements from Overpass (with retries)."""
    west, south, east, north = bbox
    query = (
        f"[out:json][timeout:90];"
        f"({body}({south},{west},{north},{east}););"
        "out geom;"
    ).encode("utf-8")
    req = urllib.request.Request(
        OVERPASS_URL,
        data=query,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "fiber-ftth-permits/1.0 (permits load command)",
        },
    )
    last_exc = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("elements", [])
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(5 * (attempt + 1))
    raise CommandError(f"Overpass request failed: {last_exc}") from last_exc


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
    help = "Load OSM reference layers (railway/waterway/environmental) into gis.osm_* tables."

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
