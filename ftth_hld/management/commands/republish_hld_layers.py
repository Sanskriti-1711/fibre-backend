"""Republish a project's stored HLD layers from the engine's current design.

The platform's ``FtthLayer`` rows are what the results map, the BOQ and the
permit matrix read. They were only ever written for layers a project did NOT
have yet, so a run that finished after its publish left every existing name
holding the PREVIOUS design's geometry while PostGIS held the new one — one
project's pole and aerial stages wrote two hours after their publish, and its
map mixed 810-duct geometry with a 129-duct engine.

This command is the explicit, forced counterpart of the polling sync: it
re-publishes every layer the engine reports, removes engine-owned layers the
run no longer has, and leaves the platform-derived ones (``trench_sections``)
to the post-HLD chain, which its revision guard re-arms on its own.

Run (the engine must be reachable at ``FTTH_ENGINE_URL``)::

    python manage.py republish_hld_layers <project_id>
    python manage.py republish_hld_layers <project_id> --dry-run
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from ftth_hld import pipeline


class Command(BaseCommand):
    help = "Republish a project's stored HLD layers from the engine's current design."

    def add_arguments(self, parser):
        parser.add_argument('project_id', help='Project to republish')
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what the engine reports and what is stored, change nothing',
        )

    def handle(self, *args, **opts):
        project_id = opts['project_id']
        status = pipeline.get_status(project_id) or {}
        engine_layers = [
            (layer.get('name') or '').lower()
            for layer in status.get('layers') or []
            if layer.get('name')
        ]
        if not engine_layers:
            self.stdout.write(
                self.style.ERROR(
                    f"The engine reports no layers for {project_id} — nothing to "
                    'republish (is FTTH_ENGINE_URL pointing at the right engine?).'
                )
            )
            return

        self.stdout.write(f"Engine reports {len(engine_layers)} layer(s) for {project_id}:")
        for layer in status.get('layers') or []:
            self.stdout.write(
                f"  {layer.get('name'):<20} {layer.get('feature_count')}  "
                f"{layer.get('geometry_type') or ''}"
            )

        if opts['dry_run']:
            from ftth_hld.models import FtthLayer

            stored = sorted(
                FtthLayer.objects.filter(ftth_project__project_id=project_id).values_list(
                    'name', flat=True
                )
            )
            self.stdout.write(f"\nStored ({len(stored)}): {', '.join(stored)}")
            self.stdout.write('Dry run — nothing changed.')
            return

        result = pipeline.republish_project_layers(project_id)
        published = result['published']
        self.stdout.write(f"\nRepublished {len(published)} layer(s):")
        for name, count in sorted(published.items()):
            self.stdout.write(f"  {name:<20} {count}")
        removed = result['removed']
        if removed:
            self.stdout.write(
                self.style.WARNING(
                    f"Removed {len(removed)} layer(s) the run no longer has: "
                    f"{', '.join(sorted(removed))}"
                )
            )
        self.stdout.write(
            self.style.SUCCESS(
                f'Done. The post-HLD chain (road class, trench sections, permit '
                f'matrix, package) re-runs on the next status poll because the '
                f'trench content revision changed.'
            )
        )
