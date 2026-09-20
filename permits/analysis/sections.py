"""Geometry helpers for civil sections of grouped trench features.

The HLD trench layer publishes **one feature per construction sub-category**
(Open Cut / Garden / HDD).  Every continuous run of that category is a
geometry *part* inside the feature — a buildable civil section.  Permit
section tables, street-wise summaries and the BOQ reference all need those
sections back, so the expansion lives here and is shared by the data access
layer (``generators.data``) and the section builder (``trench_sections``).

Everything is deliberately small and dependency-free (no Django, no GDAL) so
it can be imported from any layer of the app.
"""

from __future__ import annotations

import math
from typing import Any

# Section identity is derived from the section's midpoint rounded to 5
# decimals of a degree (~1.1 m).  Coordinates come from the same geometry on
# every code path, so the key is stable and identical wherever it is computed;
# it is robust to part re-ordering, which a positional index would not be.
MID_KEY_DECIMALS = 5


def feature_parts(feature: dict[str, Any]) -> list[list[list[float]]]:
    """Every LineString part of a GeoJSON feature (MultiLineString aware)."""
    geom = (feature or {}).get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "MultiLineString":
        return [p for p in coords if isinstance(p, list) and len(p) >= 2]
    if gtype == "LineString":
        return [coords] if isinstance(coords, list) and len(coords) >= 2 else []
    return []


def part_midpoint(part: list) -> tuple[float, float] | None:
    """Mid-vertex of a part (lon, lat), or None when unusable.

    The mid *vertex* is preferred over the average of all vertices: on a
    L-shaped run the vertex average can land off the line, while a real
    vertex always sits on the section.
    """
    if not part:
        return None
    node = part[len(part) // 2]
    try:
        return float(node[0]), float(node[1])
    except (TypeError, IndexError, ValueError):
        return None


def mid_key(part: list) -> str | None:
    """Stable lookup key for a part's street attribution."""
    mid = part_midpoint(part)
    if mid is None:
        return None
    return coord_key(*mid)


def coord_key(lon: float, lat: float) -> str:
    """Section key — MUST match the engine's ``attr_enrich._mid_key``.

    Both sides use ``%.5f`` (not ``round()``) so the key is byte-identical even
    on half-way values, where Python's banker's rounding and float formatting
    can disagree.
    """
    return f"{float(lon):.{MID_KEY_DECIMALS}f},{float(lat):.{MID_KEY_DECIMALS}f}"


def part_fingerprint(part: list) -> tuple:
    """Rounded coordinate tuple — used to de-duplicate identical parts."""
    out = []
    for coord in part or []:
        try:
            out.append((round(float(coord[0]), 6), round(float(coord[1]), 6)))
        except (TypeError, IndexError, ValueError):
            continue
    return tuple(out)


def line_length_m(coords: list) -> float:
    """Geodesic length of a stored GeoJSON line.

    The engine publishes HLD layers in EPSG:4326; a projected layer (metres)
    is detected by its coordinate magnitude and measured with plain Euclidean
    distance so the helper is safe on either.
    """
    total = 0.0
    for i in range(len(coords) - 1):
        try:
            (x1, y1), (x2, y2) = coords[i][:2], coords[i + 1][:2]
        except (TypeError, IndexError):
            continue
        if None in (x1, y1, x2, y2):
            continue
        if abs(x1) > 180 or abs(y1) > 90:
            # Projected metres (EPSG:25833) — plain Euclidean distance.
            total += math.hypot(x2 - x1, y2 - y1)
            continue
        p1, p2 = math.radians(y1), math.radians(y2)
        dp = p2 - p1
        dl = math.radians(x2 - x1)
        h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        total += 2 * 6371008.8 * math.asin(math.sqrt(h))
    return total


# Sub-metre noding slivers are not buildable (and would inflate every
# quantity downstream), so sections shorter than this are flagged SLIVER=1.
SLIVER_M = 1.0
