"""Trench-design endpoints (Phase A of the civil trench designer).

The designer itself lives in the engine
(``HLD_Planning_01/HLDPlanning/design/trench_design.py``,
``HLD_Planning_01/web/backend/design.py``); these views proxy it for the
platform, which renders the result on ``fiber-fe/ftth-trench-design.html``.

    GET  /api/ftth/hld/results/<project_id>/trench-design/       payload
    POST /api/ftth/hld/results/<project_id>/trench-design/run/   (re)run
"""

from django.http import JsonResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from .pipeline import get_trench_design, run_trench_design


class TrenchDesignView(APIView):
    """Designed civil trench network for a project (status + layers)."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        include = request.query_params.get('layers', 'true').lower() != 'false'
        data = get_trench_design(project_id, include_layers=include)
        if data is None:
            return JsonResponse(
                {'detail': 'Trench design service unavailable.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return JsonResponse(data)


class RunTrenchDesignView(APIView):
    """Start (or re-run) the trench designer for a project."""

    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        force = str(request.data.get('force', '')).lower() in ('1', 'true', 'yes')
        data = run_trench_design(project_id, force=force)
        if data is None:
            return JsonResponse(
                {'detail': 'Trench design service unavailable.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return JsonResponse(data, status=status.HTTP_202_ACCEPTED)
