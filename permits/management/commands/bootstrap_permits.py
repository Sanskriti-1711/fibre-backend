"""Bring a project's permit data up to the point where permits can be generated.

Generating a permit package needs four things on disk/db that no single command
prepares, and the ORDER matters:

  1. seed_permit_authorities      — the authority registry (who to submit to)
  2. load_osm_reference_layers    — railway/waterway/env/tree/admin boundaries
                                    the crossing rules read (optional per area)
  3. backfill_trench_fclass       — road class per trench segment, which the
                                    road-authority and permit-type rules read
  4. assign_permit_groups         — street-level grouping on the matrix rows

Skipping (3) is the quiet failure: authority mapping and road-class detection
then match nothing, so permits come out unmapped rather than obviously broken.

This command runs them in that order with one entry point. Every step is
idempotent, so re-running is safe, and ``--dry-run`` reports what would run and
the row counts as they stand without changing anything.
"""

from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Prepare a project's permit inputs in the right order (authorities, OSM "
        "reference layers, trench road class, permit groups). Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--project", help="project_id to scope the trench/permit-group steps to"
        )
        parser.add_argument(
            "--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"),
            help="load the OSM reference layers for this bbox (omitted = skip them)",
        )
        parser.add_argument(
            "--layer", action="append", dest="layers",
            help="restrict the OSM load to this reference layer (repeatable)",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="re-fetch the OSM reference layers even if already loaded",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="report the plan and current counts, change nothing",
        )

    # ------------------------------------------------------------------

    def _counts(self):
        """Current state of the four things the chain prepares."""
        from permits.models import PermitAuthority, PermitMatrix

        counts = {
            "authorities": PermitAuthority.objects.count(),
            "permit_matrix_rows": PermitMatrix.objects.count(),
        }
        matrix = getattr(PermitMatrix, "objects", None)
        if matrix is not None:
            grouped = PermitMatrix.objects.exclude(permit_group__isnull=True) \
                .exclude(permit_group="").count()
            counts["matrix_rows_with_group"] = grouped
        return counts

    def handle(self, *args, **options):
        project = options.get("project")
        dry_run = options.get("dry_run")
        bbox = options.get("bbox")

        if dry_run:
            self.stdout.write("bootstrap_permits --dry-run")
            for line in self._plan(project, bbox, options.get("layers")):
                self.stdout.write(f"  would run: {line}")
            try:
                for key, value in self._counts().items():
                    self.stdout.write(f"  currently: {key} = {value}")
            except Exception as exc:  # noqa: BLE001 - reporting only
                self.stdout.write(f"  could not read counts ({type(exc).__name__}: {exc})")
            return

        for label, fn in self._steps(project, bbox, options.get("layers"),
                                     options.get("force")):
            self.stdout.write(f"[bootstrap] {label} ...")
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                # A step that cannot run must not silently leave the chain
                # looking finished: say which one broke and stop.
                raise CommandError(f"{label} failed: {type(exc).__name__}: {exc}") from exc

        self.stdout.write(self.style.SUCCESS("[bootstrap] permit inputs prepared."))
        try:
            for key, value in self._counts().items():
                self.stdout.write(f"  {key} = {value}")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------

    def _plan(self, project, bbox, layers):
        plan = ["seed_permit_authorities"]
        if bbox:
            scope = f" (bbox {bbox})" if bbox else ""
            extra = f", layers={layers}" if layers else ""
            plan.append(f"load_osm_reference_layers{scope}{extra}")
        else:
            plan.append("load_osm_reference_layers  [skipped — pass --bbox to run it]")
        plan.append(
            "backfill_trench_fclass"
            + (f" --project {project}" if project else " (all projects)")
        )
        plan.append(
            "assign_permit_groups"
            + (f" --project {project}" if project else " (all projects)")
        )
        return plan

    def _steps(self, project, bbox, layers, force):
        def _seed_authorities():
            call_command("seed_permit_authorities", verbosity=0)

        def _load_osm():
            kwargs = {"bbox": bbox}
            if layers:
                # The loader takes one --layer per invocation.
                for layer in layers:
                    call_command("load_osm_reference_layers", layer=layer,
                                 force=force, verbosity=0)
                return
            call_command("load_osm_reference_layers", force=force, verbosity=0, **kwargs)

        def _backfill_fclass():
            call_command("backfill_trench_fclass",
                         project=project, verbosity=0)

        def _assign_groups():
            call_command("assign_permit_groups",
                         project=project, verbosity=0)

        steps = [("seed permit authorities", _seed_authorities)]
        if bbox or layers:
            steps.append(("load OSM reference layers", _load_osm))
        steps.append(("backfill trench road class", _backfill_fclass))
        steps.append(("assign street-level permit groups", _assign_groups))
        return steps
