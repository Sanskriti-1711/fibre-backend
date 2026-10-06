"""The work that follows a completed HLD run — off the status request path.

Everything here used to run **inside** ``GET /api/ftth/hld/results/<id>/``
(``ftth_hld.api.PipelineStatusView``), guarded by data the chain itself
produces: "any ``gis.trench_layer`` row with a NULL ``fclass``" and
``FtthLayer.updated_at``. Both guards re-arm on their own:

* ``gis.trench_layer`` is written by the **engine**, once per run, and the
  engine's trench payload carries no ``fclass`` — so every fresh run leaves 544
  NULL rows and the next poll pays the full spatial join against the project's
  405,599-road extract (**measured 234.7 s**, one call).
* ``sections_are_fresh()`` compared ``trenches.updated_at`` with
  ``trench_sections.updated_at``, and any re-publish of the trench layer bumps
  the former while the engine deliberately serves nothing for the latter — so
  the section rebuild re-fired on its own too (2.6 s).

This module makes both facts durable:

* the chain never runs in a request — ``schedule_post_hld()`` claims a
  ``HldPostProcess`` row and runs the steps in a background thread, so a status
  poll is always a couple of cheap queries;
* the guard is the **content revision of the project's trench rows**
  (``trench_content_revision`` — a hash of the gis row ids), not a timestamp or
  a NULL count, so re-publishing unchanged layers is a no-op and only a new
  engine run (which re-inserts the rows with new ids) re-arms it. The NULL-count
  check is kept as a belt-and-braces trigger, but it now costs a background
  thread rather than the request.

The row also carries the per-step outcome, so the frontend can show what the
post-processing actually did instead of the chain being invisible.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import timedelta

from django.db import connection, connections
from django.utils import timezone

from .models import HldPostProcess

logger = logging.getLogger(__name__)

# Ordered steps. The order is load-bearing: sections read the road attribution,
# the reference layers must exist before the matrix reads them, and the package
# reads the matrix.
STEP_ORDER = (
    "layers",
    "road_class",
    "trench_sections",
    "reference_layers",
    "permit_matrix",
    "permit_package",
)

STEP_LABELS = {
    "layers": "Sync HLD layers into the GIS database",
    "road_class": "Attribute OSM road class onto the trench segments",
    "trench_sections": "Expand trenches into street-attributed sections",
    "reference_layers": "Load OSM reference layers for the project's own area",
    "permit_matrix": "Run the permit rule engine",
    "permit_package": "Generate the preliminary permit package",
}

PACKAGE_PREFIX = "HLD detailed preliminary permit package"

# A worker that dies leaves its row ``running``; after this long the next poll
# may claim it again. Generous: the road attribution alone is ~4 minutes.
STALE_SECONDS = 3600


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _gis_relation(name: str) -> bool:
    """Does ``gis.<name>`` exist?

    ``to_regclass`` answers without raising for a missing relation, which
    matters: the gis tables are created by the engine, not by a migration, so
    they are absent on a fresh deployment and in the test database. A plain
    ``SELECT ... FROM gis.<name>`` would abort the surrounding transaction —
    and with ``ATOMIC_REQUESTS`` or a test's atomic block that poisons every
    later query in the request, not just this check.
    """
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", ["gis." + name])
            return cur.fetchone()[0] is not None
    except Exception:  # noqa: BLE001
        return False


def trench_content_revision(project_id: str) -> str:
    """Content revision of a project's trench rows — ``""`` when there are none.

    A hash of the gis row ``id``s. Those are ``bigserial``, written by the
    engine once per run, so the revision changes exactly when a new run has
    replaced the trench rows and stays put when layers are merely re-published
    — which is the property ``updated_at`` and "any NULL fclass" both lack.

    Returns ``""`` (and callers skip the trench-consuming steps) when the gis
    table is absent or the project has no trenches, so the chain is safe to
    call on a fresh project and from a test database without the GIS schema.
    """
    if not _gis_relation("trench_layer"):
        return ""
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT count(*), COALESCE(md5(string_agg(id::text, ',' "
                "ORDER BY id)), '') FROM gis.trench_layer WHERE project_id = %s",
                [project_id],
            )
            count, digest = cur.fetchone()
    except Exception:  # noqa: BLE001 - no gis schema / no table is not an error
        return ""
    count = int(count or 0)
    if not count:
        return ""
    return "%d:%s" % (count, digest or "")


def road_class_gaps(project_id: str) -> int:
    """Trench rows still missing a road class (``fclass``)."""
    if not _gis_relation("trench_layer"):
        return 0
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM gis.trench_layer WHERE project_id = %s "
                "AND properties->>'fclass' IS NULL",
                [project_id],
            )
            return int(cur.fetchone()[0])
    except Exception:  # noqa: BLE001
        return 0


def _package_exists(project_id: str) -> bool:
    from permits.models import PermitDocument

    return PermitDocument.objects.filter(
        permit__project_id=project_id, name__startswith=PACKAGE_PREFIX
    ).exists()


def _reference_layers_done(row) -> bool:
    """Has the reference-layer load already succeeded for this row?

    Keyed on the step's own recorded outcome rather than on a table count: an
    area with no railway legitimately has zero railway rows, so counting rows
    would re-fetch from Overpass on every poll forever, while a *failed* load
    (Overpass down) must stay pending until it succeeds. A new trench revision
    re-runs the step anyway (the bbox follows the trenches), which is what the
    ``stale`` arm of the caller covers.
    """
    step = ((row.steps or {}) if row is not None else {}).get("reference_layers") or {}
    return bool(step.get("ok"))


def _matrix_exists(project_id: str) -> bool:
    from permits.models import PermitMatrix

    return PermitMatrix.objects.filter(project_id=project_id).exists()


def _missing_layers(project_id: str, layer_names=None) -> list[str]:
    """Engine layers that have no ``FtthLayer`` row yet."""
    from .models import FtthLayer

    if layer_names is None:
        try:
            from .pipeline import get_status
            layer_names = [
                (l.get("name") or "").lower()
                for l in get_status(project_id).get("layers", [])
                if l.get("name")
            ]
        except Exception:  # noqa: BLE001
            return []
    have = set(
        FtthLayer.objects.filter(ftth_project__project_id=project_id)
        .values_list("name", flat=True)
    )
    return [n for n in layer_names if n and n.lower() not in have]


def _pending_steps(project_id: str, row, revision: str,
                   layer_names=None) -> list[str]:
    """The steps that are genuinely out of date — in ``STEP_ORDER``.

    ``revision`` is the trench content revision right now. Empty means the
    project has no trench rows at all, so only the layer sync is meaningful.
    Every trench-consuming step also keeps its own guard (gaps, section
    freshness, matrix/package existence), checked with OR — a step is re-run
    when either the trenches moved under it or its own output is gone.
    """
    pending = []
    if _missing_layers(project_id, layer_names):
        pending.append("layers")
    if not revision:
        return pending
    # A row that has never run (a project whose chain ran inline before this
    # module existed) falls back to each step's own existence guard, exactly as
    # the old completion hook did — so the first poll after deploying this does
    # not re-run a permit matrix and a package that are already there. From the
    # second run on, the recorded revision decides.
    stale = row is not None and (row.trench_revision or "") != revision
    if stale or road_class_gaps(project_id):
        pending.append("road_class")
    from permits.analysis.trench_sections import sections_are_fresh

    if stale or not sections_are_fresh(project_id):
        pending.append("trench_sections")
    if stale or not _reference_layers_done(row):
        pending.append("reference_layers")
    if stale or not _matrix_exists(project_id):
        pending.append("permit_matrix")
    if stale or not _package_exists(project_id):
        pending.append("permit_package")
    return pending


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------

def _step_layers(project_id: str, project_name: str = "") -> str:
    from .pipeline import sync_project_layers

    counts = sync_project_layers(project_id)
    return "synced %d layer(s)" % len(counts)


def _step_road_class(project_id: str, project_name: str = "") -> str:
    from permits.analysis.road_class import ensure_road_class

    summary = ensure_road_class(project_id)
    if summary.get("error"):
        raise RuntimeError(summary["error"])
    return "attributed %s/%s trench(es) from %s road(s)" % (
        summary.get("attributed"), summary.get("trenches"), summary.get("roads"),
    )


def _step_trench_sections(project_id: str, project_name: str = "") -> str:
    from permits.analysis.trench_sections import build_trench_sections

    summary = build_trench_sections(project_id)
    return "%s section(s) from %s trench row(s), %s street-attributed" % (
        summary.get("sections"), summary.get("rows"), summary.get("attributed"),
    )


def _step_reference_layers(project_id: str, project_name: str = "") -> str:
    """Load the OSM permit reference layers for this project's own area.

    Without this step the railway/waterway/protected-area/tree rules can only
    ever record a gap: their ``gis.osm_*`` tables are filled by a management
    command nobody runs per project, and its default bbox covers one city — so
    a permit package built from an area input anywhere else silently "found no"
    waterways, railways and protected areas. The bbox is the project's own
    trench extent, and only this project's rows are replaced.
    """
    from permits.management.commands.load_osm_reference_layers import (
        load_for_project,
    )

    summary = load_for_project(project_id)
    if summary.get("skipped"):
        return summary["skipped"]
    counts = summary.get("counts") or {}
    return "%s feature(s) across %s layer(s) for bbox %s" % (
        sum(counts.values()), len(counts), summary.get("bbox"),
    )


def _step_permit_matrix(project_id: str, project_name: str = "") -> str:
    from permits.rules.engine import run_analysis

    summary = run_analysis(project_id)
    return "%s row(s) from %s rule(s) fired" % (
        summary.get("rows_created", 0), len(summary.get("rules_fired", [])),
    )


def _step_permit_package(project_id: str, project_name: str = "") -> str:
    from permits.generators.hld_package import generate_hld_package

    summary = generate_hld_package(project_id, project_name or project_id)
    return "v%s, %s file(s)" % (
        summary.get("version"), len(summary.get("files", [])),
    )


STEP_FUNCS = {
    "layers": _step_layers,
    "road_class": _step_road_class,
    "trench_sections": _step_trench_sections,
    "reference_layers": _step_reference_layers,
    "permit_matrix": _step_permit_matrix,
    "permit_package": _step_permit_package,
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_post_hld(project_id: str, project_name: str = "",
                 steps=None) -> dict:
    """Run the post-HLD steps and record each outcome on the row.

    Each step is recorded as it finishes, so a long chain (the road
    attribution alone is minutes) is observable while it runs. One step
    failing does not abort the rest — the matrix is still worth running with
    an incomplete attribution — but the row ends ``failed`` and the error is
    kept, and ``trench_revision`` is only advanced when every step succeeded.
    """
    steps = list(steps or STEP_ORDER)
    row = HldPostProcess.objects.filter(project_id=project_id).first()
    if row is None:
        row = HldPostProcess.objects.create(project_id=project_id)
    HldPostProcess.objects.filter(pk=row.pk).update(
        status=HldPostProcess.STATUS_RUNNING,
        started_at=timezone.now(),
        error_message="",
    )

    revision = trench_content_revision(project_id)
    outcomes = dict(row.steps or {})
    failures = []
    for name in steps:
        step = STEP_FUNCS.get(name)
        if step is None:
            continue
        started = time.monotonic()
        try:
            detail = step(project_id, project_name) or ""
            ok, error = True, ""
        except Exception as exc:  # noqa: BLE001 - one step must not kill the chain
            ok, error = False, str(exc)
            failures.append("%s: %s" % (name, exc))
            logger.warning("post-HLD %s step %s failed: %s", project_id, name, exc)
        outcomes[name] = {
            "ok": ok,
            "seconds": round(time.monotonic() - started, 2),
            "detail": detail if ok else "",
            "error": error,
            "at": timezone.now().isoformat(),
        }
        HldPostProcess.objects.filter(pk=row.pk).update(steps=outcomes)

    # Re-read: a new engine run while we worked means the revision we just
    # applied to is already gone, and the next poll should re-schedule.
    revision_now = trench_content_revision(project_id)
    HldPostProcess.objects.filter(pk=row.pk).update(
        status=(
            HldPostProcess.STATUS_FAILED if failures
            else HldPostProcess.STATUS_DONE
        ),
        trench_revision="" if failures else revision_now,
        steps=outcomes,
        error_message="; ".join(failures)[:2000],
        finished_at=timezone.now(),
    )
    logger.info(
        "post-HLD %s: %s step(s) run (%s), revision %s",
        project_id, len(steps), "failed" if failures else "ok",
        revision_now or "(no trenches)",
    )
    return outcomes


def _worker(project_id: str, project_name: str, steps) -> None:
    """Thread entry point — its own DB connection, closed when it finishes."""
    try:
        run_post_hld(project_id, project_name, steps)
    except Exception as exc:  # noqa: BLE001
        logger.warning("post-HLD worker for %s died: %s", project_id, exc)
        HldPostProcess.objects.filter(project_id=project_id).update(
            status=HldPostProcess.STATUS_FAILED,
            error_message=str(exc)[:2000],
            finished_at=timezone.now(),
        )
    finally:
        connections.close_all()


def _spawn_disabled() -> bool:
    """Never spawn a worker from a test runner.

    The test database has no ``gis`` schema and is rolled back per test, so a
    worker thread could only fail or fight the rollback; and
    ``PipelineStatusView`` must stay request-cheap in tests.
    """
    if os.environ.get("FTTH_POST_HLD_SYNC") == "1":
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return "test" in sys.argv


def schedule_post_hld(project_id: str, project_name: str = "",
                      layer_names=None):
    """Claim the post-HLD chain for ``project_id`` and run it off-request.

    Returns the ``HldPostProcess`` row, or ``None`` when the caller should do
    nothing (test runner). Idempotent and cheap: when every step is already
    fresh this is two or three queries and no thread. Repeated polls while a
    worker runs do not start a second one — the claim is a conditional update
    that excludes a ``running`` row younger than ``STALE_SECONDS``.
    """
    if _spawn_disabled():
        return None

    try:
        row, _created = HldPostProcess.objects.get_or_create(project_id=project_id)
        now = timezone.now()
        if (row.status == HldPostProcess.STATUS_RUNNING and row.started_at
                and now - row.started_at < timedelta(seconds=STALE_SECONDS)):
            return row

        revision = trench_content_revision(project_id)
        pending = _pending_steps(project_id, row, revision, layer_names)
        if not pending:
            if row.status != HldPostProcess.STATUS_DONE:
                HldPostProcess.objects.filter(pk=row.pk).update(
                    status=HldPostProcess.STATUS_DONE,
                    trench_revision=revision,
                    finished_at=now,
                )
            return row

        claimed = (
            HldPostProcess.objects.filter(pk=row.pk)
            .exclude(
                status=HldPostProcess.STATUS_RUNNING,
                started_at__gte=now - timedelta(seconds=STALE_SECONDS),
            )
            .update(status=HldPostProcess.STATUS_RUNNING, started_at=now)
        )
        if not claimed:
            return HldPostProcess.objects.filter(pk=row.pk).first()
        logger.info(
            "post-HLD %s scheduled off-request: %s", project_id, ", ".join(pending)
        )
        threading.Thread(
            target=_worker,
            args=(project_id, project_name, pending),
            name="post-hld-%s" % project_id[:8],
            daemon=True,
        ).start()
        return HldPostProcess.objects.filter(pk=row.pk).first()
    except Exception as exc:  # noqa: BLE001 - the status poll must never fail on this
        logger.warning("post-HLD scheduling for %s failed: %s", project_id, exc)
        return None


def post_hld_state(project_id: str) -> dict:
    """Cheap read of the chain's state, for the status payload."""
    try:
        row = HldPostProcess.objects.filter(project_id=project_id).first()
    except Exception:  # noqa: BLE001
        return {}
    if row is None:
        return {"status": "none", "steps": {}, "remaining": list(STEP_ORDER)}
    steps = row.steps or {}
    return {
        "status": row.status,
        "trench_revision": row.trench_revision,
        "steps": steps,
        "remaining": [n for n in STEP_ORDER if n not in steps],
        "error": row.error_message,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }
