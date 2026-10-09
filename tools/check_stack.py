#!/usr/bin/env python3
"""Fibre-FTTH stack check.

Probes every component of the live stack and, most importantly, the
INTEGRATION between them: whether the backend will actually answer the
frontend's origin.  Component liveness alone does not prove the stack
works -- the backend and the frontend were both healthy on 2026-10-06
while every browser call failed, because the backend's CORS allowlist
did not contain the frontend's origin.

Checks (in order):

  1. frontend        -- the deployed static UI answers
  2. frontend config -- which backend URL the deployed UI is pointed at
  3. backend liveness-- GET /healthz (no external dependencies)
  4. backend API     -- the login endpoint answers (proves Django serves)
  5. CORS preflight  -- the backend allows the frontend's origin
  6. engine health   -- GET /health on the FastAPI/QGIS engine
  7. database        -- TCP reachability of the shared Postgres

Exit status is 0 only when nothing FAILed (WARN is tolerated).  Suitable
for cron, a GitHub Actions schedule, or an uptime monitor running it.

Everything is overridable by environment variable:

  FRONTEND_URL    default https://fibre-fe-98f8e0.gitlab.io
  FRONTEND_ORIGIN default = FRONTEND_URL's origin
  BACKEND_URL     default https://fibre-backend-wml3.onrender.com
  ENGINE_URL      default https://ftth-planning.onrender.com
  PG_HOST / PG_PORT  default 91.98.18.217 / 32467
  CHECK_TIMEOUT   default 120 seconds, per request

No credentials are needed or read -- the database check is a TCP connect
only, so this is safe to run anywhere and safe to share its output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Per-request timeout, in seconds.
# 20s is NOT enough for the hosts this checks.  Measured 2026-10-09: a cold
# Render engine took 85.7s to answer /health and the backend 17.7s, because the
# free tier sleeps when idle.  A short timeout therefore reports a false FAIL --
# and the scheduled monitor opens a "Stack check failing" issue -- every time the
# stack has napped.  Override with CHECK_TIMEOUT.
TIMEOUT = float(os.getenv("CHECK_TIMEOUT", "120"))

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://fibre-fe-98f8e0.gitlab.io").rstrip("/")
BACKEND_URL = os.getenv("BACKEND_URL", "https://fibre-backend-wml3.onrender.com").rstrip("/")
ENGINE_URL = os.getenv("ENGINE_URL", "https://ftth-planning.onrender.com").rstrip("/")
PG_HOST = os.getenv("PG_HOST", "91.98.18.217")
PG_PORT = int(os.getenv("PG_PORT", "32467"))

_SSL = ssl.create_default_context()


def _origin(url: str) -> str:
    m = re.match(r"^(https?://[^/]+)", url)
    return m.group(1) if m else url


FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", _origin(FRONTEND_URL))


class Result:
    __slots__ = ("name", "status", "detail")

    def __init__(self, name: str, status: str, detail: str) -> None:
        self.name = name
        self.status = status  # PASS | WARN | FAIL
        self.detail = detail


def _request(method: str, url: str, headers: dict[str, str] | None = None,
             body: bytes | None = None):
    """Return (status_code, headers, body_text).  HTTP errors are values,
    not exceptions -- a 404 or a 400 is a result we want to report."""
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("User-Agent", "fibre-ftth-stack-check/1.0")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL) as r:
            return r.status, dict(r.headers), r.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read(200_000).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - a network failure is a result
        raise ConnectionError(f"{type(e).__name__}: {e}") from e


def check_frontend() -> Result:
    t0 = time.time()
    try:
        code, _, body = _request("GET", f"{FRONTEND_URL}/")
    except ConnectionError as e:
        return Result("frontend", "FAIL", f"unreachable -- {e}")
    ms = (time.time() - t0) * 1000
    if code != 200:
        return Result("frontend", "FAIL", f"HTTP {code} in {ms:.0f}ms")
    m = re.search(r"<title>([^<]*)</title>", body)
    title = m.group(1).strip() if m else "(no title)"
    return Result("frontend", "PASS", f"200 in {ms:.0f}ms -- {title}")


def check_frontend_config() -> tuple[Result, str | None]:
    """Which backend URL the *deployed* UI actually calls."""
    try:
        code, _, body = _request("GET", f"{FRONTEND_URL}/js/ftth-config.js")
    except ConnectionError as e:
        return Result("frontend config", "WARN", f"could not read ftth-config.js -- {e}"), None
    if code != 200:
        return Result("frontend config", "WARN", f"ftth-config.js HTTP {code}"), None
    m = re.search(r"PROD_API\s*=\s*['\"]([^'\"]+)['\"]", body)
    if not m:
        return Result("frontend config", "WARN", "no PROD_API found in ftth-config.js"), None
    api = m.group(1).rstrip("/")
    if api == BACKEND_URL:
        return Result("frontend config", "PASS", f"PROD_API -> {api}"), api
    return Result("frontend config", "WARN", f"PROD_API -> {api} (probe target is {BACKEND_URL})"), api


def check_backend_liveness() -> Result:
    t0 = time.time()
    try:
        code, _, body = _request("GET", f"{BACKEND_URL}/healthz")
    except ConnectionError as e:
        return Result("backend /healthz", "FAIL", f"unreachable -- {e}")
    ms = (time.time() - t0) * 1000
    if code == 200:
        return Result("backend /healthz", "PASS", f"200 in {ms:.0f}ms")
    if code == 404:
        return Result(
            "backend /healthz", "WARN",
            f"404 -- the deployed revision predates the health probe ({ms:.0f}ms)",
        )
    return Result("backend /healthz", "FAIL", f"HTTP {code} in {ms:.0f}ms -- {body[:80]}")


def check_backend_api() -> Result:
    body = json.dumps({}).encode()
    try:
        code, _, text = _request(
            "POST", f"{BACKEND_URL}/api/users/login/",
            headers={"Content-Type": "application/json"}, body=body,
        )
    except ConnectionError as e:
        return Result("backend API", "FAIL", f"unreachable -- {e}")
    if code in (400, 401, 403):
        return Result("backend API", "PASS", f"login endpoint answered {code} (app serving)")
    if code == 200:
        return Result("backend API", "WARN", "login endpoint returned 200 to an empty body")
    return Result("backend API", "FAIL", f"HTTP {code} -- {text[:80]}")


def check_cors() -> Result:
    """The check that catches the 2026-10-06 outage: can a browser at the
    frontend's origin actually talk to this backend?"""
    try:
        code, headers, _ = _request(
            "OPTIONS", f"{BACKEND_URL}/api/users/login/",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    except ConnectionError as e:
        return Result("CORS", "FAIL", f"preflight unreachable -- {e}")
    allowed = {k.lower(): v for k, v in headers.items()}.get("access-control-allow-origin")
    if allowed in (FRONTEND_ORIGIN, "*"):
        return Result("CORS", "PASS", f"{FRONTEND_ORIGIN} is allowed")
    if allowed:
        return Result("CORS", "FAIL",
                      f"backend allows '{allowed}', not {FRONTEND_ORIGIN}")
    return Result(
        "CORS", "FAIL",
        f"no Access-Control-Allow-Origin for {FRONTEND_ORIGIN} "
        f"(preflight HTTP {code}) -- browsers will block every API call",
    )


def engine_health_result(payload: dict, ms: float) -> Result:
    """Grade the engine's /health payload.

    Separate from the request so the payload shapes can be graded in a test
    without a live engine.  Both flags it reads are nested, and reading the
    wrong level is silent:

      * ``postgis`` is ``db_info()``, i.e. ``{"available": true|false}``.  That
        dict is ALWAYS truthy, so the obvious ``if d.get("postgis")`` reported
        "postgis ok" and a PASS on an engine with no database at all.  The live
        Zeabur engine has answered ``{"available": false}`` since it came up on
        2026-07-17 while this monitor, and the docs quoting it, read "postgis
        ok" -- a broken link that looked like a healthy one for 81 days.

      * ``qgis_process`` is a resolved PATH, or ``null`` when the engine found
        no launcher.  A path proves only that one was found, never that it
        runs, and ``null`` used to be dropped from the line entirely while the
        check still PASSed.

    A bare boolean is still accepted for ``postgis``, for older builds.
    """
    parts: list[str] = []
    up = payload.get("uptime_seconds")
    if isinstance(up, (int, float)):
        parts.append(f"up {up / 86400:.1f}d")

    qgis = payload.get("qgis_process")
    qgis_missing = not qgis
    parts.append(f"qgis {'ok' if not qgis_missing else 'MISSING'}")

    postgis = payload.get("postgis")
    if isinstance(postgis, dict):
        postgis_ok: bool | None = bool(postgis.get("available"))
    elif postgis is None:
        postgis_ok = None
    else:
        postgis_ok = bool(postgis)
    if postgis_ok is not None:
        parts.append(f"postgis {'ok' if postgis_ok else 'DOWN'}")

    parts.append(f"{ms:.0f}ms")
    bad = qgis_missing or postgis_ok is False
    return Result("engine /health", "WARN" if bad else "PASS", ", ".join(parts))


def check_engine() -> Result:
    t0 = time.time()
    try:
        code, _, body = _request("GET", f"{ENGINE_URL}/health")
    except ConnectionError as e:
        return Result("engine /health", "FAIL", f"unreachable -- {e}")
    ms = (time.time() - t0) * 1000
    if code != 200:
        return Result("engine /health", "FAIL", f"HTTP {code} in {ms:.0f}ms")
    try:
        d = json.loads(body)
    except ValueError:
        return Result("engine /health", "WARN", f"200 but body is not JSON ({ms:.0f}ms)")
    return engine_health_result(d, ms)


def check_database() -> Result:
    t0 = time.time()
    try:
        with socket.create_connection((PG_HOST, PG_PORT), timeout=TIMEOUT):
            pass
    except OSError as e:
        return Result("shared postgres", "FAIL", f"{PG_HOST}:{PG_PORT} unreachable -- {e}")
    ms = (time.time() - t0) * 1000
    return Result("shared postgres", "PASS", f"TCP {PG_HOST}:{PG_PORT} in {ms:.0f}ms")


def main() -> int:
    ap = argparse.ArgumentParser(description="Fibre-FTTH stack check")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quiet", action="store_true", help="only print failures and warnings")
    args = ap.parse_args()

    results: list[Result] = []
    results.append(check_frontend())
    cfg_result, _ = check_frontend_config()
    results.append(cfg_result)
    results.append(check_backend_liveness())
    results.append(check_backend_api())
    results.append(check_cors())
    results.append(check_engine())
    results.append(check_database())

    failed = [r for r in results if r.status == "FAIL"]
    warned = [r for r in results if r.status == "WARN"]

    if args.json:
        print(json.dumps({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "frontend_url": FRONTEND_URL,
            "frontend_origin": FRONTEND_ORIGIN,
            "backend_url": BACKEND_URL,
            "engine_url": ENGINE_URL,
            "results": [{"check": r.name, "status": r.status, "detail": r.detail} for r in results],
            "failed": len(failed),
            "warned": len(warned),
        }, indent=2))
        return 1 if failed else 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"Fibre-FTTH stack check -- {stamp}")
    print(f"  frontend {FRONTEND_URL}  ->  backend {BACKEND_URL}")
    print("-" * 78)
    for r in results:
        if args.quiet and r.status == "PASS":
            continue
        print(f"{r.status:5} {r.name:18} {r.detail}")
    print("-" * 78)
    if failed:
        print(f"{len(failed)} FAILED, {len(warned)} warning(s)")
    elif warned:
        print(f"All checks passed with {len(warned)} warning(s)")
    else:
        print("All checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
