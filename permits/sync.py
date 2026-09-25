"""
Status-sync poller framework (P20b) — pluggable adapters + inbound webhook.

Design
------
* Every poller adapter implements `poll(submission) -> SyncResult | None` — a
  small value that, when present, says "the authority's portal thinks this
  submission should now be <status>". The framework walks every non-terminal
  submission, asks each enabled adapter, and applies the first non-None result
  via the single state machine `transition_submission(..., sync_source=adapter)`.

* Adapters are registered in ADAPTERS and enabled via settings or explicitly
  passed to `poll_all(...)`. The built-in mock adapter ("mock_bezirk") is
  always available and useful for demos / tests — it deterministically moves
  a submission along after N days since submission_date, without any network.

* Inbound sync (webhook / email / CSV) reuses the same inbound path:
  `ingest_inbound(status, reference?, notes?, conditions?, expiry_date?)`
  is the shared code for POST .../submissions/sync/ (webhook) and for the
  CSV import that the management command can read. Both land on
  `transition_submission` so the audit trail stays honest (sync_source).

Cross-project by default; --project filters to one. Never touches
PermitMatrix except through the submission transition (the row mirror there).

Adapters
--------
* mock_bezirk — time-based demo: submitted (0-1d) -> under_review, then
  under_review (2-4d) -> approved (with synthetic expiry + conditions).
* csv_inbox   — reads a CSV file (submission_id,status,reference,notes,
  conditions,expiry_date) and applies each line. Useful for email/portal
  exports without an API.
* http_portal — placeholder for a real portal API (fetch status by reference
  and map it onto our state machine). Disabled until a base URL + token are
  configured (PERMITS_PORTAL_*). Returns None until then.

No DB mutation outside transition_submission. No LLM.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.utils import timezone

from .models import PermitSubmission
from .submissions import ALLOWED_TRANSITIONS, transition_submission

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncResult:
    to_status: str
    reference: str | None = None
    notes: str | None = None
    conditions: str | None = None
    expiry_date: Any | None = None
    detail: dict[str, Any] | None = None  # adapter-specific trace


# ----- Adapter interface ---------------------------------------------------


class SyncAdapter:
    """Base class — subclass and implement poll()."""

    name: str = "base"
    description: str = ""

    def poll(self, submission: PermitSubmission) -> SyncResult | None:
        """Return a SyncResult if the adapter wants to move this submission,
        or None if it has no opinion / cannot reach the portal."""
        raise NotImplementedError

    def is_enabled(self) -> bool:
        return True


# ----- Built-in adapters ---------------------------------------------------


class MockBezirkAdapter(SyncAdapter):
    """Deterministic time-based mock for demos and tests.

    Rules (all dates in UTC, based on submission_date):
      submitted, age >= 1d  -> under_review
      under_review, age >= 3d -> approved (expiry = now + 365d, synthetic conditions)
    Terminal states (approved/rejected/closed) are never polled.
    """

    name = "mock_bezirk"
    description = "Time-based mock: 1d -> under_review, 3d -> approved (demo only)."

    def poll(self, submission: PermitSubmission) -> SyncResult | None:
        now = timezone.now()
        age = (now - submission.submission_date).total_seconds() / 86400 if submission.submission_date else 999
        if submission.status == PermitSubmission.STATUS_SUBMITTED and age >= 1:
            return SyncResult(
                to_status=PermitSubmission.STATUS_UNDER_REVIEW,
                reference=submission.reference or f"MOCK-{submission.id.hex[:8].upper()}",
                notes="Mock adapter: auto-acknowledged by Bezirk (demo).",
                detail={"adapter": self.name, "age_days": round(age, 1)},
            )
        if submission.status == PermitSubmission.STATUS_UNDER_REVIEW and age >= 3:
            # Synthetic approval: expiry 365d out, street-specific conditions where possible
            expiry = (now + timedelta(days=365)).date().isoformat()
            return SyncResult(
                to_status=PermitSubmission.STATUS_APPROVED,
                conditions="Mock approval: work 22:00-06:00, reinstatement per ZTV A-StB (demo).",
                expiry_date=expiry,
                detail={"adapter": self.name, "age_days": round(age, 1)},
            )
        return None


class CsvInboxAdapter(SyncAdapter):
    """CSV-driven inbox — not a poller, but exposed as an adapter so the
    management command can drive it uniformly. poll() always returns None;
    ingest is via ingest_csv()."""

    name = "csv_inbox"
    description = "CSV inbox: submission_id,status,reference,notes,conditions,expiry_date"

    def poll(self, submission: PermitSubmission) -> SyncResult | None:
        return None


class HttpPortalAdapter(SyncAdapter):
    """Real portal API — disabled until PERMITS_PORTAL_* env is configured.

    When configured, fetches GET {PERMITS_PORTAL_URL}/status?reference=...
    and maps the remote status string onto our state machine via STATUS_MAP.
    """

    name = "http_portal"
    description = "HTTP portal: maps remote status onto transition_submission (requires env)."

    STATUS_MAP: dict[str, str] = {
        "acknowledged": PermitSubmission.STATUS_UNDER_REVIEW,
        "under_review": PermitSubmission.STATUS_UNDER_REVIEW,
        "in_progress": PermitSubmission.STATUS_UNDER_REVIEW,
        "approved": PermitSubmission.STATUS_APPROVED,
        "granted": PermitSubmission.STATUS_APPROVED,
        "rejected": PermitSubmission.STATUS_REJECTED,
        "denied": PermitSubmission.STATUS_REJECTED,
        "closed": PermitSubmission.STATUS_CLOSED,
        "completed": PermitSubmission.STATUS_CLOSED,
    }

    def is_enabled(self) -> bool:
        import os

        return bool((os.getenv("PERMITS_PORTAL_URL") or "").strip())

    def poll(self, submission: PermitSubmission) -> SyncResult | None:
        import os

        import requests  # already in requirements

        base = (os.getenv("PERMITS_PORTAL_URL") or "").strip()
        token = (os.getenv("PERMITS_PORTAL_TOKEN") or "").strip()
        if not base or not submission.reference:
            return None
        # Never fail the poll loop — portal downtime is recorded, not raised.
        try:
            headers = {"Accept": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            # Also try X-API-Key for portals that use it
            alt_token = (os.getenv("PERMITS_PORTAL_API_KEY") or "").strip()
            if alt_token:
                headers["X-API-Key"] = alt_token
            url = base.rstrip("/") + "/status"
            resp = requests.get(url, params={"reference": submission.reference}, headers=headers, timeout=12)
            if resp.status_code < 200 or resp.status_code >= 300:
                logger.info("http_portal: non-2xx for %s — %s %s", submission.reference, resp.status_code, resp.text[:300])
                return None
            data = resp.json() if "json" in (resp.headers.get("content-type") or "").lower() else {}
            remote_status = str((data.get("status") or data.get("state") or "")).strip().lower()
            if not remote_status:
                return None
            to_status = self.STATUS_MAP.get(remote_status)
            if not to_status:
                logger.info("http_portal: unknown remote status %r for %s", remote_status, submission.reference)
                return None
            # Only return a result if it is a legal edge from current status
            if to_status not in ALLOWED_TRANSITIONS.get(submission.status, frozenset()):
                return None
            return SyncResult(
                to_status=to_status,
                reference=data.get("reference") or None,
                notes=data.get("notes") or data.get("reason") or None,
                conditions=data.get("conditions") or None,
                expiry_date=data.get("expiry_date") or data.get("expiry") or None,
                detail={"adapter": self.name, "remote_status": remote_status, "raw": {k: str(v)[:300] for k, v in data.items()}},
            )
        except Exception as exc:  # pragma: no cover — network failures are normal
            logger.warning("http_portal poll failed for %s: %s", submission.reference, exc)
            return None


ADAPTERS: dict[str, SyncAdapter] = {
    MockBezirkAdapter.name: MockBezirkAdapter(),
    CsvInboxAdapter.name: CsvInboxAdapter(),
    HttpPortalAdapter.name: HttpPortalAdapter(),
}


def enabled_adapters(names: list[str] | None = None) -> list[SyncAdapter]:
    """Return the adapters to run for this invocation."""
    if names is not None:
        out: list[SyncAdapter] = []
        for n in names:
            a = ADAPTERS.get(n)
            if a:
                out.append(a)
        return out
    # Default enabled set: mock_bezirk always; http_portal only when configured
    out = [ADAPTERS["mock_bezirk"]]
    if ADAPTERS["http_portal"].is_enabled():
        out.append(ADAPTERS["http_portal"])
    return out


def poll_all(
    project_id: str | None = None,
    adapter_names: list[str] | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Walk non-terminal submissions and apply the first adapter opinion.

    Returns a summary dict for the management command / API response.
    """
    adapters = enabled_adapters(adapter_names)
    now = timezone.now()

    qs = PermitSubmission.objects.filter(status__in=(
        PermitSubmission.STATUS_SUBMITTED,
        PermitSubmission.STATUS_UNDER_REVIEW,
        PermitSubmission.STATUS_REJECTED,
    )).select_related("authority", "project").order_by("submission_date")
    if project_id:
        qs = qs.filter(project_id=project_id)

    total = qs.count()
    rows = list(qs[:limit] if limit else qs)

    attempted = 0
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    # Optional actor label for audit (e.g. "poller:mock_bezirk")
    from django.contrib.auth import get_user_model  # type: ignore

    for sub in rows:
        opinion: SyncResult | None = None
        chosen_adapter: SyncAdapter | None = None
        for adapter in adapters:
            if not adapter.is_enabled():
                continue
            try:
                opinion = adapter.poll(sub)
            except Exception as exc:
                errors.append({"submission_id": str(sub.id), "adapter": adapter.name, "error": str(exc)[:300]})
                continue
            if opinion is not None:
                chosen_adapter = adapter
                break
        if opinion is None:
            skipped.append({"submission_id": str(sub.id), "status": sub.status, "reference": sub.reference or ""})
            continue
        attempted += 1
        # Validate the edge before calling transition_submission so the error
        # message is poller-specific, not just the state machine's ValueError.
        if opinion.to_status not in ALLOWED_TRANSITIONS.get(sub.status, frozenset()):
            errors.append({"submission_id": str(sub.id), "adapter": chosen_adapter.name if chosen_adapter else "?", "error": f"Adapter proposed illegal edge {sub.status} -> {opinion.to_status}"})
            continue
        before_status = sub.status
        if dry_run:
            applied.append({
                "submission_id": str(sub.id),
                "project_id": str(sub.project_id),
                "from": before_status,
                "to": opinion.to_status,
                "adapter": chosen_adapter.name if chosen_adapter else "?",
                "reference": opinion.reference or sub.reference or "",
                "dry_run": True,
                "detail": opinion.detail or {},
            })
            continue
        try:
            transition_submission(
                sub,
                opinion.to_status,
                user=None,
                reference=opinion.reference,
                notes=opinion.notes,
                conditions=opinion.conditions,
                expiry_date=opinion.expiry_date,
                sync_source=chosen_adapter.name if chosen_adapter else "portal",
            )
            applied.append({
                "submission_id": str(sub.id),
                "project_id": str(sub.project_id),
                "from": before_status,
                "to": opinion.to_status,
                "adapter": chosen_adapter.name if chosen_adapter else "?",
                "reference": opinion.reference or sub.reference or "",
                "detail": opinion.detail or {},
            })
        except Exception as exc:  # pragma: no cover
            errors.append({"submission_id": str(sub.id), "adapter": chosen_adapter.name if chosen_adapter else "?", "error": str(exc)[:500]})
            logger.warning("poll transition failed for %s: %s", sub.id, exc)

    return {
        "now": now.isoformat(),
        "project_id": project_id,
        "adapters": [a.name for a in adapters],
        "total_non_terminal": total,
        "scanned": len(rows),
        "attempted": attempted,
        "applied": applied,
        "skipped": skipped[:50],
        "errors": errors,
        "dry_run": dry_run,
    }


def ingest_inbound(
    submission: PermitSubmission,
    to_status: str,
    *,
    reference: str | None = None,
    notes: str | None = None,
    conditions: str | None = None,
    expiry_date: Any = None,
    sync_source: str = "portal",
) -> PermitSubmission:
    """Shared inbound path for webhook + CSV + manual inbox.

    Validates the edge and calls transition_submission with the supplied
    sync_source so the audit trail records how the state arrived.
    """
    if to_status not in ALLOWED_TRANSITIONS:
        raise ValueError(f"Unknown submission status: {to_status}")
    if to_status not in ALLOWED_TRANSITIONS.get(submission.status, frozenset()):
        raise ValueError(f"Invalid transition {submission.status} -> {to_status} (allowed: {sorted(ALLOWED_TRANSITIONS.get(submission.status, frozenset())) or 'none'})")
    return transition_submission(
        submission,
        to_status,
        user=None,
        reference=reference,
        notes=notes,
        conditions=conditions,
        expiry_date=expiry_date,
        sync_source=sync_source,
    )


def ingest_csv(text: str, sync_source: str = "portal") -> dict[str, Any]:
    """Apply a CSV inbox file. Columns:

    submission_id,status,reference,notes,conditions,expiry_date

    Only submission_id + status are required; the rest are optional.
    Returns a summary dict.
    """
    reader = csv.DictReader(io.StringIO(text))
    applied: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for idx, row in enumerate(reader, start=2):
        sid = (row.get("submission_id") or row.get("id") or "").strip()
        to_status = (row.get("status") or "").strip().lower()
        if not sid or not to_status:
            errors.append({"line": idx, "error": "submission_id and status are required"})
            continue
        sub = PermitSubmission.objects.filter(pk=sid).first()
        if sub is None:
            errors.append({"line": idx, "submission_id": sid, "error": "Submission not found"})
            continue
        try:
            ingest_inbound(
                sub,
                to_status,
                reference=(row.get("reference") or None),
                notes=(row.get("notes") or row.get("reason") or None),
                conditions=(row.get("conditions") or None),
                expiry_date=(row.get("expiry_date") or row.get("expiry") or None),
                sync_source=sync_source,
            )
            applied.append({"line": idx, "submission_id": sid, "to": to_status})
        except Exception as exc:
            errors.append({"line": idx, "submission_id": sid, "error": str(exc)[:500]})
    return {"applied": applied, "errors": errors, "total": len(applied) + len(errors)}
