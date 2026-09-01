"""Backfill street-level permit grouping for existing projects.

Stamps ``street_name`` onto trench properties (same pass as the fclass
backfill) and assigns ``permit_group`` on the matrix rows so the tracker and
permit-package forms present one permit per street. Idempotent: re-running
only fills what is still blank.

Run::

    python manage.py assign_permit_groups                      # all projects
    python manage.py assign_permit_groups --project <id>       # one project
"""

from django.core.management.base import BaseCommand

from ...analysis.grouping import assign_groups
from ...analysis.road_class import attribute_road_class


class Command(BaseCommand):
    help = "Backfill street-level permit_group on matrix rows (street name + grouping)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--project", help="project_id to process (default: every project)"
        )

    def handle(self, *args, **opts):
        from ftth_hld.models import FtthProject

        projects = FtthProject.objects.all()
        if opts["project"]:
            projects = projects.filter(pk=opts["project"])
        for ftth in projects:
            pid = str(ftth.pk)
            road = attribute_road_class(pid)
            grp = assign_groups(pid)
            self.stdout.write(
                self.style.SUCCESS(
                    f"{ftth.name or pid}: {road.get('attributed', 0)} trenches "
                    f"attributed · {grp['trench_rows']} trench rows + "
                    f"{grp['lld_rows']} LLD rows grouped"
                    + (" (no roads file)" if grp.get("no_roads") else "")
                )
            )
