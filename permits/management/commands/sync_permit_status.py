"""Status-sync poller command (P20b).

python manage.py sync_permit_status [--project <id>] [--adapter mock_bezirk]
                                    [--dry-run] [--limit 100] [--csv file.csv]
                                    [--json]

Walks every non-terminal submission and asks each enabled adapter for an
opinion. The first adapter that has one drives transition_submission with
sync_source=adapter name (so the audit trail is honest).

--csv file.csv instead drives ingest_csv (no polling), useful for
portal/email exports.

Exit codes: 0 = no errors, 1 = some errors, 2 = all applied (for cron alerting).
"""

from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand

from permits.sync import ADAPTERS, enabled_adapters, ingest_csv, poll_all


class Command(BaseCommand):
    help = "Poll authority portals (or ingest a CSV) and sync submission statuses (P20b)."

    def add_arguments(self, parser):
        parser.add_argument("--project", dest="project_id", default=None, help="Limit to one project_id")
        parser.add_argument("--adapter", dest="adapter", default=None, help="Comma-separated adapters (mock_bezirk,http_portal,csv_inbox) or omit for default enabled set")
        parser.add_argument("--dry-run", action="store_true", help="Do not mutate — just report what would happen")
        parser.add_argument("--limit", type=int, default=None, help="Max submissions to scan")
        parser.add_argument("--csv", dest="csv_path", default=None, help="Path to a CSV file with columns submission_id,status,reference,notes,conditions,expiry_date — ingests instead of polling")
        parser.add_argument("--json", action="store_true", help="Emit JSON instead of a text table")
        parser.add_argument("--sync-source", default="portal", help="sync_source for CSV ingest (default: portal)")

    def handle(self, *args, **options):
        project_id = options.get("project_id")
        adapter_raw = options.get("adapter")
        adapter_names = [s.strip() for s in adapter_raw.split(",") if s.strip()] if adapter_raw else None
        dry_run = bool(options.get("dry_run"))
        limit = options.get("limit")
        csv_path = options.get("csv_path")
        as_json = bool(options.get("json"))
        sync_source = options.get("sync_source") or "portal"

        if csv_path:
            # CSV ingest mode — no polling
            import pathlib

            p = pathlib.Path(csv_path)
            if not p.exists():
                self.stderr.write(f"CSV not found: {csv_path}")
                sys.exit(1)
            text = p.read_text(encoding="utf-8")
            result = ingest_csv(text, sync_source=sync_source)
            if as_json:
                self.stdout.write(json.dumps(result, indent=2, default=str))
            else:
                self._print_csv(result)
            sys.exit(1 if result["errors"] else 0)

        # Poll mode
        result = poll_all(project_id=project_id, adapter_names=adapter_names, dry_run=dry_run, limit=limit)

        if as_json:
            self.stdout.write(json.dumps(result, indent=2, default=str))
        else:
            self._print_poll(result, adapter_names)

        if result["errors"]:
            sys.exit(1)
        if result["applied"]:
            sys.exit(0)
        # No errors, nothing to apply — all clear
        sys.exit(0)

    def _print_poll(self, data: dict, adapter_names):
        self.stdout.write(f"Permit status sync — adapters {', '.join(data['adapters'])} — {data['now']}")
        if data["project_id"]:
            self.stdout.write(f"  Project: {data['project_id']}")
        self.stdout.write(f"  Non-terminal: {data['total_non_terminal']}  Scanned: {data['scanned']}  Attempted: {data['attempted']}")
        if data["dry_run"]:
            self.stdout.write("  DRY RUN — no mutations.")
        if not data["applied"] and not data["errors"]:
            self.stdout.write("  Nothing to sync — all submissions are terminal or adapters had no opinion.")
            return
        if data["applied"]:
            self.stdout.write(f"  Applied {len(data['applied'])} transition(s):")
            for r in data["applied"]:
                self.stdout.write(f"    {r['submission_id'][:8]}  {r.get('from','?')} -> {r['to']}  via {r['adapter']}  ref {r.get('reference','') or '-'}")
        if data["errors"]:
            self.stdout.write(f"  Errors {len(data['errors'])}:")
            for e in data["errors"][:20]:
                self.stdout.write(f"    {e.get('submission_id','?')[:8]}  {e.get('adapter','?')}: {e.get('error','')}")
        if len(data.get("skipped") or []) > 0 and data["total_non_terminal"] > len(data["applied"]) + len(data["errors"]):
            self.stdout.write(f"  Skipped {len(data['skipped'])} (no opinion). Use --json for full list.")

    def _print_csv(self, data: dict):
        self.stdout.write(f"CSV ingest — total {data['total']} rows — applied {len(data['applied'])} — errors {len(data['errors'])}")
        for r in data["applied"][:20]:
            self.stdout.write(f"  ok  line {r['line']}: {r['submission_id'][:8]} -> {r['to']}")
        for e in data["errors"][:20]:
            self.stdout.write(f"  err line {e['line']}: {e.get('submission_id','?')[:8]} — {e.get('error','')}")
