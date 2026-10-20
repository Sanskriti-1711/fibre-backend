"""
Pipeline proxy layer.

Instead of calling ``docker exec`` / ``docker cp`` directly, this
module proxies all pipeline operations to the **FTTH FastAPI engine**
(``HLD_Planning_01/web/backend``), which is the single service responsible
for orchestrating ``qgis_process`` inside the Docker container.

This keeps the Django app clean and allows future services (Survey,
LLD, etc.) to reuse the same FastAPI pipeline gateway.
"""

import contextlib
import io
import json
import logging
import os
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import requests
from django.conf import settings

from projects.layer_rules import (
    FIELD_SURVEY_ALWAYS_PACKAGED_LAYERS,
    FIELD_SURVEY_EXCLUDED_LAYERS,
)

from .config import (
    DEFAULT_SOURCE_CRS,
    DESIGN_GEOJSON_FILES,
    DESIGN_PACKAGE_FILES,
    DISPLAYED_LAYER_FILES,
    FTTH_ENGINE_URL,
    LAYER_STEM_GEOMETRY,
    LAYER_STEM_GIS_TABLES,
    LAYER_STEM_SUBLAYER,
    SURVEY_GEOJSON_FILES,
    SURVEY_PACKAGE_FILES,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Where local copies of results are cached
# ---------------------------------------------------------------------------
HOST_OUTPUTS_DIR = settings.MEDIA_ROOT / 'ftth_outputs'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENGINE = FTTH_ENGINE_URL


def _engine_url(path: str) -> str:
    """Build an absolute URL for the FastAPI engine."""
    return f'{_ENGINE}{path}'


def _read_status(project_id: str) -> dict | None:
    """Read locally-cached status JSON. Returns None if missing."""
    path = HOST_OUTPUTS_DIR / project_id / 'status.json'
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
            logger.warning('Unexpected status.json shape for %s', project_id)
        except Exception as exc:
            logger.warning('Corrupt status.json for %s: %s', project_id, exc)
    return None


def _write_status(data):
    """Cache a status dict to disk (for faster local reads)."""
    project_id = data.get('project_id', 'unknown')
    path = HOST_OUTPUTS_DIR / project_id / 'status.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


# ======================================================================
# Public API: proxy to FastAPI engine
# ======================================================================


def run_pipeline(
    excel_path: str,
    roads_path: str,
    project_id: str | None = None,
    name: str = '',
    poly_method: int = 3,
    brownfield_path: str | None = None,
    osm_layer_paths: dict | None = None,
) -> dict:
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
    url = _engine_url('/ftth/hld/run')

    with (
        Path(excel_path).open('rb') as ef,
        Path(roads_path).open('rb') as rf,
        contextlib.ExitStack() as stack,
    ):
        files = {
            'excel': (
                Path(excel_path).name,
                ef,
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            ),
            'roads': (Path(roads_path).name, rf, 'application/octet-stream'),
        }
        if brownfield_path:
            files['brownfield'] = (
                Path(brownfield_path).name,
                stack.enter_context(Path(brownfield_path).open('rb')),
                'application/octet-stream',
            )
        for key, path in (osm_layer_paths or {}).items():
            if path:
                files[key] = (
                    Path(path).name,
                    stack.enter_context(Path(path).open('rb')),
                    'application/octet-stream',
                )
        data = {'poly_method': str(poly_method)}
        if name:
            data['name'] = name
        if project_id:
            data['project_id'] = project_id

        resp = requests.post(url, files=files, data=data, timeout=120)

    if resp.status_code not in (200, 201, 202):
        detail = 'Unknown error'
        try:
            body = resp.json()
            detail = body.get('detail') or body.get('message') or str(body)
        except Exception:
            detail = resp.text[:500]
        msg = f'Engine returned {resp.status_code}: {detail}'
        raise RuntimeError(msg)

    result: dict = resp.json()

    # Cache the initial status locally
    _write_status(result)

    return result


class EngineError(RuntimeError):
    """An engine failure carrying the upstream status code and detail.

    The engine distinguishes "area not found" (404) from "the OSM services are
    unreachable" (502) from "too many premises for one run" (422).  Collapsing
    those into a single 502 would tell the user the wrong thing — one is a
    different area name, one is a retry — so the status travels with the
    exception and the view passes it straight through.
    """

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = detail


def _engine_detail(resp) -> str:
    try:
        body = resp.json()
    except Exception:
        return resp.text[:500] or 'Unknown engine error'
    if isinstance(body, dict):
        return body.get('detail') or body.get('message') or str(body)
    return str(body)


def _area_payload(
    area: str = '', country: str = '', city: str = '', postcode: str = '', area_name: str = ''
) -> dict:
    """Structured area inputs for the engine, with the empty parts omitted.

    The parts are forwarded as given rather than joined into a label here: the
    engine owns the composition, and two composers would eventually disagree
    about how "12105" plus "Berlin" becomes a search string.
    """
    payload: dict = {}
    for key, value in (
        ('area', area),
        ('country', country),
        ('city', city),
        ('postcode', postcode),
        ('area_name', area_name),
    ):
        text = str(value or '').strip()
        if text:
            payload[key] = text
    return payload


def resolve_area(
    area: str = '',
    boundary_only: bool = False,
    max_premises: int | None = None,
    timeout: int | None = None,
    country: str = '',
    city: str = '',
    postcode: str = '',
    area_name: str = '',
) -> dict:
    """Area -> boundary, and (unless ``boundary_only``) its premise counts.

    Resolving the boundary is one Nominatim call.  The counts additionally need
    the area's OSM data, which the engine fetches on first use — measured at
    ~2.5 minutes for a Berlin Ortsteil and **~16 minutes for Southampton**
    (56 km²), and instant once cached — hence the ``boundary_only`` split rather
    than holding the map render behind it.

    The timeout used to be a flat 900 s, so a cold city-sized area came back as
    a 502 with the boundary already drawn and nothing else: the page showed a
    map and no data.  The boundary call now starts the download, and the full
    call waits on that same download (the engine fetches each bbox once), so
    the wait is shared rather than doubled — but a large area can still exceed
    15 minutes, so the default is now 40.
    """
    if timeout is None:
        timeout = int(os.environ.get('FTTH_RESOLVE_TIMEOUT', '2400'))
    payload: dict = _area_payload(area, country, city, postcode, area_name)
    payload['boundary_only'] = bool(boundary_only)
    if max_premises:
        payload['max_premises'] = int(max_premises)
    try:
        resp = requests.post(_engine_url('/ftth/hld/resolve-area'), json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise EngineError(502, f'Engine unreachable: {exc}') from exc
    if resp.status_code != 200:
        raise EngineError(resp.status_code, _engine_detail(resp))
    return dict(resp.json())


def get_area_fetch(area: str = '', bbox: str = '') -> dict:
    """What the engine's OSM download for an area is doing, for a progress poll.

    Read-only and cheap: it never writes to the database, so the page can poll
    it every few seconds while a cold area downloads.
    """
    params = {}
    if area:
        params['area'] = area
    if bbox:
        params['bbox'] = bbox
    try:
        resp = requests.get(_engine_url('/ftth/hld/area-fetch'), params=params, timeout=30)
    except requests.RequestException as exc:
        logger.warning('Engine unreachable for area-fetch: %s', exc)
        return {'state': 'unavailable', 'label': 'Could not reach the engine', 'fetching': False}
    if resp.status_code != 200:
        return {'state': 'unavailable', 'label': 'Could not reach the engine', 'fetching': False}
    return dict(resp.json())


def get_input_layer(
    area: str,
    layer: str,
    country: str = '',
    city: str = '',
    postcode: str = '',
    area_name: str = '',
) -> dict:
    """Fetch one complete OSM/HLD input layer for the pre-run review map.

    The 300 s timeout here was another reason a cold area showed a boundary and
    nothing else: the first layer asked for waited on the same download the
    preview had kicked off, and was cut before it returned.  Layers now join the
    in-flight download rather than starting their own, so the wait is the one
    the area actually needs — up to the same 40 minutes the preview allows.
    """
    payload = _area_payload(area, country, city, postcode, area_name)
    payload['layer'] = layer
    try:
        resp = requests.post(
            _engine_url('/ftth/hld/input-layers'),
            json=payload,
            timeout=int(os.environ.get('FTTH_INPUT_LAYER_TIMEOUT', '2400')),
        )
    except requests.RequestException as exc:
        raise EngineError(502, f'Engine unreachable: {exc}') from exc
    if resp.status_code != 200:
        raise EngineError(resp.status_code, _engine_detail(resp))
    return dict(resp.json())


def run_from_area(
    area: str,
    project_id: str,
    name: str = '',
    poly_method: int = 3,
    country: str = '',
    city: str = '',
    postcode: str = '',
    area_name: str = '',
    brownfield_path: str | None = None,
) -> dict:
    """Start a full HLD run from an area.

    The engine generates the two input files from OSM and then runs the
    untouched pipeline on them, so the run is identical to a manual upload from
    the engine onwards.  Returns immediately (202) — input generation and the
    design both happen in the engine's background task.

    ``brownfield_path`` is optional: a ZIP (or single vector file) of existing
    infrastructure that the engine unzips and feeds to the pipeline's BF_*
    parameters, exactly as the manual-upload path does.  When present the
    request becomes multipart (the area fields travel as form fields);
    otherwise it stays JSON, so an existing JSON client is untouched.
    """
    payload: dict = _area_payload(area, country, city, postcode, area_name)
    payload['project_id'] = project_id
    payload['poly_method'] = int(poly_method)
    if name:
        payload['name'] = name
    try:
        if brownfield_path:
            with Path(brownfield_path).open('rb') as bf:
                files = {
                    'brownfield': (
                        Path(brownfield_path).name,
                        bf,
                        'application/octet-stream',
                    )
                }
                resp = requests.post(
                    _engine_url('/ftth/hld/run-from-area'),
                    data=payload,
                    files=files,
                    timeout=120,
                )
        else:
            resp = requests.post(_engine_url('/ftth/hld/run-from-area'), json=payload, timeout=120)
    except requests.RequestException as exc:
        raise EngineError(502, f'Engine unreachable: {exc}') from exc
    if resp.status_code not in (200, 201, 202):
        raise EngineError(resp.status_code, _engine_detail(resp))
    result: dict = resp.json()
    _write_status(result)
    return result


def osm_status() -> dict:
    """What the engine's local OSM store holds (reported, not required)."""
    try:
        resp = requests.get(_engine_url('/ftth/hld/osm-status'), timeout=15)
        if resp.status_code == 200:
            return dict(resp.json())
    except requests.RequestException as exc:
        logger.warning('Engine unreachable for osm-status: %s', exc)
    return {'loaded': False, 'unavailable': True}


def list_countries() -> list[dict]:
    """Country options for the area input's country dropdown.

    An engine outage must not empty the dropdown — the country a planner picks
    is what keeps a five-digit postcode from being read on the wrong continent —
    so a failure returns an empty list rather than raising.

    The timeout is 60s, not the 15s the neighbouring status calls use, because
    the list is static and cheap: the only thing this call ever waits for is a
    sleeping engine instance booting. A cold start measured 42s, so a 15s budget
    expired before the engine could answer and the dropdown came back empty on
    the first visit of the day.
    """
    try:
        resp = requests.get(_engine_url('/ftth/hld/countries'), timeout=60)
        if resp.status_code == 200:
            countries = list(resp.json().get('countries') or [])
            if not countries:
                logger.warning('Engine returned no country list (status 200)')
            return countries
        logger.warning('Engine answered countries with %s: %s', resp.status_code, _engine_detail(resp))
    except requests.RequestException as exc:
        logger.warning('Engine unreachable for countries: %s', exc)
    return []


def suggest_places(q: str, country: str = '', limit: int = 8) -> dict:
    """City/town suggestions for the area input's city combobox.

    Best-effort: an empty list is a normal answer while someone is typing, so a
    failure is reported as ``reason: unavailable`` instead of a 502 that would
    break the page they are still filling in.
    """
    params: dict = {'q': q, 'limit': int(limit)}
    if country:
        params['country'] = country
    try:
        resp = requests.get(_engine_url('/ftth/hld/places'), params=params, timeout=30)
        if resp.status_code == 200:
            return dict(resp.json())
        return {'query': q, 'places': [], 'reason': 'engine_error', 'detail': _engine_detail(resp)}
    except requests.RequestException as exc:
        logger.warning('Engine unreachable for places: %s', exc)
        return {'query': q, 'places': [], 'reason': 'unavailable'}


def get_status(project_id: str) -> dict:
    """
    Get the current pipeline status from the FastAPI engine.

    Falls back to the locally-cached status if the engine is unreachable
    (so the frontend still gets a response during brief network blips).
    """
    url = _engine_url(f'/ftth/hld/results/{project_id}')

    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data: dict = resp.json()
            _write_status(data)  # refresh local cache
            return data
    except requests.RequestException as exc:
        logger.warning('Engine unreachable for status %s: %s', project_id, exc)

    # Fallback: return locally-cached status
    cached = _read_status(project_id)
    if cached:
        return cached

    return {
        'project_id': project_id,
        'status': 'unknown',
        'messages': [],
        'layers': [],
        'downloads': [],
    }


def get_layer_geojson(project_id: str, layer_name: str) -> bytes | None:
    """
    Fetch a pipeline layer as raw GeoJSON bytes from the FastAPI engine.
    """
    url = _engine_url(f'/ftth/hld/results/{project_id}/layers/{layer_name}')

    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            return resp.content
    except requests.RequestException as exc:
        logger.warning('Engine unreachable for layer %s/%s: %s', project_id, layer_name, exc)

    # Fallback: try locally-cached GeoJSON
    from .config import LAYER_NAME_MAP

    entry = LAYER_NAME_MAP.get(layer_name.lower())
    if entry:
        stem = entry[0]
        host_geojson = HOST_OUTPUTS_DIR / project_id / f'{stem}.geojson'
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
    url = _engine_url(f'/ftth/hld/results/{project_id}/design')
    try:
        resp = requests.get(
            url,
            params={'layers': 'true' if include_layers else 'false'},
            timeout=180,
        )
        if resp.status_code != 200:
            logger.warning('Engine design payload %s -> HTTP %s', project_id, resp.status_code)
            return None
        data: dict = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning('Engine unreachable for design %s: %s', project_id, exc)
        return None

    layers = data.get('layers') or {}
    source_crs = data.get('crs') or DEFAULT_SOURCE_CRS
    for name, layer in layers.items():
        geojson = layer.get('geojson') if isinstance(layer, dict) else None
        if not isinstance(geojson, dict) or not geojson.get('features'):
            continue
        # _detect_crs reads the GeoJSON `crs` member; the designer's files do
        # not carry one, so stamp the CRS the engine reported.
        geojson.setdefault(
            'crs',
            {
                'type': 'name',
                'properties': {'name': source_crs},
            },
        )
        try:
            layer['geojson'] = json.loads(_reproject_geojson(json.dumps(geojson).encode('utf-8')))
        except Exception as exc:  # noqa: BLE001 — keep the run usable
            logger.warning('Design layer %s/%s not reprojected: %s', project_id, name, exc)
    if layers:
        data['map_crs'] = 'EPSG:4326'
    return data


def run_trench_design(project_id: str, force: bool = False) -> dict | None:
    """Start (or re-run) the trench designer for a project on the engine."""
    url = _engine_url(f'/ftth/hld/design/{project_id}')
    try:
        resp = requests.post(
            url,
            params={'force': 'true' if force else 'false'},
            timeout=60,
        )
        if resp.status_code in (200, 202):
            return dict(resp.json())
        logger.warning('Engine design run %s -> HTTP %s', project_id, resp.status_code)
    except requests.RequestException as exc:
        logger.warning('Engine unreachable to start design %s: %s', project_id, exc)
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
    from .models import FtthLayer, FtthProject

    ftth = FtthProject.objects.filter(pk=project_id).first()
    if ftth is None:
        return 0
    features = geojson_data.get('features', []) if isinstance(geojson_data, dict) else []
    # ── Assign human-readable feature_id: <layer>-001, <layer>-002, … ──
    for idx, feat in enumerate(features, start=1):
        props = feat.get('properties') or {}
        if not props.get('feature_id'):
            props['feature_id'] = '%s-%03d' % (layer_name, idx)
            feat['properties'] = props
    count = len(features)
    FtthLayer.objects.update_or_create(
        ftth_project=ftth,
        name=layer_name,
        defaults={'geojson': geojson_data, 'feature_count': count},
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
        features = fc.get('features', [])
        updated = False
        for idx, feat in enumerate(features, start=1):
            props = feat.get('properties') or {}
            if not props.get('feature_id'):
                props['feature_id'] = '%s-%03d' % (layer_obj.name, idx)
                feat['properties'] = props
                total_updated += 1
                updated = True
        if updated:
            layer_obj.geojson = fc
            layer_obj.save(update_fields=['geojson'])

    return total_updated


def sync_project_layers(project_id: str, layer_names=None, force: bool = False) -> dict:
    """Fetch every HLD output layer and persist it into ``FtthLayer`` rows.

    Idempotent — layers already in the DB are skipped so repeated polls stay
    cheap. If ``layer_names`` is omitted it is derived from the engine status.
    Returns ``{layer_name: feature_count}`` for the layers persisted.

    ``force`` re-persists a layer whose name is already stored. Skipping by
    NAME is right for a poll (a run gaining a layer must not rewrite thousands
    of features every few seconds) but wrong after a re-run of the same
    project: every name the project already had keeps the PREVIOUS design's
    geometry, and the map, the BOQ and the permit matrix are all read from
    those rows — a re-run that halved the ducts kept showing the old ones.
    See ``republish_project_layers`` for the forced path.
    """
    from .models import FtthLayer

    if layer_names is None:
        status_data = get_status(project_id)
        layer_names = [
            (item.get('name') or '').lower()
            for item in status_data.get('layers', [])
            if item.get('name')
        ]
    counts = {}
    for name in layer_names:
        if (
            not force
            and FtthLayer.objects.filter(ftth_project__project_id=project_id, name=name).exists()
        ):
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


def republish_project_layers(project_id: str, layer_names=None) -> dict:
    """Publish the engine's CURRENT layers over whatever the platform stored.

    For a run that finished after its publish — the pole and aerial stages of
    one project wrote two hours after theirs — every layer the project already
    had kept the previous design's geometry on the platform while PostGIS held
    the new one, so the results map, the BOQ and the permits disagreed with the
    engine. This is the forced, explicit counterpart of the polling sync:

    * every layer the engine reports is re-persisted, whatever is stored;
    * a layer the engine no longer reports is REMOVED when the engine owns it —
      a re-run that drops the couplers must not leave the previous design's
      couplers on the map, and a stored row is served as-is forever (the layer
      view only refreshes a row that is absent or empty);
    * layers the PLATFORM derives are left alone — `trench_sections` is built
      from the trenches by the post-HLD chain, which its own revision guard
      re-arms once the republished trenches land.

    Never call this from a poll: `_ingest_outputs` on the engine clears a
    project's GIS rows before reloading them, so mid-run the engine reports
    fewer layers than the project has, and a prune at that moment would delete
    rows that are about to be republished.

    Returns ``{'published': {name: features}, 'removed': [name, ...]}``.
    """
    from .models import FtthLayer

    status_data = get_status(project_id) or {}
    names = [
        (layer.get('name') or '').lower()
        for layer in status_data.get('layers') or []
        if layer.get('name')
    ]
    published = sync_project_layers(project_id, names or layer_names, force=True)

    engine_names = set(names if layer_names is None else layer_names)
    derived = _derived_layer_names()
    removed = []
    stored = FtthLayer.objects.filter(ftth_project__project_id=project_id).values_list(
        'name', flat=True
    )
    for name in stored:
        if name in engine_names or name in derived:
            continue
        FtthLayer.objects.filter(ftth_project__project_id=project_id, name=name).delete()
        removed.append(name)
    if removed:
        logger.info('Republished %s: removed stale layer(s) %s', project_id, ', '.join(removed))
    return {'published': published, 'removed': removed}


def _derived_layer_names() -> set:
    """Layer names the PLATFORM builds from the engine's layers.

    They have no engine counterpart, so a republish must not prune them: the
    post-HLD chain owns them and rebuilds them when their source changed.
    """
    from permits.analysis.trench_sections import SECTION_LAYER

    return {SECTION_LAYER}


def get_surface_ai_review(project_id: str) -> dict | None:
    """Read the separate surface-AI review report from the engine, if present."""
    url = _engine_url(f"/ftth/hld/results/{quote(str(project_id), safe='')}/surface-ai-review")
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            return dict(resp.json())
    except (requests.RequestException, ValueError) as exc:
        logger.warning('Could not load surface AI review for %s: %s', project_id, exc)
    return None


# A point classify fetches imagery and runs the vision model inside the engine,
# which bounds slow local inference itself (SURFACE_AI_OLLAMA_TIMEOUT). This
# timeout only guards transport, so it sits just above that bound.
SURFACE_CLASSIFY_TIMEOUT_SECONDS = 300


def classify_surface_at_point(
    project_id: str,
    coordinates,
    crs: str = 'EPSG:4326',
    length_m=None,
    bearing=None,
    span_id=None,
    coordinates_crs=None,
    include_imagery=False,
    claimed_surface=None,
    geometry_reason=None,
    geometry_confidence=None,
    known_share=None,
) -> dict:
    """Classify the surface at one clicked point or along one span (advisory only).

    ``coordinates`` is a point ``[x, y]`` for a map click, or a span's own route
    ``[[x, y], ...]`` for a per-span opt-in; the engine tells the two apart by
    shape, so a span caller only adds ``span_id`` and ``coordinates_crs``. Returns
    the engine's single review item. Only transport failures or a non-200 upstream
    status raise ``EngineError``; a refusal the engine chose to report (for
    example ``review_status: "error"`` because the vision model answered with
    unparseable JSON) is a normal 200 body.
    """
    url = _engine_url(
        f"/ftth/hld/results/{quote(str(project_id), safe='')}" "/surface-ai-review/classify"
    )
    payload = {
        'coordinates': coordinates,
        'crs': crs or 'EPSG:4326',
    }
    for key, value in (
        ('span_id', span_id),
        ('coordinates_crs', coordinates_crs),
        ('length_m', length_m),
        ('bearing', bearing),
        ('claimed_surface', claimed_surface),
        ('geometry_reason', geometry_reason),
        ('geometry_confidence', geometry_confidence),
        ('known_share', known_share),
    ):
        if value is not None:
            payload[key] = value
    if include_imagery:
        payload['include_imagery'] = True
    try:
        resp = requests.post(url, json=payload, timeout=SURFACE_CLASSIFY_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        logger.warning('Surface point classify failed for %s: %s', project_id, exc)
        raise EngineError(502, 'The engine could not complete the surface review.') from exc
    if resp.status_code != 200:
        raise EngineError(resp.status_code, _engine_detail(resp))
    try:
        return dict(resp.json())
    except ValueError as exc:
        raise EngineError(502, 'The engine returned an unreadable surface review.') from exc


# An imagery preview fetches one patch and never calls the model, so it is bounded
# by the imagery providers' own timeout plus a possible fallback retry.
SURFACE_IMAGERY_TIMEOUT_SECONDS = 120


def preview_surface_imagery(project_id: str, payload: dict) -> dict:
    """Fetch the imagery patch a surface detect would send, without the model.

    Imagery-only: it spends no vision-model quota, so the results page can show a
    reader exactly what the model would be given before they opt a span or a point
    into a detect. ``payload`` carries the same geometry as the classify route and
    is forwarded as-is.
    """
    url = _engine_url(
        f"/ftth/hld/results/{quote(str(project_id), safe='')}" "/surface-ai-review/imagery"
    )
    try:
        resp = requests.post(url, json=payload, timeout=SURFACE_IMAGERY_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        logger.warning('Surface imagery preview failed for %s: %s', project_id, exc)
        raise EngineError(502, 'The engine could not fetch the surface imagery.') from exc
    if resp.status_code != 200:
        raise EngineError(resp.status_code, _engine_detail(resp))
    try:
        return dict(resp.json())
    except ValueError as exc:
        raise EngineError(502, 'The engine returned an unreadable imagery preview.') from exc


def _db_cursor():
    """Open a cursor on the shared database.

    A one-line indirection on purpose: patching ``connection.cursor`` itself
    makes Django's own test teardown fail (it restores a wrapper it hung on the
    connection), so the DB-backed layer fallback is exercised by patching this
    instead.
    """
    from django.db import connection

    return connection.cursor()


def _db_layer_geojson(project_id: str, file_path: str) -> bytes | None:
    """
    Serve a layer file from the shared database when the engine cannot.

    Every run publishes its layers into the shared PostGIS database, which is
    the store the results map already reads — but the engine's own output
    **files** live on the engine's disk, and that is not the same machine that
    serves this API. A run executed elsewhere (or a container restarted without
    a persistent volume) leaves the project's map perfectly intact while every
    ``/ftth/hld/download/...`` returns 404, and both download packages come out
    all but empty — a layer the database holds must never silently vanish from
    a package.

    Returns GeoJSON bytes for a layer table that carries this project's rows,
    or ``None`` when the file is not a layer GeoJSON or the table is empty.
    Only stems named in :data:`LAYER_STEM_GIS_TABLES` are served, so no
    caller-supplied name ever reaches the SQL.
    """
    if not file_path.lower().endswith('.geojson'):
        return None
    stem = Path(file_path).stem
    table = LAYER_STEM_GIS_TABLES.get(stem)
    if not table:
        return None

    # A sub-layer of a merged table is served as itself, not as the whole table.
    where = 'project_id = %s AND geom IS NOT NULL'
    params = [project_id]
    sublayer = LAYER_STEM_SUBLAYER.get(stem)
    if sublayer:
        where += " AND properties->>'sublayer' = %s"
        params.append(sublayer)
    # Some stems differ by GEOMETRY rather than by a property (the brownfield
    # lines and its points share one table), so filter on the geometry type.
    # The tuple is a literal whitelist, never caller-supplied.
    geometry = LAYER_STEM_GEOMETRY.get(stem)
    if geometry:
        types = ', '.join(f"'{name}'" for name in geometry)
        where += f' AND GeometryType(geom) IN ({types})'

    try:
        with _db_cursor() as cur:
            cur.execute(
                f'SELECT ST_AsGeoJSON(geom), properties, ST_SRID(geom) '
                f'FROM gis."{table}" WHERE {where}',
                params,
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - a missing table must not fail the download
        logger.warning('DB layer fallback failed for %s/%s: %s', project_id, file_path, exc)
        return None

    srid = None
    features = []
    for geom_json, props, row_srid in rows:
        if not geom_json:
            continue
        if srid is None and row_srid:
            srid = int(row_srid)
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except (TypeError, ValueError):
                props = {}
        try:
            geometry = json.loads(geom_json)
        except (TypeError, ValueError):
            continue
        features.append(
            {
                'type': 'Feature',
                'geometry': geometry,
                'properties': dict(props or {}),
            }
        )

    if not features:
        return None

    # The `gis.*` geometry is stored in the project CRS, and this declares it so
    # `_reproject_geojson` converts it once (to WGS84) instead of assuming the
    # default and garbling the coordinates.
    payload = {
        'type': 'FeatureCollection',
        'crs': {'type': 'name', 'properties': {'name': f'EPSG:{srid or 4326}'}},
        'features': features,
    }
    logger.info(
        'Served %s/%s from gis.%s (%d features)',
        project_id,
        file_path,
        table,
        len(features),
    )
    return json.dumps(payload).encode('utf-8')


# How many output files a project's engine may 404 in a row before we stop
# asking it for the rest of the run.
#
# A run's files are either all on the engine's disk or none of them are -- the
# disk belongs to the machine that executed the pipeline. The engine still
# *lists* every file for a run it restored from the shared database row, so a
# package built on another host asks for each one and gets a 404 back. Measured
# 2026-10-13 against the live engine, project 24cd3573f4884a578c1b98a80bbd4040:
# 160 output files listed, 160 requests, 160 x 404, ~54s of pure dead time
# inside the survey assignment. One 404 is not proof (a run can legitimately
# lack an optional layer); three in a row is.
ENGINE_MISS_LIMIT = 3

# Seconds to wait before asking an engine about a project again after its files
# went missing there. The files can come back (the run is re-executed on this
# engine), so the verdict is explicitly temporary: it only spares us the dead
# round trips of a package build, it never decides correctness -- the host
# cache and the shared database still serve every layer they hold.
ENGINE_MISS_COOLDOWN = 600.0

# project_id -> (consecutive 404s, monotonic time of the last one)
_ENGINE_FILE_MISSES: dict[str, tuple[int, float]] = {}


def _engine_files_missing(project_id: str) -> bool:
    """True while this engine is known not to have the run's output files.

    Only once ENGINE_MISS_LIMIT files have 404'd in a row: one absent layer is
    a normal run, and writing the engine off for it would silently drop every
    file the engine *does* have from the next package.
    """
    misses = _ENGINE_FILE_MISSES.get(project_id)
    if not misses:
        return False
    count, seen_at = misses
    if count < ENGINE_MISS_LIMIT:
        return False
    if time.monotonic() - seen_at > ENGINE_MISS_COOLDOWN:
        _ENGINE_FILE_MISSES.pop(project_id, None)
        return False
    return True


def _note_engine_download(project_id: str, found: bool) -> None:
    """Track consecutive engine 404s so a package stops paying for them."""
    if found:
        _ENGINE_FILE_MISSES.pop(project_id, None)
        return
    count, _seen_at = _ENGINE_FILE_MISSES.get(project_id, (0, 0.0))
    count += 1
    _ENGINE_FILE_MISSES[project_id] = (count, time.monotonic())
    if count >= ENGINE_MISS_LIMIT:
        logger.info(
            'Engine has none of %s output files (%d consecutive 404s); '
            'serving the rest of this run from the host cache and the shared '
            'database for the next %ds.',
            project_id,
            count,
            int(ENGINE_MISS_COOLDOWN),
        )


def get_download_file(project_id: str, file_path: str) -> bytes | None:
    """
    Fetch one of a run's output files, from wherever it still exists.

    Order: the engine's disk (the canonical file), then this host's cache of
    the outputs, then the shared database -- which holds the layer itself even
    when the engine's disk (or this host's cache) does not. A GeoPackage is
    rebuilt from those rows when the database is the only source (`ftth_hld.gpkg`),
    because a package that carries no GeoPackage is not the package the
    contract promises.
    """
    if not file_path or '\\' in file_path or '..' in Path(file_path).parts:
        return None
    clean_path = Path(file_path)
    if clean_path.is_absolute():
        return None
    safe_path = clean_path.as_posix()

    if not _engine_files_missing(project_id):
        url = _engine_url(f"/ftth/hld/download/{project_id}/{quote(safe_path, safe='/')}")
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 200:
                _note_engine_download(project_id, True)
                return resp.content
            _note_engine_download(project_id, False)
        except requests.RequestException as exc:
            logger.warning('Engine unreachable for download %s/%s: %s', project_id, file_path, exc)

    # Fallback: try locally-cached file
    host_file = HOST_OUTPUTS_DIR / project_id / safe_path
    if host_file.exists() and host_file.is_file():
        return host_file.read_bytes()

    # Last resort: the shared database, which holds the layer itself even when
    # the engine's disk (or this host's cache) does not.
    if safe_path.lower().endswith('.gpkg'):
        # A GeoPackage is a container, and the rows behind it are still in the
        # database — measured 2026-10-19 on the deployed engine: every `.gpkg`
        # of every live run answers 404 while its GeoJSON is rebuildable. Write
        # the container from those rows rather than shipping a package with no
        # GeoPackage at all (see `ftth_hld.gpkg`).
        stem = Path(safe_path).stem
        geojson = _db_layer_geojson(project_id, f'{stem}.geojson')
        if geojson is None:
            return None
        try:
            from .gpkg import geojson_to_gpkg

            return geojson_to_gpkg(geojson, stem)
        except Exception as exc:  # noqa: BLE001 - a failed rebuild must not fail the package
            logger.warning(
                'Could not rebuild %s for %s from the database: %s',
                safe_path,
                project_id,
                exc,
            )
            return None
    return _db_layer_geojson(project_id, safe_path)


# ---------------------------------------------------------------------------
# CRS reprojection helpers
# ---------------------------------------------------------------------------

# Regex to extract the EPSG code from a GeoJSON ``crs`` field like
# ``urn:ogc:def:crs:EPSG::25833`` or ``EPSG:25833``.
_EPSG_RE = re.compile(r'EPSG(?::|::|/)(\d+)', re.IGNORECASE)


def _detect_crs(geojson: dict) -> str:
    """
    Detect the source CRS from a GeoJSON object's ``crs`` field.
    Falls back to ``DEFAULT_SOURCE_CRS`` if not present.

    The HLD engine already exports its GeoJSON layers in WGS84 using the
    OGC CRS84 URN (``urn:ogc:def:crs:OGC:1.3:CRS84``). That is the same
    lon/lat datum as EPSG:4326, so it is normalized to ``EPSG:4326`` here
    to avoid a double-reprojection that would garble every coordinate.
    """
    crs = geojson.get('crs')
    if crs and isinstance(crs, dict):
        name = str(crs.get('properties', {}).get('name', ''))
        upper = name.upper()
        # OGC CRS84 / WGS84 / EPSG:4326 are all already lon/lat WGS84.
        if 'CRS84' in upper or 'WGS84' in upper or 'EPSG:4326' in upper:
            return 'EPSG:4326'
        match = _EPSG_RE.search(name)
        if match:
            return f'EPSG:{match.group(1)}'
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
            'pyproj is not installed — GeoJSON will be bundled '
            'WITHOUT reprojection. Coordinates may be wrong. '
            'Install with: pip install pyproj'
        )
        return geojson_bytes

    try:
        data = json.loads(geojson_bytes)
    except json.JSONDecodeError:
        logger.exception('Invalid GeoJSON for reprojection')
        return geojson_bytes  # pass through unchanged

    source_crs = _detect_crs(data)

    # If already WGS84, no reprojection needed
    if source_crs.upper() in ('EPSG:4326', 'WGS84'):
        return geojson_bytes

    transformer = Transformer.from_crs(source_crs, 'EPSG:4326', always_xy=True)
    features = data.get('features', [])
    reprojected = 0

    for feature in features:
        geom = feature.get('geometry')
        if not geom:
            continue
        _reproject_geometry(geom, transformer)
        reprojected += 1

    # Update / remove the CRS field — WGS84 is the GeoJSON default
    data.pop('crs', None)

    logger.info(
        'Reprojected %d features from %s → EPSG:4326',
        reprojected,
        source_crs,
    )
    return json.dumps(data, ensure_ascii=False).encode('utf-8')


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
    gtype = geom.get('type')
    coords = geom.get('coordinates')
    if coords is None:
        return

    if gtype == 'Point':
        geom['coordinates'] = _reproject_coord(coords, transformer)
    elif gtype in ('MultiPoint', 'LineString'):
        geom['coordinates'] = [_reproject_coord(c, transformer) for c in coords]
    elif gtype in ('MultiLineString', 'Polygon'):
        geom['coordinates'] = [[_reproject_coord(c, transformer) for c in ring] for ring in coords]
    elif gtype == 'MultiPolygon':
        geom['coordinates'] = [
            [[_reproject_coord(c, transformer) for c in ring] for ring in poly] for poly in coords
        ]
    elif gtype == 'GeometryCollection':
        for sub in geom.get('geometries', []):
            _reproject_geometry(sub, transformer)


def _engine_deliverable_names(project_id: str, status: dict | None = None) -> list:
    """
    Filenames the engine reports for a project, minus the raw source inputs.

    Both packages have to contain **every layer the run produced**, and a
    curated list alone silently drops a layer the pipeline gained after    the list was written (``Trench_Nodes``, ``Poles``, the aerial pair). The
    engine's own ``downloads`` is the single source of truth for what exists,
    so both packages are driven from it and the curated lists are only the
    fallback for an engine that cannot be reached.

    The status is read through ``get_status`` (which ASKS the engine) and not
    ``_read_status``: the latter is the disk cache of the last poll and nothing
    invalidates it, so a package built after a run finished late was assembled
    from the list that predated its last layers — one project's aerial chain
    and poles were missing from both packages while their files sat on disk.
    """
    status = status if status is not None else (get_status(project_id) or {})
    names = []
    for dl in status.get('downloads') or []:
        n = dl.get('name') if isinstance(dl, dict) else str(dl)
        if not n or '/' in n or '\\' in n:
            # Raw source inputs (``inputs/...``, ``brownfield/...``) are not
            # deliverables -- and neither is anything else in a subdirectory.
            # The engine also lists the trench designer's scratch runs
            # (``design_all/``, ``design_chain/``, ``design_crossings/``,
            # ``design_defaults/``, ``design_score/``, ``design_score_compare/``
            # and ``brownfield_readback_check/``), each holding its own copy of
            # ``Aerial_Drops`` and ``Final_Trenches``. Bundling them put SIX
            # duplicate copies of those two layers into the survey package --
            # 2,640 of its 4,804 Feature rows -- under exactly the same layer
            # names as the real ones, so the field engineer received the same
            # trench layer six times over. Each copy also cost an engine round
            # trip, every one of them a 404 whenever the run's files are not on
            # this engine's disk. A deliverable is a file in the run's output
            # root: every name in SURVEY_PACKAGE_FILES / DESIGN_PACKAGE_FILES
            # and every key in the two GeoJSON maps is a bare filename.
            continue
        if n.lower().endswith('.geojson') and (
            n in DESIGN_GEOJSON_FILES or n in SURVEY_GEOJSON_FILES
        ):
            # Known layers are added reprojected (WGS84) via the map below;
            # keep any brand-new geojson raw so nothing is lost.
            continue
        names.append(n)

    # Deduplicate while preserving order
    seen = set()
    ordered = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


_LAYER_FILE_EXTS = ('.gpkg', '.geojson')


def _displayed_layer_stems(status: dict | None) -> set[str] | None:
    """File stems behind the layers the results map displays, or None.

    The names are the engine's published ``layers`` list — the same list the
    results map draws — mapped through ``DISPLAYED_LAYER_FILES``. ``None``
    means the run did not say what it publishes (or publishes something this
    map has no rule for), and the caller must then leave the package alone:
    stripping only the names it recognises would silently drop a brand-new
    layer instead of packaging it.

    ``FIELD_SURVEY_EXCLUDED_LAYERS`` is checked FIRST. The name has no rule in
    this map, so before this it landed in the ``unknown`` bucket, the function
    returned ``None`` and the caller kept the package whole — with the artefact
    inside. It is not an unknown layer, it is a known one the field side never
    receives, and a run (or a re-published run) that names it must not be able
    to bring it back that way.
    """
    stems: set[str] = set()
    unknown: list[str] = []
    for layer in (status or {}).get('layers') or []:
        name = layer.get('name') if isinstance(layer, dict) else layer
        if not name:
            continue
        key = str(name).lower()
        if key in FIELD_SURVEY_EXCLUDED_LAYERS:
            continue
        files = DISPLAYED_LAYER_FILES.get(key)
        if files is None:
            unknown.append(str(name))
            continue
        stems.update(stem.lower() for stem in files)
    if unknown:
        logger.info(
            'Published layer(s) with no package mapping: %s; keeping the package as built.',
            ', '.join(sorted(unknown)[:8]),
        )
        return None
    return stems or None


def _restrict_to_displayed(filenames, stems: set[str] | None, project_id: str, label: str) -> list:
    """Drop layer files that back no layer the results map displays.

    A run writes intermediates the platform never publishes (the per-tier
    trench files, Pseudo_HH, Served_Premises, Tangent_Crossings, Trench_Nodes).
    They were bundled because the engine's ``downloads`` list mentions them, so
    a package carried layers HLD Outputs does not show. Documents (``.json``,
    ``.xlsx``) are not layers and stay.

    ``FIELD_SURVEY_EXCLUDED_LAYERS`` is dropped unconditionally, even when
    ``stems`` is None: those files are never a deliverable, whatever the run
    publishes and however little it says. Everything else follows ``stems``, so
    a brand-new layer still survives an unknown list.
    """
    names = list(filenames)
    kept: list = []
    dropped: list[str] = []
    for name in names:
        if name.lower().endswith(_LAYER_FILE_EXTS):
            stem = Path(name).stem.lower()
            if stem in FIELD_SURVEY_EXCLUDED_LAYERS or (stems is not None and stem not in stems):
                dropped.append(name)
                continue
        kept.append(name)
    if dropped:
        logger.info(
            '%s package for %s: dropped %d layer file(s) the results map does not display (%s)',
            label,
            project_id,
            len(dropped),
            ', '.join(dropped[:8]),
        )
    return kept


def _restrict_map_to_displayed(mapping: dict, stems: set[str] | None) -> dict:
    """The GeoJSON map, limited to the displayed layers (when known).

    The excluded stems are dropped even when ``stems`` is None, so a map entry
    for one of them (none today — the two maps are curated — but the rule is
    what keeps that true) cannot slip through on an unknown layer list.
    """
    return {
        k: v
        for k, v in mapping.items()
        if Path(k).stem.lower() not in FIELD_SURVEY_EXCLUDED_LAYERS
        and (stems is None or Path(k).stem.lower() in stems)
    }


def _package_stems(status: dict | None) -> set[str] | None:
    """``_displayed_layer_stems`` plus the layers the field survey always gets.

    ``Objects`` is the premises layer the whole exercise is keyed on, so it is
    added back even when a run's published list omitted it. ``None`` (no usable
    list) stays ``None`` — the caller must keep the package whole rather than
    strip the layers it cannot classify.
    """
    stems = _displayed_layer_stems(status)
    if stems is None:
        return None
    return stems | set(FIELD_SURVEY_ALWAYS_PACKAGED_LAYERS)


# The entry every package carries, naming what it could not bundle. Not a
# layer: readers select by extension (`discover_zip_layers` takes `.geojson`).
PACKAGE_REPORT_NAME = 'PACKAGE_REPORT.txt'


def _package_report_text(
    label: str,
    project_id: str,
    bundled: list,
    missing: list,
    substituted: list,
) -> str:
    """The human-readable half of the package report (the other half is the
    ``report`` dict and the WARNING log line)."""
    lines = [
        f'{label} package — run {project_id}',
        '',
        f'bundled: {len(bundled)} file(s)',
        f'missing: {len(missing)} file(s)',
        f'substituted: {len(substituted)} file(s)',
        '',
    ]
    if missing:
        lines.append('These files are part of the package contract and could not be fetched:')
        lines.extend(f'  {item["name"]} — {item["reason"]}' for item in missing)
        lines.append('')
    if substituted:
        lines.append('These files are in the package under a different name:')
        lines.extend(f'  {item["name"]} — {item["reason"]}' for item in substituted)
        lines.append('')
    if missing or substituted:
        lines.extend(
            [
                'THIS PACKAGE IS INCOMPLETE. It carries the files listed above, ' 'and no others.',
                "A run's output files live on the engine that executed it; when "
                'that disk is gone, a',
                'layer can still be served as GeoJSON from the shared database, '
                'which a GeoPackage cannot.',
                'Re-run the pipeline to publish the missing files again.',
            ]
        )
    else:
        lines.append('Every file in the package contract is present.')
    return '\n'.join(lines) + '\n'


def _build_package_zip(
    project_id: str,
    gpkg_files: list,
    geojson_map: dict,
    label: str,
    extra_files: dict[str, bytes] | None = None,
    report: dict | None = None,
) -> bytes:
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

    Whatever could not be fetched is **reported, never dropped in silence**:
    the ZIP always carries a ``PACKAGE_REPORT.txt`` naming every contract file
    that is absent (and every one substituted), ``report`` is filled in when
    the caller wants the same facts in the response, and the missing names are
    logged at WARNING. Measured on the live service 2026-10-14: a run whose
    files are no longer on the engine's disk produced a 150 KB "survey" ZIP
    holding 16 GeoJSON files and **not one of its 17 GeoPackages**, and nothing
    in the download, the response or the log said so.
    """
    # A gap inside one package must not poison the next one. The miss counter
    # is CONSECUTIVE 404s and the GeoJSON pass runs last, so a build whose only
    # gaps are its final few layers ended with the engine written off -- and
    # the next build then skipped the engine from its first GeoPackage.
    # Measured 2026-10-18 against the live engine: a run missing 3 GeoJSONs
    # produced a 50-file design package, and the request right after it (same
    # files on disk, same engine) produced 19 files with no GeoPackage at all.
    # Strikes are kept inside one build, where they save the round trips they
    # were meant to; the next build asks again.
    _ENGINE_FILE_MISSES.pop(project_id, None)

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        files_added = 0
        extra_files = extra_files or {}
        # A ZIP must not carry the same name twice: a layer served as both a
        # GeoPackage and a GeoJSON can reach this list twice (the plain file
        # pass, and the GeoJSON fallback below when the GeoJSON is absent).
        # Duplicate entries make some unzippers overwrite and others refuse.
        written: set = set()
        bundled: list = []
        missing: list = []
        substituted: list = []

        def _write(name: str, payload: bytes) -> None:
            nonlocal files_added
            if name in written:
                return
            written.add(name)
            zf.writestr(name, payload)
            files_added += 1
            bundled.append(name)

        def _note_missing(name: str, reason: str) -> None:
            missing.append({'name': name, 'reason': reason})

        # 1. Original GPKG / document files (skipped when an override exists)
        for fname in gpkg_files:
            if fname in extra_files:
                continue
            data = get_download_file(project_id, fname)
            if data is None:
                _note_missing(
                    fname,
                    'not on the engine, not in the output cache, and no database '
                    'fallback exists for a GeoPackage',
                )
                continue
            _write(fname, data)

        # 2. GeoJSON versions (reprojected to WGS84)
        for engine_fname, zip_fname in geojson_map.items():
            raw = get_download_file(project_id, engine_fname)
            if raw is None:
                # The layer can exist as a GeoPackage with no GeoJSON beside it
                # (an intermediate the results map does not publish, or a
                # project built before the engine exported one for every
                # layer). Bundle the GeoPackage rather than dropping the layer
                # from the package — an absent layer is the one failure a
                # reader cannot work around. `_write` skips it when the plain
                # file pass already bundled that GeoPackage.
                gpkg_fname = engine_fname.rsplit('.', 1)[0] + '.gpkg'
                gpkg = get_download_file(project_id, gpkg_fname)
                if gpkg is not None:
                    _write(gpkg_fname, gpkg)
                    substituted.append(
                        {
                            'name': zip_fname,
                            'reason': f'{engine_fname} was unavailable; {gpkg_fname} '
                            'was bundled in its place',
                        }
                    )
                    continue
                _note_missing(
                    zip_fname,
                    f'neither {engine_fname} nor {gpkg_fname} could be fetched',
                )
                continue
            _write(zip_fname, _reproject_geojson(raw))

        # 3. Extra generated documents (override engine files of same name)
        for zip_fname, data in extra_files.items():
            _write(zip_fname, data)

        # 4. The report travels with the package.
        # A reader cannot otherwise tell a complete package from a partial one:
        # the ZIP is valid either way, and the download authenticates as
        # successful either way. `.txt` is not a layer — `discover_zip_layers`
        # and every other reader skip anything that is not `.geojson`.
        report_text = _package_report_text(label, project_id, bundled, missing, substituted)
        written.add(PACKAGE_REPORT_NAME)
        zf.writestr(PACKAGE_REPORT_NAME, report_text.encode('utf-8'))

    zip_buffer.seek(0)

    # The failure is "nothing at all could be bundled" — the report entry is
    # not a deliverable, so it does not count here.
    if files_added == 0:
        msg = (
            f"No {label} files found for project '{project_id}'. "
            'The pipeline may still be running.'
        )
        raise FileNotFoundError(msg)

    if missing or substituted:
        logger.warning(
            '%s package for %s is INCOMPLETE: %d of %d file(s) missing (%s), ' '%d substituted',
            label,
            project_id,
            len(missing),
            len(missing) + len(bundled),
            ', '.join(item['name'] for item in missing[:8]) or '-',
            len(substituted),
        )
    else:
        logger.info('%s package for %s is complete: %d file(s)', label, project_id, len(bundled))

    if report is not None:
        report.update(
            {
                'label': label,
                'project_id': project_id,
                'bundled': sorted(bundled),
                'missing': missing,
                'substituted': substituted,
                'complete': not missing and not substituted,
            }
        )

    return zip_buffer.getvalue()


def _build_package_from_copy(project_id: str, report: dict | None = None) -> bytes | None:
    """Rebuild the field package from the Survey copy imported from this run.

    The engine's disk is not durable, and the shared `gis.*` rows can be gone
    as well — measured 2026-10-19 on run `24cd3573…`: the engine answers 404 for
    every file and every `gis` table has **0** rows for it, while its Survey
    copy is active with 2,800 features. The copy is the field dataset and it
    was imported FROM this run's package, so it is a faithful source for that
    package: every served layer is there, already in WGS84.

    Returns ``None`` when there is no copy or it holds no servable layer, so
    the caller keeps the original "nothing found" error.
    """
    from projects.layer_rules import is_field_survey_layer, normalize_layer_id
    from projects.models import Feature, ImportSession

    session = (
        ImportSession.objects.filter(
            original_filename=f'{project_id}_survey_package.zip',
        )
        .select_related('project')
        .first()
    )
    if session is None:
        return None

    grouped: dict[str, list[dict]] = {}
    rows = (
        Feature.objects.filter(project=session.project)
        .exclude(geometry=None)
        .order_by('created_at')
        .values('layer_id', 'layer_name', 'geometry', 'properties')
    )
    for row in rows.iterator():
        layer_id = normalize_layer_id(row['layer_id'] or row['layer_name'] or '')
        if not layer_id or not is_field_survey_layer(layer_id):
            continue
        geometry = row['geometry']
        if not isinstance(geometry, dict):
            continue
        properties = row['properties'] if isinstance(row['properties'], dict) else {}
        grouped.setdefault(layer_id, []).append(
            {'type': 'Feature', 'geometry': geometry, 'properties': properties}
        )
    if not grouped:
        return None

    from .gpkg import geojson_to_gpkg

    bundled: list[str] = []
    substituted: list[dict] = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        written: set[str] = set()

        def _write(name: str, payload: bytes) -> None:
            if name in written:
                return
            written.add(name)
            archive.writestr(name, payload)
            bundled.append(name)

        for engine_name, zip_name in SURVEY_GEOJSON_FILES.items():
            features = grouped.get(normalize_layer_id(Path(zip_name).stem))
            if not features:
                continue
            # Already WGS84: the copy was imported from this package's
            # reprojected GeoJSON, so it must not be reprojected again.
            raw = json.dumps({'type': 'FeatureCollection', 'features': features}).encode()
            _write(zip_name, raw)
            gpkg_name = Path(engine_name).stem + '.gpkg'
            try:
                _write(gpkg_name, geojson_to_gpkg(raw, Path(engine_name).stem))
            except Exception as exc:  # noqa: BLE001 - one layer must not empty the package
                logger.warning(
                    'Could not write %s for %s from its Survey copy: %s',
                    gpkg_name,
                    project_id,
                    exc,
                )
                substituted.append(
                    {
                        'name': gpkg_name,
                        'reason': f'could not be written from the copy: {exc}',
                    }
                )

        if not bundled:
            return None

        report_text = _package_report_text(
            'survey (rebuilt from the Survey copy)',
            project_id,
            sorted(bundled),
            [],
            substituted,
        )
        archive.writestr(PACKAGE_REPORT_NAME, report_text.encode('utf-8'))

    if report is not None:
        report.update(
            {
                'label': 'survey',
                'project_id': project_id,
                'bundled': sorted(bundled),
                'missing': [],
                'substituted': substituted,
                'complete': not substituted,
                'source': f'survey copy {session.project_id}',
            }
        )

    return buffer.getvalue()


def generate_survey_package(project_id: str, report: dict | None = None) -> bytes:
    """
    Generate the **field-survey package** zip — the compact subset a
    surveyor needs on site: polygons, PDPs, MFG and its service areas,
    cables, chambers, final trenches, ducts (incl. drop ducts) and existing
    brownfield infrastructure.

    Includes GPKG files plus GeoJSON versions **reprojected to
    EPSG:4326 (WGS84)** so the mobile app can render them on MapLibre
    without needing a GPKG reader or client-side reprojection.

    Only the files that back a layer the results map displays are bundled:
    the run's intermediates (Pseudo_HH, Served_Premises, Tangent_Crossings,
    Trench_Nodes, the per-tier trench publications) are not layers the
    platform shows, so they are not layers the crew receives.

    Returns the raw zip bytes.
    """
    # Driven by the engine's own layer list so a layer the run produced is
    # never missing from the pack (the curated list is the offline fallback).
    status = get_status(project_id) or {}
    ordered = _engine_deliverable_names(project_id, status) or list(SURVEY_PACKAGE_FILES)
    stems = _package_stems(status)
    try:
        return _build_package_zip(
            project_id,
            _restrict_to_displayed(ordered, stems, project_id, 'survey'),
            _restrict_map_to_displayed(SURVEY_GEOJSON_FILES, stems),
            'survey',
            report=report,
        )
    except FileNotFoundError:
        # The run's files are gone from the engine AND from the shared `gis`
        # tables. Its Survey copy still holds every layer those files carried,
        # so the package can be rebuilt from the copy instead of 404-ing a
        # crew out of its own project (project `24cd3573…`, 2026-10-19).
        rebuilt = _build_package_from_copy(project_id, report)
        if rebuilt is None:
            raise
        logger.warning(
            'Survey package for %s rebuilt from its Survey copy: the engine and '
            'the shared gis rows have no files for this run.',
            project_id,
        )
        return rebuilt


def generate_design_package(project_id: str, report: dict | None = None) -> bytes:
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
    from the deliverable, and so is any file that backs no layer the
    results map displays (see ``DISPLAYED_LAYER_FILES``).

    Returns the raw zip bytes.
    """
    # `get_status`, not the disk cache — see _engine_deliverable_names.
    status = get_status(project_id) or {}
    downloads = status.get('downloads') or []

    names = []
    for dl in downloads:
        n = dl.get('name') if isinstance(dl, dict) else str(dl)
        if not n or '/' in n or '\\' in n:
            # Raw source inputs AND the trench designer's per-variant scratch
            # runs -- see _engine_deliverable_names for the full reasoning.
            continue
        if n.lower().endswith('.geojson'):
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
    stems = _package_stems(status)
    ordered = _restrict_to_displayed(ordered, stems, project_id, 'design')
    # Inject the database-computed BOQ/BOM over the engine's hardcoded
    # template files (see ftth_hld/boq.py — quantities come from the
    # persisted HLD layers, priced by the BoqRate card).
    extra_files = {}
    try:
        from .boq import render_boq_xlsx

        extra_files['BOQ.xlsx'] = render_boq_xlsx(project_id, sheets='boq')
        extra_files['BOM.xlsx'] = render_boq_xlsx(project_id, sheets='bom')
    except Exception as exc:
        logger.warning('Could not generate BOQ/BOM for design package: %s', exc)

    return _build_package_zip(
        project_id,
        ordered,
        _restrict_map_to_displayed(DESIGN_GEOJSON_FILES, stems),
        'design',
        extra_files=extra_files,
        report=report,
    )


def delete_project(project_id: str) -> dict:
    """
    Delete a project from the FastAPI engine (disk + PostGIS).

    Returns the engine's response.
    """
    url = _engine_url(f'/ftth/hld/projects/{project_id}')

    try:
        resp = requests.delete(url, timeout=30)
    except requests.RequestException as exc:
        return {'deleted': False, 'detail': f'Engine unreachable: {exc}'}
    else:
        if resp.status_code == 200:
            return dict(resp.json())
        detail = 'Unknown error'
        try:
            body = resp.json()
            detail = body.get('detail') or body.get('message') or str(body)
        except Exception:
            detail = resp.text[:500]
        return {'deleted': False, 'detail': f'Engine returned {resp.status_code}: {detail}'}


def list_projects() -> list[dict]:
    """List recent pipeline runs from the FastAPI engine."""
    url = _engine_url('/ftth/projects')
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return list(resp.json())
    except requests.RequestException as exc:
        logger.warning('Engine unreachable for project list: %s', exc)
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
            pid = ep.get('project_id')
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

    # The engine's project list carries no layer inventory -- it only knows the
    # runs still in its memory -- so taking ``layers`` from it alone left the
    # HLD Outputs Layers column showing "—" on every row, including completed
    # runs whose 14 layers are persisted (measured 2026-10-18: the list returned
    # ``layers: []`` while /results/<id>/ returned 14 layers / 22,888 features
    # for the same run). Read the persisted rows as the fallback: they are what
    # the results map already draws from, so the list cannot contradict it.
    projects = list(FtthProject.objects.all().order_by('-updated_at', '-created_at')[:limit])

    from .models import FtthLayer

    db_layers: dict[str, list] = {}
    try:
        for row in FtthLayer.objects.filter(
            ftth_project__project_id__in=[p.project_id for p in projects]
        ).values('ftth_project__project_id', 'name', 'feature_count'):
            db_layers.setdefault(row['ftth_project__project_id'], []).append(
                {'name': row['name'], 'count': row['feature_count']}
            )
    except Exception:  # pragma: no cover - a list must not fail on enrichment
        db_layers = {}

    data = []
    # Most-recently-active first: updated_at moves on every re-run/poll,
    # so a re-run of an old project surfaces at the top instead of its
    # original creation-date position (fall back to created_at when equal).
    for p in projects:
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
                ).select_related('assignee')
                for job in eng_rows:
                    assigned_engineers.append(
                        {
                            'id': str(job.assignee.id),
                            'email': job.assignee.email,
                            'full_name': job.assignee.full_name,
                        }
                    )
            except Exception:
                pass
        data.append(
            {
                'project_id': p.project_id,
                'name': p.name,
                'status': enriched.get('status', p.status),
                'progress': enriched.get('progress', p.progress),
                'stage_name': enriched.get('stage_name', p.stage_name),
                'excel_filename': p.excel_filename,
                'roads_filename': p.roads_filename,
                'created_at': p.created_at.isoformat(),
                'updated_at': p.updated_at.isoformat(),
                'downloads': enriched.get('downloads', []),
                'layers': enriched.get('layers') or db_layers.get(p.project_id, []),
                'assigned_engineer': (
                    {
                        'id': str(engineer.id),
                        'email': engineer.email,
                        'full_name': engineer.full_name,
                    }
                    if engineer
                    else None
                ),
                'assigned_engineers': assigned_engineers,
                'assigned_at': p.assigned_at.isoformat() if p.assigned_at else None,
                'survey_copy_project_id': str(copy.id) if copy else None,
                'survey_status': copy.status if copy else None,
            }
        )
    return data
