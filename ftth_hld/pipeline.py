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
import zipfile
from pathlib import Path

import requests

from django.conf import settings

from .config import FTTH_ENGINE_URL, STAGES, SURVEY_PACKAGE_FILES

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
                 poly_method: int = 3) -> dict:
    """
    Upload files to the FastAPI engine and start a pipeline run.

    Returns the JSON response from the engine (which includes
    ``project_id``, ``status``, etc.).
    """
    url = _engine_url("/ftth/hld/run")

    with open(excel_path, "rb") as ef, open(roads_path, "rb") as rf:
        files = {
            "excel": (Path(excel_path).name, ef, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "roads": (Path(roads_path).name, rf, "application/octet-stream"),
        }
        data = {"poly_method": str(poly_method)}
        if name:
            data["name"] = name
        if project_id:
            data["project_id"] = project_id

        resp = requests.post(url, files=files, data=data, timeout=120)

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


def generate_survey_package(project_id: str) -> bytes:
    """
    Generate a survey package zip by fetching all GPKG + BOQ + BOM
    files from the FastAPI engine and bundling them.

    Returns the raw zip bytes.
    """
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        files_added = 0
        for fname in SURVEY_PACKAGE_FILES:
            data = get_download_file(project_id, fname)
            if data is not None:
                zf.writestr(fname, data)
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
