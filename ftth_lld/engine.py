"""LLD engine proxy.

Proxies the LLD pipeline operations to the FTTH FastAPI engine (the single
service that orchestrates ``qgis_process``). Separated from the HLD pipeline
proxy so the LLD stage keeps its own, self-contained engine surface.
"""

import logging

import requests

from ftth_hld.config import FTTH_ENGINE_URL

logger = logging.getLogger(__name__)

_ENGINE = FTTH_ENGINE_URL


def _engine_url(path: str) -> str:
    """Build an absolute URL for the FastAPI engine."""
    return f'{_ENGINE}{path}'


def lld_run(project_id: str, lld_version: str, dataset: dict) -> dict:
    """Submit an LLD run to the engine. Returns the engine's task dict."""
    url = _engine_url('/ftth/lld/run')
    resp = requests.post(
        url,
        json={'project_id': project_id, 'lld_version': lld_version, 'dataset': dataset},
        timeout=30,
    )
    if resp.status_code not in (200, 201, 202):
        detail = 'Unknown error'
        try:
            body = resp.json()
            detail = body.get('detail') or str(body)
        except Exception:
            detail = resp.text[:500]
        raise RuntimeError(f'LLD engine returned {resp.status_code}: {detail}')
    return resp.json()


def lld_replan(project_id: str, lld_version: str, dataset: dict) -> dict:
    """Submit a Mode B (full re-plan) LLD run to the engine.

    Same request shape as ``lld_run`` — the engine re-runs the HLD oneclick
    pipeline with the approved survey dataset as brownfield input.
    """
    url = _engine_url('/ftth/lld/replan')
    resp = requests.post(
        url,
        json={'project_id': project_id, 'lld_version': lld_version, 'dataset': dataset},
        timeout=30,
    )
    if resp.status_code not in (200, 201, 202):
        detail = 'Unknown error'
        try:
            body = resp.json()
            detail = body.get('detail') or str(body)
        except Exception:
            detail = resp.text[:500]
        raise RuntimeError(f'LLD replan engine returned {resp.status_code}: {detail}')
    return resp.json()


def lld_status(project_id: str, lld_version: str) -> dict | None:
    """Poll the engine for an LLD run's status. Returns None if unreachable."""
    url = _engine_url(f'/ftth/lld/results/{project_id}/{lld_version}')
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException as exc:
        logger.warning('Engine unreachable for LLD status %s/%s: %s', project_id, lld_version, exc)
    return None


def lld_layer_geojson(project_id: str, lld_version: str, layer: str) -> bytes | None:
    """Fetch one LLD output layer as raw GeoJSON bytes from the engine."""
    url = _engine_url(f'/ftth/lld/results/{project_id}/{lld_version}/layers/{layer}')
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            return resp.content
    except requests.RequestException as exc:
        logger.warning(
            'Engine unreachable for LLD layer %s/%s/%s: %s', project_id, lld_version, layer, exc
        )
    return None


def lld_download_zip(project_id: str, lld_version: str) -> bytes | None:
    """Download the LLD output zip from the engine."""
    url = _engine_url(f'/ftth/lld/download/{project_id}/{lld_version}')
    try:
        resp = requests.get(url, timeout=60)
        if resp.status_code == 200:
            return resp.content
    except requests.RequestException as exc:
        logger.warning(
            'Engine unreachable for LLD download %s/%s: %s', project_id, lld_version, exc
        )
    return None
