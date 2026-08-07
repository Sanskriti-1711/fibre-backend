"""
Pipeline proxy layer.

Instead of calling ``docker exec`` / ``docker cp`` directly, this
module proxies all pipeline operations to the **FTTH FastAPI engine**
(``ftth-engine/``), which is the single service responsible for
orchestrating ``qgis_process`` inside the Docker container.

This keeps the Django app clean and allows future services (Survey,
LLD, etc.) to reuse the same FastAPI pipeline gateway.
"""

import io
import json
import logging
import re
import zipfile
from pathlib import Path

import requests

from django.conf import settings

from .config import (
    FTTH_ENGINE_URL,
    STAGES,
    SURVEY_PACKAGE_FILES,
    SURVEY_GEOJSON_FILES,
    DEFAULT_SOURCE_CRS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Where local copies of results are cached
# ---------------------------------------------------------------------------
HOST_OUTPUTS_DIR = settings.MEDIA_ROOT / "ftth_outputs"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENGINE = FTTH_ENGINE_URL


def _engine_url(path: str) -> str:
    """Build an absolute URL for the FastAPI engine."""
    return f"{_ENGINE}{path}"


def _read_status(project_id):
    """Read locally-cached status JSON. Returns None if missing."""
    path = HOST_OUTPUTS_DIR / project_id / "status.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as exc:
            logger.warning("Corrupt status.json for %s: %s", project_id, exc)
    return None


def _write_status(data):
    """Cache a status dict to disk (for faster local reads)."""
    project_id = data.get("project_id", "unknown")
    path = HOST_OUTPUTS_DIR / project_id / "status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


# ======================================================================
# Public API: proxy to FastAPI engine
# ======================================================================


def run_pipeline(excel_path: str, roads_path: str,
                 project_id: str = None, name: str = "",
                 poly_method: int = 3, brownfield_path: str = None) -> dict:
    """
    Upload files to the FastAPI engine and start a pipeline run.

    ``brownfield_path`` is optional: a ZIP (or single vector file) of
    existing infrastructure layers that the engine unzips and feeds to
    the pipeline's BF_* parameters (USE_BROWNFIELD=true).

    Returns the JSON response from the engine (which includes
    ``project_id``, ``status``, etc.).
    """
    url = _engine_url("/ftth/hld/run")

    with open(excel_path, "rb") as ef, open(roads_path, "rb") as rf:
        files = {
            "excel": (Path(excel_path).name, ef, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "roads": (Path(roads_path).name, rf, "application/octet-stream"),
        }
        if brownfield_path:
            files["brownfield"] = (
                Path(brownfield_path).name,
                open(brownfield_path, "rb"),
                "application/octet-stream",
            )
        data = {"poly_method": str(poly_method)}
        if name:
            data["name"] = name
        if project_id:
            data["project_id"] = project_id

        resp = requests.post(url, files=files, data=data, timeout=120)
        if "brownfield" in files:
            files["brownfield"][1].close()

    if resp.status_code not in (200, 201, 202):
        detail = "Unknown error"
        try:
            body = resp.json()
            detail = body.get("detail") or body.get("message") or str(body)
        except Exception:
            detail = resp.text[:500]
        raise RuntimeError(f"Engine returned {resp.status_code}: {detail}")

    result = resp.json()

    # Cache the initial status locally
    _write_status(result)

    return result


def get_status(project_id: str) -> dict:
    """
    Get the current pipeline status from the FastAPI engine.

    Falls back to the locally-cached status if the engine is unreachable
    (so the frontend still gets a response during brief network blips).
    """
    url = _engine_url(f"/ftth/hld/results/{project_id}")

    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            _write_status(data)  # refresh local cache
            return data
    except requests.RequestException as exc:
        logger.warning("Engine unreachable for status %s: %s", project_id, exc)

    # Fallback: return locally-cached status
    cached = _read_status(project_id)
    if cached:
        return cached

    return {
        "project_id": project_id,
        "status": "unknown",
        "messages": [],
        "layers": [],
        "downloads": [],
    }


def get_layer_geojson(project_id: str, layer_name: str) -> bytes | None:
    """
    Fetch a pipeline layer as raw GeoJSON bytes from the FastAPI engine.
    """
    url = _engine_url(f"/ftth/hld/results/{project_id}/layers/{layer_name}")

    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            return resp.content
    except requests.RequestException as exc:
        logger.warning("Engine unreachable for layer %s/%s: %s",
                       project_id, layer_name, exc)

    # Fallback: try locally-cached GeoJSON
    from .config import LAYER_NAME_MAP
    entry = LAYER_NAME_MAP.get(layer_name.lower())
    if entry:
        stem = entry[0]
        host_geojson = HOST_OUTPUTS_DIR / project_id / f"{stem}.geojson"
        if host_geojson.exists():
            return host_geojson.read_bytes()

    return None


def get_download_file(project_id: str, file_path: str) -> bytes | None:
    """
    Download an output file from the FastAPI engine.
    """
    url = _engine_url(f"/ftth/hld/download/{project_id}/{file_path}")

    try:
        resp = requests.get(url, timeout=60)
        if resp.status_code == 200:
            return resp.content
    except requests.RequestException as exc:
        logger.warning("Engine unreachable for download %s/%s: %s",
                       project_id, file_path, exc)

    # Fallback: try locally-cached file
    host_file = HOST_OUTPUTS_DIR / project_id / file_path
    if host_file.exists() and host_file.is_file():
        return host_file.read_bytes()

    return None


# ---------------------------------------------------------------------------
# CRS reprojection helpers
# ---------------------------------------------------------------------------

# Regex to extract the EPSG code from a GeoJSON ``crs`` field like
# ``urn:ogc:def:crs:EPSG::25833`` or ``EPSG:25833``.
_EPSG_RE = re.compile(r"EPSG(?::|::|/)(\d+)", re.IGNORECASE)


def _detect_crs(geojson: dict) -> str:
    """
    Detect the source CRS from a GeoJSON object's ``crs`` field.
    Falls back to ``DEFAULT_SOURCE_CRS`` if not present.
    """
    crs = geojson.get("crs")
    if crs and isinstance(crs, dict):
        name = crs.get("properties", {}).get("name", "")
        match = _EPSG_RE.search(str(name))
        if match:
            return f"EPSG:{match.group(1)}"
    return DEFAULT_SOURCE_CRS


def _reproject_geojson(geojson_bytes: bytes) -> bytes:
    """
    Reproject a GeoJSON FeatureCollection from its source CRS to
    EPSG:4326 (WGS84) so that MapLibre / Mapbox can render it.

    The source CRS is auto-detected from the ``crs`` field in the
    GeoJSON. If absent, ``DEFAULT_SOURCE_CRS`` is used.

    Returns the reprojected GeoJSON as bytes with the ``crs`` field
    removed (WGS84 is the GeoJSON default).
    """
    try:
        from pyproj import Transformer
    except ImportError:
        logger.warning(
            "pyproj is not installed — GeoJSON will be bundled "
            "WITHOUT reprojection. Coordinates may be wrong. "
            "Install with: pip install pyproj"
        )
        return geojson_bytes

    try:
        data = json.loads(geojson_bytes)
    except json.JSONDecodeError as exc:
        logger.error("Invalid GeoJSON for reprojection: %s", exc)
        return geojson_bytes  # pass through unchanged

    source_crs = _detect_crs(data)

    # If already WGS84, no reprojection needed
    if source_crs.upper() in ("EPSG:4326", "WGS84"):
        return geojson_bytes

    transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
    features = data.get("features", [])
    reprojected = 0

    for feature in features:
        geom = feature.get("geometry")
        if not geom:
            continue
        _reproject_geometry(geom, transformer)
        reprojected += 1

    # Update / remove the CRS field — WGS84 is the GeoJSON default
    data.pop("crs", None)

    logger.info(
        "Reprojected %d features from %s → EPSG:4326",
        reprojected, source_crs,
    )
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _reproject_coord(coord: list, transformer) -> list:
    """
    Reproject a single coordinate pair. Preserves optional Z values.
    """
    lng, lat = transformer.transform(coord[0], coord[1])
    result = [lng, lat]
    if len(coord) > 2:
        result.append(coord[2])  # preserve elevation / Z
    return result


def _reproject_geometry(geom: dict, transformer) -> None:
    """
    Recursively reproject all coordinate pairs in a GeoJSON geometry.
    Handles 2D and 3D coordinates. Modifies ``geom`` in place.
    """
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if coords is None:
        return

    if gtype == "Point":
        geom["coordinates"] = _reproject_coord(coords, transformer)
    elif gtype in ("MultiPoint", "LineString"):
        geom["coordinates"] = [
            _reproject_coord(c, transformer) for c in coords
        ]
    elif gtype in ("MultiLineString", "Polygon"):
        geom["coordinates"] = [
            [_reproject_coord(c, transformer) for c in ring]
            for ring in coords
        ]
    elif gtype == "MultiPolygon":
        geom["coordinates"] = [
            [[_reproject_coord(c, transformer) for c in ring] for ring in poly]
            for poly in coords
        ]
    elif gtype == "GeometryCollection":
        for sub in geom.get("geometries", []):
            _reproject_geometry(sub, transformer)


def generate_survey_package(project_id: str) -> bytes:
    """
    Generate a survey package zip by fetching all GPKG + BOQ + BOM
    files from the FastAPI engine and bundling them.

    Also includes GeoJSON versions of each layer, **reprojected to
    EPSG:4326 (WGS84)**, so the mobile app can render them on MapLibre
    without needing a GPKG reader or client-side reprojection.

    Returns the raw zip bytes.
    """
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        files_added = 0

        # 1. Original GPKG + BOQ + BOM files
        for fname in SURVEY_PACKAGE_FILES:
            data = get_download_file(project_id, fname)
            if data is not None:
                zf.writestr(fname, data)
                files_added += 1

        # 2. GeoJSON versions (reprojected to WGS84) for mobile app parsing
        for engine_fname, zip_fname in SURVEY_GEOJSON_FILES.items():
            raw = get_download_file(project_id, engine_fname)
            if raw is None:
                logger.warning("GeoJSON not found: %s/%s", project_id, engine_fname)
                continue
            reprojected = _reproject_geojson(raw)
            zf.writestr(zip_fname, reprojected)
            files_added += 1

    zip_buffer.seek(0)

    if files_added == 0:
        raise FileNotFoundError(
            f"No survey files found for project '{project_id}'. "
            "The pipeline may still be running."
        )

    return zip_buffer.getvalue()


def delete_project(project_id: str) -> dict:
    """
    Delete a project from the FastAPI engine (disk + PostGIS).

    Returns the engine's response.
    """
    url = _engine_url(f"/ftth/hld/projects/{project_id}")

    try:
        resp = requests.delete(url, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        detail = "Unknown error"
        try:
            body = resp.json()
            detail = body.get("detail") or body.get("message") or str(body)
        except Exception:
            detail = resp.text[:500]
        return {"deleted": False, "detail": f"Engine returned {resp.status_code}: {detail}"}
    except requests.RequestException as exc:
        return {"deleted": False, "detail": f"Engine unreachable: {exc}"}


def list_projects() -> list[dict]:
    """List recent pipeline runs from the FastAPI engine."""
    url = _engine_url("/ftth/projects")
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException as exc:
        logger.warning("Engine unreachable for project list: %s", exc)
    return []
