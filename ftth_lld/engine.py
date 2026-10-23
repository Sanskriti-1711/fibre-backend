"""LLD engine proxy.

Proxies the LLD pipeline operations to the FTTH FastAPI engine (the single
service that orchestrates ``qgis_process``). Separated from the HLD pipeline
proxy so the LLD stage keeps its own, self-contained engine surface.
"""

import email.utils
import logging
import os
import time
from datetime import UTC, datetime

import requests

from ftth_hld.config import FTTH_ENGINE_URL, engine_write_headers
from ftth_hld.pipeline import ensure_engine_awake

logger = logging.getLogger(__name__)

_ENGINE = FTTH_ENGINE_URL


def _engine_url(path: str) -> str:
    """Build an absolute URL for the FastAPI engine."""
    return f'{_ENGINE}{path}'


# ---------------------------------------------------------------------------
# A sleeping engine is a wait, not a failed run
# ---------------------------------------------------------------------------
# The engine is a free Render instance: it stops after 15 minutes without
# traffic and its edge answers a mutating request with 429/502/503 until the
# service is up.  On 2026-10-23 that is exactly what killed LLD-V01 for a
# project whose six changes had just been re-approved: AS-V01 was frozen at
# 10:02:36, the re-plan was accepted at 10:03:00, the edge answered 429 and
# the run was settled as `failed` at 0 % half a second later — while the
# engine finished booting on its own at 10:05:19.
#
# So the submit waits like every other engine call in this codebase does
# (``ftth_hld.pipeline.ensure_engine_awake``, spent before the call, on a
# /health probe).  The probe spends the boot; the retry below covers the edge
# still refusing writes for a moment after /health answers.
#
# Only statuses that mean "the request never reached the application" are
# retried.  A read timeout is NOT: the engine keys a task by
# (project_id, lld_version), so re-posting one that was in fact accepted would
# queue the same design twice and have both writers race over the same files.
EDGE_REFUSALS = frozenset({429, 502, 503, 504})
SUBMIT_RETRY_S = 5.0
SUBMIT_MAX_RETRY_S = 30.0
# A boot measured 139 s on 2026-10-23 (10:03:00 accepted → 10:05:19 live), so
# the retry budget is the same order as the wake budget and does not add a
# second long wait on top of it: the wake has already spent the boot.
SUBMIT_BUDGET_S = 120.0


def _seconds(name: str, default: float) -> float:
    """A retry setting from the environment, read per call like the wake's."""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _detail(resp: requests.Response) -> str:
    """The engine's own message, or the edge's body when there is no JSON."""
    try:
        body = resp.json()
    except Exception:
        return (resp.text or '').strip()[:300] or 'no detail'
    if isinstance(body, dict):
        return str(body.get('detail') or body)[:300]
    return str(body)[:300]


def _retry_after(resp: requests.Response) -> float | None:
    """Seconds the edge asked for, from ``Retry-After`` (seconds or HTTP-date)."""
    headers = getattr(resp, 'headers', None) or {}
    raw = headers.get('Retry-After') if headers else None
    if not raw:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        pass
    try:
        when = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _give_up(what: str, budget: float, last: str) -> RuntimeError:
    """The reply for an engine that stayed asleep past the budget."""
    return RuntimeError(
        f'{what} could not start: the engine at {_ENGINE} did not accept it within '
        f'{int(budget)} s ({last}). It is a free instance that sleeps when it has been '
        'idle, so the first request after a quiet spell has to wait for it to boot. '
        'Nothing was submitted and nothing was lost \u2014 start the run again in a '
        'minute; a boot usually finishes well inside two.'
    )


def _submit(path: str, what: str, project_id: str, lld_version: str, dataset: dict) -> dict:
    """Wake the engine, then submit until it accepts or the budget runs out."""
    ensure_engine_awake(what)
    url = _engine_url(path)
    budget = _seconds('FTTH_LLD_SUBMIT_BUDGET', SUBMIT_BUDGET_S)
    delay = max(0.5, _seconds('FTTH_LLD_SUBMIT_RETRY', SUBMIT_RETRY_S))
    max_delay = max(delay, _seconds('FTTH_LLD_SUBMIT_MAX_RETRY', SUBMIT_MAX_RETRY_S))
    deadline = time.monotonic() + budget
    waited = 0.0
    last = 'no answer'
    while True:
        pause = delay
        try:
            resp = requests.post(
                url,
                json={'project_id': project_id, 'lld_version': lld_version, 'dataset': dataset},
                timeout=120,
                headers=engine_write_headers(),
            )
        except requests.RequestException as exc:
            # An ambiguous failure, not an edge refusal: the request may have
            # been accepted. Fail with the engine's own wording instead of
            # risking a second task for the same version.
            msg = f'{what} could not reach the engine at {_ENGINE}: {exc}'
            raise RuntimeError(msg) from exc
        if resp.status_code in (200, 201, 202):
            if waited:
                logger.info('%s accepted after %.0f s of waiting for the engine.', what, waited)
            return dict(resp.json())
        if resp.status_code not in EDGE_REFUSALS:
            msg = f'{what} returned {resp.status_code}: {_detail(resp)}'
            raise RuntimeError(msg)
        last = f'HTTP {resp.status_code}: {_detail(resp)}'
        asked = _retry_after(resp)
        if asked is not None:
            pause = max(pause, asked)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _give_up(what, budget, last)
        pause = min(pause, remaining)
        logger.warning(
            '%s was refused by the engine edge (%s); retrying in %.0f s.', what, last, pause
        )
        time.sleep(pause)
        waited += pause
        delay = min(delay * 2, max_delay)


def lld_run(project_id: str, lld_version: str, dataset: dict) -> dict:
    """Submit an LLD run to the engine. Returns the engine's task dict."""
    return _submit('/ftth/lld/run', 'LLD run', project_id, lld_version, dataset)


def lld_replan(project_id: str, lld_version: str, dataset: dict) -> dict:
    """Submit a Mode B (full re-plan) LLD run to the engine.

    Same request shape as ``lld_run`` — the engine re-runs the HLD oneclick
    pipeline with the approved survey dataset as brownfield input.
    """
    return _submit('/ftth/lld/replan', 'LLD re-plan', project_id, lld_version, dataset)


def lld_status(project_id: str, lld_version: str) -> dict | None:
    """Poll the engine for an LLD run's status. Returns None if unreachable."""
    url = _engine_url(f'/ftth/lld/results/{project_id}/{lld_version}')
    try:
        # 30 s, not 10: the hosted free instance runs at 0.1 vCPU and a status
        # read on a busy engine (10 k features in memory) can take longer than
        # a local one. A timeout here is retried by the caller, but a short one
        # turns a working run into a wall of "engine unreachable" warnings.
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            return dict(resp.json())
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
