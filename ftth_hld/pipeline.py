"""
Pipeline proxy layer.

Instead of calling ``docker exec`` / ``docker cp`` directly, this
module proxies all pipeline operations to the **FTTH FastAPI engine**
(``HLD_Planning_01/web/backend``), which is the single service responsible
for orchestrating ``qgis_process`` inside the Docker container.

This keeps the Django app clean and allows future services (Survey,
LLD, etc.) to reuse the same FastAPI pipeline gateway.
"""

import io
import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Optional

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
                 poly_method: int = 3, brownfield_path: str = None,
                 osm_layer_paths: dict = None) -> dict:
    """
    Upload files to the FastAPI engine and start a pipeline run.

    ``brownfield_path`` is optional: a ZIP (or single vector file) of
    existing infrastructure layers that the engine unzips and feeds to
    the pipeline's BF_* parameters (USE_BROWNFIELD=true).

    ``osm_layer_paths`` is optional: {railways, waterways, water, landuse,
    natural: path} — OSM reference layers stored with the project for
    future routing-constraint / permit use. Forwarded to the engine, which
    stores them in inputs/; the design algorithm does not consume them yet.

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
        _osm_open = []
        for key, path in (osm_layer_paths or {}).items():
            if path:
                files[key] = (Path(path).name, open(path, "rb"), "application/octet-stream")
                _osm_open.append(key)
        data = {"poly_method": str(poly_method)}
        if name:
            data["name"] = name
        if project_id:
            data["project_id"] = project_id

        resp = requests.post(url, files=files, data=data, timeout=120)
        if "brownfield" in files:
            files["brownfield"][1].close()
        for key in _osm_open:
            files[key][1].close()

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


def get_trench_design(project_id: str, include_layers: bool = True) -> dict | None:
    """Trench-design payload (status + report + design layers) from the engine.

    Phase A of ``docs/subprojects/ftth-engine/TRENCH_DESIGN.md``: the standalone
    civil trench designer runs on the project's own HLD outputs and returns the
    designed spans, structural nodes, HDD crossings, aerial drops and the aerial
    zones that drove them.

    The designer writes in the project CRS (metres, EPSG:25833), which a browser
    map cannot draw — every layer is reprojected to WGS84 here, the same way the
    pipeline layers are, so MapLibre renders it at the right place.
    """
    url = _engine_url(f"/ftth/hld/results/{project_id}/design")
    try:
        resp = requests.get(
            url, params={"layers": "true" if include_layers else "false"},
            timeout=180,
        )
        if resp.status_code != 200:
            logger.warning("Engine design payload %s -> HTTP %s",
                           project_id, resp.status_code)
            return None
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Engine unreachable for design %s: %s", project_id, exc)
        return None

    layers = data.get("layers") or {}
    source_crs = data.get("crs") or DEFAULT_SOURCE_CRS
    for name, layer in layers.items():
        geojson = layer.get("geojson") if isinstance(layer, dict) else None
        if not isinstance(geojson, dict) or not geojson.get("features"):
            continue
        # _detect_crs reads the GeoJSON `crs` member; the designer's files do
        # not carry one, so stamp the CRS the engine reported.
        geojson.setdefault("crs", {
            "type": "name", "properties": {"name": source_crs},
        })
        try:
            layer["geojson"] = json.loads(
                _reproject_geojson(json.dumps(geojson).encode("utf-8"))
            )
        except Exception as exc:  # noqa: BLE001 — keep the run usable
            logger.warning("Design layer %s/%s not reprojected: %s",
                           project_id, name, exc)
    if layers:
        data["map_crs"] = "EPSG:4326"
    return data


def run_trench_design(project_id: str, force: bool = False) -> dict | None:
    """Start (or re-run) the trench designer for a project on the engine."""
    url = _engine_url(f"/ftth/hld/design/{project_id}")
    try:
        resp = requests.post(
            url, params={"force": "true" if force else "false"}, timeout=60,
        )
        if resp.status_code in (200, 202):
            return resp.json()
        logger.warning("Engine design run %s -> HTTP %s", project_id,
                       resp.status_code)
    except requests.RequestException as exc:
        logger.warning("Engine unreachable to start design %s: %s",
                       project_id, exc)
    return None


def persist_layer(project_id: str, layer_name: str, geojson_data: dict) -> int:
    """Upsert a single layer's GeoJSON into the ``FtthLayer`` table.

    Every feature gets a human-readable ``feature_id`` (``<layer>-001``,
    ``<layer>-002``, …) if one is not already present.  This ensures every
    feature across all layers can be uniquely referenced by the survey app,
    permit engine, and LLD.

    Returns the feature count persisted. Safely no-ops if the project is not
    tracked by Django (e.g. the engine returned data for an unknown run).
    """
    from .models import FtthProject, FtthLayer

    ftth = FtthProject.objects.filter(pk=project_id).first()
    if ftth is None:
        return 0
    features = (
        geojson_data.get("features", []) if isinstance(geojson_data, dict) else []
    )
    # ── Assign human-readable feature_id: <layer>-001, <layer>-002, … ──
    for idx, feat in enumerate(features, start=1):
        props = feat.get("properties") or {}
        if not props.get("feature_id"):
            props["feature_id"] = "%s-%03d" % (layer_name, idx)
            feat["properties"] = props
    count = len(features)
    FtthLayer.objects.update_or_create(
        ftth_project=ftth,
        name=layer_name,
        defaults={"geojson": geojson_data, "feature_count": count},
    )
    return count


def backfill_feature_ids(project_id: str) -> int:
    """Add ``feature_id`` to every feature in every layer that lacks one.

    Called once per project to retrofit the unique ID onto features that
    were persisted before the feature_id assignment was added.
    Returns the total number of features updated.
    """
    from .models import FtthLayer, FtthProject

    ftth = FtthProject.objects.filter(pk=project_id).first()
    if ftth is None:
        return 0

    total_updated = 0
    for layer_obj in FtthLayer.objects.filter(ftth_project=ftth):
        fc = layer_obj.geojson
        if not isinstance(fc, dict):
            continue
        features = fc.get("features", [])
        updated = False
        for idx, feat in enumerate(features, start=1):
            props = feat.get("properties") or {}
            if not props.get("feature_id"):
                props["feature_id"] = "%s-%03d" % (layer_obj.name, idx)
                feat["properties"] = props
                total_updated += 1
                updated = True
        if updated:
            layer_obj.geojson = fc
            layer_obj.save(update_fields=["geojson"])

    return total_updated


def sync_project_layers(project_id: str, layer_names=None) -> dict:
    """Fetch every HLD output layer and persist it into ``FtthLayer`` rows.

    Idempotent — layers already in the DB are skipped so repeated polls stay
    cheap. If ``layer_names`` is omitted it is derived from the engine status.
    Returns ``{layer_name: feature_count}`` for the layers persisted.
    """
    from .models import FtthLayer

    if layer_names is None:
        status_data = get_status(project_id)
        layer_names = [
            (l.get("name") or "").lower()
            for l in status_data.get("layers", [])
            if l.get("name")
        ]
    counts = {}
    for name in layer_names:
        if FtthLayer.objects.filter(
            ftth_project__project_id=project_id, name=name
        ).exists():
            continue
        try:
            raw = get_layer_geojson(project_id, name)
        except Exception:
            continue
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        counts[name] = persist_layer(project_id, name, data)
    return counts


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
                         label: str, extra_files: Optional[dict] = None) -> bytes:
    """
    Build a ZIP of pipeline outputs fetched from the FastAPI engine.

    Each file in ``gpkg_files`` is bundled under its original engine
    filename. Each entry in ``geojson_map`` (engine filename → zip
    filename) is bundled **reprojected to EPSG:4326 (WGS84)** so the
    layers render on MapLibre / web viewers without client-side
    reprojection. ``label`` is used in the "no files found" error.

    ``extra_files`` (optional) maps zip filename → bytes and **overrides**
    any engine file of the same name — used to inject the database-computed
    BOQ/BOM workbooks over the engine's hardcoded templates.
    """
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        files_added = 0
        extra_files = extra_files or {}

        # 1. Original GPKG / document files (skipped when an override exists)
        for fname in gpkg_files:
            if fname in extra_files:
                continue
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

        # 3. Extra generated documents (override engine files of same name)
        for zip_fname, data in extra_files.items():
            zf.writestr(zip_fname, data)
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

    # Inject the database-computed BOQ/BOM over the engine's hardcoded
    # template files (see ftth_hld/boq.py — quantities come from the
    # persisted HLD layers, priced by the BoqRate card).
    extra_files = {}
    try:
        from .boq import render_boq_xlsx

        extra_files["BOQ.xlsx"] = render_boq_xlsx(project_id, sheets="boq")
        extra_files["BOM.xlsx"] = render_boq_xlsx(project_id, sheets="bom")
    except Exception as exc:
        logger.warning("Could not generate BOQ/BOM for design package: %s", exc)

    return _build_package_zip(
        project_id,
        ordered,
        DESIGN_GEOJSON_FILES,
        "design",
        extra_files=extra_files,
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
    # Most-recently-active first: updated_at moves on every re-run/poll,
    # so a re-run of an old project surfaces at the top instead of its
    # original creation-date position (fall back to created_at when equal).
    for p in FtthProject.objects.all().order_by(
        "-updated_at", "-created_at"
    )[:limit]:
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
