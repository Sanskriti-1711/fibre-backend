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
    DEFAULT_SOURCE_CRS,
    DESIGN_GEOJSON_FILES,
    DESIGN_PACKAGE_FILES,
    FTTH_ENGINE_URL,
    STAGES,
    SURVEY_GEOJSON_FILES,
    SURVEY_PACKAGE_FILES,
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

    The HLD engine already exports its GeoJSON layers in WGS84 using the
    OGC CRS84 URN (``urn:ogc:def:crs:OGC:1.3:CRS84``). That is the same
    lon/lat datum as EPSG:4326, so it is normalized to ``EPSG:4326`` here
    to avoid a double-reprojection that would garble every coordinate.
    """
    crs = geojson.get("crs")
    if crs and isinstance(crs, dict):
        name = str(crs.get("properties", {}).get("name", ""))
        upper = name.upper()
        # OGC CRS84 / WGS84 / EPSG:4326 are all already lon/lat WGS84.
        if "CRS84" in upper or "WGS84" in upper or "EPSG:4326" in upper:
            return "EPSG:4326"
        match = _EPSG_RE.search(name)
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


def _build_package_zip(project_id: str, gpkg_files: list, geojson_map: dict,
                         label: str) -> bytes:
    """
    Build a ZIP of pipeline outputs fetched from the FastAPI engine.

    Each file in ``gpkg_files`` is bundled under its original engine
    filename. Each entry in ``geojson_map`` (engine filename → zip
    filename) is bundled **reprojected to EPSG:4326 (WGS84)** so the
    layers render on MapLibre / web viewers without client-side
    reprojection. ``label`` is used in the "no files found" error.
    """
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        files_added = 0

        # 1. Original GPKG / document files
        for fname in gpkg_files:
            data = get_download_file(project_id, fname)
            if data is not None:
                zf.writestr(fname, data)
                files_added += 1

        # 2. GeoJSON versions (reprojected to WGS84)
        for engine_fname, zip_fname in geojson_map.items():
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
            f"No {label} files found for project '{project_id}'. "
            "The pipeline may still be running."
        )

    return zip_buffer.getvalue()


def generate_survey_package(project_id: str) -> bytes:
    """
    Generate the **field-survey package** zip — the compact subset a
    surveyor needs on site: polygons, PDPs, cables, chambers, final
    trenches, ducts (incl. drop ducts) and existing brownfield
    infrastructure.

    Includes GPKG files plus GeoJSON versions **reprojected to
    EPSG:4326 (WGS84)** so the mobile app can render them on MapLibre
    without needing a GPKG reader or client-side reprojection.

    Returns the raw zip bytes.
    """
    return _build_package_zip(
        project_id,
        SURVEY_PACKAGE_FILES,
        SURVEY_GEOJSON_FILES,
        "survey",
    )


def generate_design_package(project_id: str) -> bytes:
    """
    Generate the **HLD design package** zip — the full deliverable for
    a design engineer: every output layer (objects, polygons, PDPs,
    MFG, all trenches, cables, ducts, chambers, poles, brownfield) plus
    the generated documents (BOQ / BOM) and WGS84 GeoJSON versions.

    Prefers the engine's own ``downloads`` list (from status.json) so
    newly added layers are picked up automatically, and falls back to
    the curated ``DESIGN_PACKAGE_FILES`` list when the engine has not
    reported any downloads (e.g. engine unreachable). Raw inputs
    (address Excel, road network, brownfield source files) are excluded
    from the deliverable.

    Returns the raw zip bytes.
    """
    status = _read_status(project_id) or {}
    downloads = status.get("downloads") or []

    names = []
    for dl in downloads:
        n = dl.get("name") if isinstance(dl, dict) else str(dl)
        if not n or n.startswith(("inputs/", "brownfield/")):
            continue  # raw source inputs are not design deliverables
        if n.lower().endswith(".geojson"):
            # Known layers are added reprojected (WGS84) via the map
            # below; keep any brand-new geojson raw so nothing is lost.
            if n not in DESIGN_GEOJSON_FILES:
                names.append(n)
            continue
        names.append(n)

    # Deduplicate while preserving order
    seen = set()
    ordered = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)

    if not ordered:
        ordered = list(DESIGN_PACKAGE_FILES)

    return _build_package_zip(
        project_id,
        ordered,
        DESIGN_GEOJSON_FILES,
        "design",
    )


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


def ftth_project_payloads(limit: int = 50) -> list[dict]:
    """Serialize FtthProject rows (DB) enriched with live engine data.

    Shared by the HLD project list endpoint (/api/ftth/hld/projects/) and the
    unified projects list (/api/projects/) so HLD rows look identical in both.
    """
    from .models import FtthProject

    engine_data = {}
    try:
        for ep in list_projects():
            pid = ep.get("project_id")
            if pid:
                engine_data[pid] = ep
    except Exception:
        pass

    # Survey copies are plain Project rows linked back via source_ftth_project_id.
    from projects.models import Project as SurveyProject

    survey_copies = {}
    try:
        for sc in SurveyProject.objects.filter(source_ftth_project_id__isnull=False):
            survey_copies[sc.source_ftth_project_id] = sc
    except Exception:
        pass

    data = []
    for p in FtthProject.objects.all()[:limit]:
        enriched = engine_data.get(p.project_id, {})
        copy = survey_copies.get(p.project_id)
        engineer = p.assigned_engineer
        assigned_engineers = []
        if copy is not None:
            try:
                from assignments.models import AssignmentJob
                eng_rows = AssignmentJob.objects.filter(
                    project=copy,
                    scope=AssignmentJob.SCOPE_PROJECT,
                ).select_related("assignee")
                for job in eng_rows:
                    assigned_engineers.append({
                        "id": str(job.assignee.id),
                        "email": job.assignee.email,
                        "full_name": job.assignee.full_name,
                    })
            except Exception:
                pass
        data.append({
            "project_id": p.project_id,
            "name": p.name,
            "status": enriched.get("status", p.status),
            "progress": enriched.get("progress", p.progress),
            "stage_name": enriched.get("stage_name", p.stage_name),
            "excel_filename": p.excel_filename,
            "roads_filename": p.roads_filename,
            "created_at": p.created_at.isoformat(),
            "updated_at": p.updated_at.isoformat(),
            "downloads": enriched.get("downloads", []),
            "layers": enriched.get("layers", []),
            "assigned_engineer": {
                "id": str(engineer.id),
                "email": engineer.email,
                "full_name": engineer.full_name,
            } if engineer else None,
            "assigned_engineers": assigned_engineers,
            "assigned_at": p.assigned_at.isoformat() if p.assigned_at else None,
            "survey_copy_project_id": str(copy.id) if copy else None,
            "survey_status": copy.status if copy else None,
        })
    return data
