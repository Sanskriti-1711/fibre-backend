"""Durable ownership of LLD run pollers.

``_run_lld_job`` drives a run from a daemon thread inside a gunicorn worker.
On the hosted free instance that worker is recycled every 30-70 minutes (and
the whole instance sleeps after 15 idle minutes), and the recycle kills the
thread silently: the engine keeps designing, nothing reads its status, and the
run row is left ``running`` forever — the output layers are never persisted, no
BOQ is regenerated, and the planner watches a progress bar that never finishes.
LLD-V20 hit exactly this on 2026-10-22 (poller killed at 20:39, engine finished
at 20:53, row still ``running``).

Ownership is therefore a database fact instead of a thread's private business:
the poller holds ``poller_token`` and refreshes ``poller_heartbeat``, and this
module's sweeper finds running rows whose heartbeat stopped and attaches a new
poller *without* re-submitting the run — the engine task is still in flight, so
the result is persisted by whichever process is alive when it completes.
"""

import logging
import os
import threading
import time
import uuid
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import LldRun

logger = logging.getLogger(__name__)

# How often an owning poller refreshes its heartbeat.
HEARTBEAT_SECONDS = int(os.environ.get('LLD_POLLER_HEARTBEAT_SECONDS', '15'))
# How old a heartbeat may be before another process may take the run over.
# Generous: one loop turn can span a 30 s layer fetch, and stealing a poller
# that is still writing is worse than waiting a couple of minutes.
STALE_SECONDS = int(os.environ.get('LLD_POLLER_STALE_SECONDS', '180'))
# How often a process looks for orphaned runs. 0 disables the sweeper.
SWEEP_SECONDS = int(os.environ.get('LLD_POLLER_SWEEP_SECONDS', '60'))
# Do not start a second poller for the same run before the first heartbeat
# from the one just started can land.
_START_THROTTLE_SECONDS = 20

_starts = {}
_starts_lock = threading.Lock()


def new_token() -> str:
    """A fresh poller identity."""
    return uuid.uuid4().hex


def claim_poller(run_id, token: str) -> bool:
    """Take (or keep) ownership of a run's poller.

    True when ``token`` owns the run after this call: either it already did, or
    the previous owner's heartbeat went stale and the run was handed over. The
    filter is part of the ``UPDATE``, so two workers sweeping at the same
    moment cannot both win.
    """
    stale_before = timezone.now() - timedelta(seconds=STALE_SECONDS)
    updated = (
        LldRun.objects.filter(pk=run_id, status=LldRun.STATUS_RUNNING)
        .filter(
            Q(poller_token=token)
            | Q(poller_heartbeat__isnull=True)
            | Q(poller_heartbeat__lt=stale_before)
        )
        .update(poller_token=token, poller_heartbeat=timezone.now())
    )
    return bool(updated)


def start_poller(project_id: str, run_id, *, token: str) -> None:
    """Attach a poller to an engine task that is already in flight.

    ``submit=False`` is what makes this safe: the re-plan keeps running on the
    engine and is only re-read, never re-sent.
    """
    from .views import _run_lld_job  # imported here: views imports this module

    threading.Thread(
        target=_run_lld_job,
        args=(project_id, run_id, False, token),
        daemon=True,
        name=f'lld-poller-{str(run_id)[:8]}',
    ).start()


def reattach_stale(project_id: str | None = None) -> int:
    """Give every running run with a dead poller a new one.

    Returns how many were reattached. Never raises: recovering a run must not
    be able to fail the request or the sweep that asked for it.
    """
    stale_before = timezone.now() - timedelta(seconds=STALE_SECONDS)
    try:
        qs = LldRun.objects.filter(status=LldRun.STATUS_RUNNING).filter(
            Q(poller_heartbeat__isnull=True) | Q(poller_heartbeat__lt=stale_before)
        )
        if project_id:
            qs = qs.filter(ftth_project_id=project_id)
        reattached = 0
        now = time.monotonic()
        for run in qs.only('id', 'ftth_project_id', 'lld_version'):
            key = str(run.pk)
            with _starts_lock:
                started_at = _starts.get(key)
                if started_at is not None and now - started_at < _START_THROTTLE_SECONDS:
                    continue
                _starts[key] = now
            start_poller(run.ftth_project_id, run.pk, token=new_token())
            reattached += 1
            logger.warning(
                'LLD poller for %s/%s had stopped heartbeating; reattached without '
                'resubmitting the engine run.',
                run.ftth_project_id,
                run.lld_version,
            )
    except Exception:  # noqa: BLE001 - recovery is best effort, the run is already recorded
        logger.exception('Could not reattach stale LLD pollers')
        return 0
    else:
        return reattached


def start_sweeper() -> None:
    """Watch for orphaned LLD runs for the life of this process."""
    logger.info('LLD poller sweeper started (every %s s).', SWEEP_SECONDS)
    threading.Thread(target=_sweep_forever, daemon=True, name='lld-poller-sweeper').start()


def _sweep_forever() -> None:
    from django.db import close_old_connections

    while True:
        time.sleep(max(5, SWEEP_SECONDS))
        try:
            close_old_connections()
            reattach_stale()
        except Exception:  # noqa: BLE001 - the loop must never die
            logger.exception('LLD poller sweep failed')
        finally:
            close_old_connections()
