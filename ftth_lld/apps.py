import os
import sys

from django.apps import AppConfig


class FtthLldConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ftth_lld'
    verbose_name = 'FTTH LLD'

    def ready(self):
        """Start the orphaned-poller sweeper (see ``poller.py``).

        A run's poller lives on a worker thread, so a worker recycle kills it
        while the engine keeps designing; without a sweeper the run would stay
        ``running`` forever. The sweeper reattaches a poller without resubmitting
        the in-flight engine task.

        Not started where polling makes no sense: management commands that
        create/drop a database (``test``), ones that may not have the table yet
        (``migrate``, ``makemigrations``, ``collectstatic``), the autoreload
        supervisor (only its child serves requests), or when explicitly
        disabled with ``LLD_POLLER_SWEEP_SECONDS=0``.
        """
        from . import poller

        if poller.SWEEP_SECONDS <= 0:
            return
        if 'test' in sys.argv or {'migrate', 'makemigrations', 'collectstatic'} & set(sys.argv):
            return
        if 'runserver' in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            return
        poller.start_sweeper()
