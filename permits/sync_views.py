"""Sync views (P20b) — read-only poll + inbound webhook.

GET  /api/ftth/permits/sync/status/        — which adapters are enabled + last poll hint
POST /api/ftth/permits/sync/poll/          — trigger one poll cycle (advisory, JWT, optional ?project_id=&dry_run=1&adapter=mock_bezirk)
POST /api/ftth/permits/submissions/sync/   — inbound webhook: {submission_id, status, reference?, notes?, conditions?, expiry_date?, sync_source?}

All mutations go through transition_submission with a sync_source, so the
audit trail is honest. The webhook is JWT-required like the rest of permits.
"""

from __future__ import annotations

from django.http import JsonResponse
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from .models import PermitSubmission
from .sync import ADAPTERS, enabled_adapters, ingest_csv, ingest_inbound, poll_all


class PermitSyncStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        adapters = []
        for name, adapter in ADAPTERS.items():
            adapters.append(
                {'name': name, 'description': adapter.description, 'enabled': adapter.is_enabled()}
            )
        enabled = [a.name for a in enabled_adapters()]
        non_terminal = PermitSubmission.objects.filter(
            status__in=(
                PermitSubmission.STATUS_SUBMITTED,
                PermitSubmission.STATUS_UNDER_REVIEW,
                PermitSubmission.STATUS_REJECTED,
            )
        ).count()
        return JsonResponse(
            {
                'now': timezone.now().isoformat(),
                'adapters': adapters,
                'enabled': enabled,
                'non_terminal_submissions': non_terminal,
                'hint': 'POST /api/ftth/permits/sync/poll/ to run one poll cycle; POST /api/ftth/permits/submissions/sync/ for inbound webhook (or POST .../submissions/sync/csv/ with a CSV body).',
            }
        )


class PermitSyncPollView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        # Also accept query params for convenience (dry_run via ?dry_run=1)
        q = request.GET
        project_id = (data.get('project_id') or q.get('project_id') or '').strip() or None
        dry_run = str(data.get('dry_run') or q.get('dry_run') or '').strip().lower() in (
            '1',
            'true',
            'yes',
        )
        adapter_param = (
            data.get('adapter') or data.get('adapters') or q.get('adapter') or ''
        ).strip()
        adapter_names = (
            [s.strip() for s in adapter_param.split(',') if s.strip()] if adapter_param else None
        )
        limit_raw = (data.get('limit') or q.get('limit') or '').strip()
        try:
            limit = int(limit_raw) if limit_raw else None
        except ValueError:
            return JsonResponse({'detail': 'limit must be an integer.'}, status=400)
        if limit is not None:
            limit = max(1, min(limit, 5000))
        result = poll_all(
            project_id=project_id, adapter_names=adapter_names, dry_run=dry_run, limit=limit
        )
        return JsonResponse(result)


class PermitSubmissionSyncView(APIView):
    """Inbound webhook for a single submission — drives one transition.

    Body: {submission_id (or id), status, reference?, notes?, conditions?, expiry_date?, sync_source?}
    submission_id can also be supplied as /submissions/<id>/sync/ but we keep
    the collection route for batch-friendly callers.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, submission_id: str | None = None):
        data = request.data or {}
        sid = (submission_id or data.get('submission_id') or data.get('id') or '').strip()
        to_status = (data.get('status') or '').strip().lower()
        if not sid:
            return JsonResponse({'detail': 'submission_id is required.'}, status=400)
        if not to_status:
            return JsonResponse({'detail': 'status is required.'}, status=400)
        sub = PermitSubmission.objects.filter(pk=sid).first()
        if sub is None:
            return JsonResponse({'detail': 'Submission not found.'}, status=404)
        sync_source = (
            data.get('sync_source') or data.get('source') or 'portal'
        ).strip().lower() or 'portal'
        try:
            ingest_inbound(
                sub,
                to_status,
                reference=data.get('reference'),
                notes=data.get('notes') or data.get('reason'),
                conditions=data.get('conditions'),
                expiry_date=data.get('expiry_date') or data.get('expiry'),
                sync_source=sync_source,
            )
        except ValueError as exc:
            return JsonResponse({'detail': str(exc)}, status=400)
        sub.refresh_from_db()
        from .views import _serialize_submission

        return JsonResponse(_serialize_submission(sub))


class PermitSubmissionCsvSyncView(APIView):
    """CSV inbox — POST text/csv or application/json with {csv: "..."}."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Accept raw CSV body (content-type text/csv) or JSON {csv: "..."}
        data = request.data or {}
        text = ''
        if isinstance(data, dict) and isinstance(data.get('csv'), str):
            text = data['csv']
        elif isinstance(data, str):
            text = data
        else:
            # Try raw body
            try:
                text = request.body.decode('utf-8') if request.body else ''
                # If it's JSON with csv field, use that
                if text.strip().startswith('{'):
                    import json as _json

                    parsed = _json.loads(text)
                    if isinstance(parsed.get('csv'), str):
                        text = parsed['csv']
            except Exception:
                text = ''
        if not text.strip():
            return JsonResponse(
                {
                    'detail': 'CSV body is required. Send text/csv or JSON {csv: "..."} with columns submission_id,status,reference,notes,conditions,expiry_date.'
                },
                status=400,
            )
        sync_source = (
            request.GET.get('sync_source') or data.get('sync_source') or 'portal'
        ).strip().lower() or 'portal'
        result = ingest_csv(text, sync_source=sync_source)
        status = 207 if result['errors'] else 200
        return JsonResponse(result, status=status)
