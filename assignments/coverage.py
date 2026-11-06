"""Which features a polygon assignment covers.

A supervisor can assign an area rather than the whole project. The engineer may
edit everything that belongs to that area:

* rows whose properties carry the polygon's id (``polygon_id``, a service-area
  id, a zone reference — any scalar property with that value), and
* rows whose geometry lies inside, or crosses into, the polygon.

The set is computed once when the assignment is saved and stored on the job so
the API does not re-walk the project's features on every request. Geometry
matching is deliberately dependency-free: GeoJSON coordinate maths only, so it
behaves the same on SQLite and PostGIS.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from projects.models import Feature

POLYGON_TYPES = ('Polygon', 'MultiPolygon')

Point = tuple[float, float]
Ring = list[Point]
Segment = tuple[Point, Point]


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_point(value: Any) -> Point | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    lng, lat = _number(value[0]), _number(value[1])
    if lng is None or lat is None:
        return None
    return (lng, lat)


def _as_ring(value: Any) -> Ring:
    if not isinstance(value, (list, tuple)):
        return []
    ring = [point for point in (_as_point(vertex) for vertex in value) if point]
    # GeoJSON rings repeat their first point; the maths below does not need it.
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring.pop()
    return ring


def _rings(geometry: Any) -> list[list[Ring]]:
    """Polygon or MultiPolygon -> list of polygons, each a list of rings."""
    if not isinstance(geometry, dict):
        return []
    kind = geometry.get('type')
    coordinates = geometry.get('coordinates')
    if kind == 'Polygon':
        polygons = [coordinates]
    elif kind == 'MultiPolygon':
        polygons = coordinates if isinstance(coordinates, (list, tuple)) else []
    else:
        return []
    rings: list[list[Ring]] = []
    for polygon in polygons:
        if not isinstance(polygon, (list, tuple)):
            continue
        parsed = [_as_ring(ring) for ring in polygon]
        parsed = [ring for ring in parsed if len(ring) >= 3]
        if parsed:
            rings.append(parsed)
    return rings


def _bbox(points: Iterable[Point]) -> tuple[float, float, float, float] | None:
    points = list(points)
    if not points:
        return None
    lngs = [point[0] for point in points]
    lats = [point[1] for point in points]
    return (min(lngs), min(lats), max(lngs), max(lats))


def _bbox_contains(outer, inner) -> bool:
    if outer is None or inner is None:
        return False
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def _bbox_overlaps(a, b) -> bool:
    if a is None or b is None:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _point_in_ring(lng: float, lat: float, ring: Ring) -> bool:
    inside = False
    count = len(ring)
    if count < 3:
        return False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > lat) != (y2 > lat):
            crossing = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lng < crossing:
                inside = not inside
        previous = current
    return inside


def _point_in_polygon(lng: float, lat: float, polygons: list[list[Ring]]) -> bool:
    for rings in polygons:
        if not rings:
            continue
        if not _point_in_ring(lng, lat, rings[0]):
            continue
        if any(_point_in_ring(lng, lat, hole) for hole in rings[1:]):
            continue
        return True
    return False


def _ccw(a: Point, b: Point, c: Point) -> bool:
    return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    return _ccw(a, c, d) != _ccw(b, c, d) and _ccw(a, b, c) != _ccw(a, b, d)


def _flat_points(value: Any) -> Iterable[Point]:
    """Every coordinate in a GeoJSON coordinates tree."""
    if not isinstance(value, (list, tuple)):
        return
    point = _as_point(value)
    if point is not None:
        yield point
        return
    for item in value:
        yield from _flat_points(item)


def _line_segments(value: Any) -> Iterable[tuple[Point, Point]]:
    if not isinstance(value, (list, tuple)):
        return
    line = [point for point in (_as_point(vertex) for vertex in value) if point]
    for start, end in zip(line, line[1:]):
        yield (start, end)


def _geometry_segments(geometry: Any) -> Iterable[tuple[Point, Point]]:
    if not isinstance(geometry, dict):
        return
    kind = geometry.get('type')
    coordinates = geometry.get('coordinates')
    if kind == 'LineString':
        yield from _line_segments(coordinates)
    elif kind == 'MultiLineString':
        for line in coordinates or []:
            yield from _line_segments(line)
    elif kind in POLYGON_TYPES:
        for polygon in _rings(geometry):
            for ring in polygon:
                for start, end in zip(ring, [*ring[1:], ring[0]]):
                    yield (start, end)
    elif kind == 'GeometryCollection':
        for child in geometry.get('geometries') or []:
            yield from _geometry_segments(child)


def _geometry_points(geometry: Any) -> list[Point]:
    if not isinstance(geometry, dict):
        return []
    kind = geometry.get('type')
    if kind == 'GeometryCollection':
        points: list[Point] = []
        for child in geometry.get('geometries') or []:
            points.extend(_geometry_points(child))
        return points
    return list(_flat_points(geometry.get('coordinates')))


class PolygonArea:
    """A parsed polygon with the checks a coverage decision needs."""

    def __init__(self, geometry: Any, polygon_id: str = ''):
        self.geometry = geometry if isinstance(geometry, dict) else None
        self.polygon_id = str(polygon_id or '').strip().lower()
        self.polygons = _rings(self.geometry)
        self.points = [point for polygon in self.polygons for ring in polygon for point in ring]
        self.bbox = _bbox(self.points)
        self.edges: list[Segment] = []
        for polygon in self.polygons:
            for ring in polygon:
                self.edges.extend(
                    (start, end) for start, end in zip(ring, [*ring[1:], ring[0]])
                )

    @property
    def valid(self) -> bool:
        return bool(self.polygons and self.bbox)

    def contains_point(self, lng: float, lat: float) -> bool:
        return self.valid and _point_in_polygon(lng, lat, self.polygons)

    def carries_id(self, properties: Any, feature_id: str = '') -> bool:
        """True when the row names this polygon in any property."""
        target = self.polygon_id
        if not target:
            return False
        if feature_id and feature_id.strip().lower() == target:
            return True
        if not isinstance(properties, dict):
            return False
        for value in properties.values():
            if isinstance(value, (str, int, float)) and str(value).strip().lower() == target:
                return True
        return False

    def covers(self, geometry: Any) -> bool:
        """True when a feature geometry belongs inside the area."""
        if not self.valid or not isinstance(geometry, dict):
            return False
        points = _geometry_points(geometry)
        if not points:
            return False
        if _bbox_contains(self.bbox, _bbox(points)):
            return True
        if not _bbox_overlaps(self.bbox, _bbox(points)):
            return False
        if any(self.contains_point(lng, lat) for lng, lat in points):
            return True
        # A line can cut the corner with both endpoints outside; a polygon can
        # straddle the ring. Either way, touching a boundary means it is in the
        # engineer's area.
        for start, end in _geometry_segments(geometry):
            if any(
                _segments_intersect(start, end, edge_start, edge_end)
                for edge_start, edge_end in self.edges
            ):
                return True
        return False


def feature_ids_in_polygon(
    project_id: Any,
    geometry: Any,
    polygon_id: str = '',
) -> list[str]:
    """Ids of the project's features a polygon assignment covers."""
    area = PolygonArea(geometry, polygon_id)
    if not area.valid:
        return []

    covered: list[str] = []
    rows = Feature.objects.filter(project_id=project_id).only(
        'id', 'properties', 'geometry', 'layer_id'
    )
    for feature in rows.iterator():
        feature_id = str(feature.id)
        if area.carries_id(feature.properties, feature_id):
            covered.append(feature_id)
        elif area.covers(feature.geometry):
            covered.append(feature_id)
    return covered


def resolve_polygon_geometry(project, polygon_id: str):
    """Find the polygon feature an id names and return its Geometry + name.

    The planner normally sends the geometry with the assignment. When it only
    sends the polygon id, the polygon row is looked up by UUID or by any
    property carrying that id, and its geometry is copied onto the job.
    """
    target = str(polygon_id or '').strip()
    if not target:
        return None, ''
    for feature in Feature.objects.filter(project=project).only(
        'id', 'properties', 'geometry', 'layer_name'
    ).iterator():
        geometry = feature.geometry
        if not isinstance(geometry, dict) or geometry.get('type') not in POLYGON_TYPES:
            continue
        if str(feature.id).lower() != target.lower() and not PolygonArea(
            geometry, target
        ).carries_id(feature.properties, str(feature.id)):
            continue
        properties = feature.properties if isinstance(feature.properties, dict) else {}
        name = str(
            properties.get('name')
            or properties.get('area_name')
            or properties.get('polygon_name')
            or properties.get('label')
            or feature.layer_name
            or target
        )
        return geometry, name
    return None, ''
