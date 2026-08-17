"""Backfill road classification (``fclass``) onto HLD trench segments.

For every existing HLD project (or ``--project``), reads the project's roads
input file from disk and persists the nearest road's ``fclass``/``highway``
onto each ``gis.trench_layer`` segment's ``properties``. Optionally re-runs
the permit analysis afterwards so the ``ROAD_AUTHORITY_001`` rule fires.

Run::

    python manage.py backfill_trench_fclass               # all projects
    python manage.py backfill_trench_fclass --project 39ec0d866dba4e5e80a4b9e7f4101953
    python manage.py backfill_trench_fclass --analyze     # + run permit analysis
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import connection

from permits.analysis.road_class import attribute_road_class, project_roads_file


class Command(BaseCommand):
    help = "Backfill road fclass onto HLD trench segments from each project's roads input."

    def add_arguments(self, parser):
        parser.add_argument("--project", help="Only backfill this project_id")
        parser.add_argument("--analyze", action="store_true",
                            help="Run the permit analysis after backfilling")

    def handle(self, *args, **opts):
        with connection.cursor() as cur:
            if opts["project"]:
                cur.execute(
                    "SELECT DISTINCT project_id FROM gis.trench_layer "
                    "WHERE project_id = %s", [opts["project"]]
                )
            else:
                cur.execute(
                    "SELECT DISTINCT project_id FROM gis.trench_layer "
                    "ORDER BY project_id"
                )
            project_ids = [r[0] for r in cur.fetchall()]

        if not project_ids:
            self.stdout.write(self.style.WARNING("No projects with trench rows found."))
            return

        total_attributed = 0
        for project_id in project_ids:
            roads = project_roads_file(project_id)
            summary = attribute_road_class(project_id, roads)
            total_attributed += summary.get("attributed", 0)
            self.stdout.write(
                f"  {project_id[:12]}… trenches={summary['trenches']} "
                f"roads={summary['roads']} attributed={summary['attributed']}"
                + (f"  ({summary.get('error')})" if summary.get("error") else "")
            )
        self.stdout.write(self.style.SUCCESS(f"Total trench segments attributed: {total_attributed}"))

        if opts["analyze"]:
            from permits.rules.engine import run_analysis
            for project_id in project_ids:
                summary = run_analysis(project_id)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Analysis {project_id[:12]}…: "
                        f"{len(summary.get('rules_fired', []))} rules fired, "
                        f"{summary.get('rows_created', 0)} rows"
                    )
                )
