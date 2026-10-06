"""Health and readiness probes for the container platform and for monitoring.

Two probes, two jobs. Keep them apart:

``/healthz``
    Liveness. Touches nothing external. This is what the hosting platform's
    health check points at, so it must stay fast and must NOT fail when a
    dependency is down.

``/healthz/engine``
    Dependency probe for the FastAPI pipeline engine. Answers 503 when the
    engine is unreachable, times out, or reports itself unhealthy, so a
    monitor can alert on it.

Do NOT point the platform's health check at ``/healthz/engine``. An engine
outage would then look like a dead container and trigger a restart loop,
turning a degraded dependency into an outage of the service that was still
healthy enough to be useful.
"""

from __future__ import annotations

import logging
import time

import requests
from django.http import JsonResponse

from ftth_hld.config import FTTH_ENGINE_URL

logger = logging.getLogger(__name__)

# The engine's own /health is NOT a cheap ping: it gathers PostGIS info and
# OSM status before answering. Measured against the live Zeabur engine
# (2026-10-06) it took ~5.3s, so anything near 5s produces intermittent false
# 503s. Note also that requests' timeout is applied per-socket-operation
# (connect, then read), not to the total duration, which is why a 5s timeout
# did not raise on a 5.3s response.
#
# 15s is deliberately generous: a dependency probe that flaps is worse than a
# slow one, and monitoring polls this far less often than it does liveness.
ENGINE_PROBE_TIMEOUT = 15


def healthz(request):
    """Liveness probe. Deliberately touches no external dependency."""
    return JsonResponse({"status": "ok"})


def engine_health(request):
    """Report whether the pipeline engine is reachable and healthy.

    ``200`` engine answered and reported ``status == "ok"``.
    ``503`` engine unreachable, timed out, or unhealthy.

    The response is trimmed deliberately: the engine's own ``/health`` exposes
    the ``qgis_process`` filesystem path and database details, and this
    endpoint is unauthenticated so a monitor can reach it. Only the fields
    that make an outage actionable are surfaced.
    """
    url = f"{FTTH_ENGINE_URL}/health"
    started = time.monotonic()
    payload = {
        "ok": False,
        "engine_url": FTTH_ENGINE_URL,
        "checked_url": url,
    }

    try:
        response = requests.get(url, timeout=ENGINE_PROBE_TIMEOUT)
    except requests.RequestException as exc:
        payload["latency_ms"] = int((time.monotonic() - started) * 1000)
        payload["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("Engine health probe failed for %s: %s", url, exc)
        return JsonResponse(payload, status=503)

    payload["latency_ms"] = int((time.monotonic() - started) * 1000)
    payload["http_status"] = response.status_code

    if response.status_code != 200:
        payload["error"] = f"engine returned HTTP {response.status_code}"
        return JsonResponse(payload, status=503)

    try:
        body = response.json()
    except ValueError:
        payload["error"] = "engine returned a non-JSON body"
        return JsonResponse(payload, status=503)

    payload["engine_status"] = body.get("status")
    payload["service"] = body.get("service")
    payload["uptime_seconds"] = body.get("uptime_seconds")
    # A reachable engine with no qgis_process cannot run a single pipeline, so
    # it is worth alerting on separately. It is reported as a boolean rather
    # than the raw path, and it is deliberately NOT part of the ok/503 verdict:
    # the engine itself still answers, which is a different failure mode from
    # being unreachable.
    payload["qgis_available"] = bool(body.get("qgis_process"))

    if body.get("status") != "ok":
        payload["error"] = f"engine reported status={body.get('status')!r}"
        return JsonResponse(payload, status=503)

    payload["ok"] = True
    return JsonResponse(payload)
