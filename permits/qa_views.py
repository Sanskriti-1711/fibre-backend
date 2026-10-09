"""QA views (P19b) — read-only full-package audit.

GET /api/ftth/permits/qa/                  — cross-project QA summary
GET /api/ftth/permits/projects/<pid>/qa/   — one-project QA audit (deterministic + optional AI paragraph)

Never mutates PermitMatrix / PermitDocument. JWT-required. The package QA is
deterministic (expected vs actual counts by kind, HDD guard, naming, readiness)
and useful offline; when PERMITS_LLM_* is configured an AI paragraph is
appended (PermitAiDraft is NOT written — QA is not Copilot drafting).
"""

from __future__ import annotations

from django.http import JsonResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from ftth_hld.models import FtthProject

from .qa import qa_for_project, qa_summary


class PermitQaView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id: str | None = None):
        pid = project_id or (request.GET.get('project_id') or '').strip() or None
        # When project_id is supplied on the collection route via ?project_id=
        # respect it — mirrors the expiry view's convenience.
        try:
            if pid and not FtthProject.objects.filter(pk=pid).exists():
                return JsonResponse({'detail': 'Project not found.'}, status=404)
        except Exception:
            pass
        if pid:
            try:
                data = qa_for_project(pid)
                return JsonResponse(data)
            except Exception as exc:  # pragma: no cover
                return JsonResponse({'detail': f'QA failed: {exc}'}, status=500)
        try:
            data = qa_summary(project_id=None)
            return JsonResponse(data)
        except Exception as exc:  # pragma: no cover
            return JsonResponse({'detail': f'QA summary failed: {exc}'}, status=500)
