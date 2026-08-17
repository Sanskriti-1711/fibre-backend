"""API views for the FTTH Permit system (Phase 1).

Endpoints (all under ``/api/ftth/permits/``, JWT-authenticated):

* ``GET  /projects/<pid>/permits/``      — permit matrix for a project
* ``POST /projects/<pid>/permits/analyze/`` — (re)run the rule engine
* ``PATCH /permits/<permit_id>/``         — status/dates/conditions/comments
* ``GET  /summary/``                      — cross-project dashboard KPI
"""

from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from django.db import models as dj_models
from django.http import JsonResponse
from django.utils import timezone

from ftth_hld.models import FtthProject

from .models import PermitEvent, PermitMatrix
from .rules.engine import project_summary, run_analysis


def _serialize(permit: PermitMatrix) -> dict:
    return {
        "permit_id": str(permit.permit_id),
        "project_id": permit.project_id,
        "route_section": permit.route_section,
        "layer": permit.layer,
        "permit_type": permit.permit_type,
        "municipality": permit.municipality,
        "authority": {
            "code": permit.authority.code if permit.authority else None,
            "name": permit.authority.name if permit.authority else None,
            "type": permit.authority.authority_type if permit.authority else None,
        },
        "rule": {
            "rule_id": permit.rule.rule_id if permit.rule else None,
            "version": permit.rule_version,
        },
        "required": permit.required,
        "blocks_construction": permit.blocks_construction,
        "status": permit.status,
        "readiness_pct": permit.readiness_pct,
        "evidence": permit.evidence,
        "documents": permit.documents,
        "analysis_notes": permit.analysis_notes,
        "submission_date": permit.submission_date.isoformat() if permit.submission_date else None,
        "approval_date": permit.approval_date.isoformat() if permit.approval_date else None,
        "expiry_date": permit.expiry_date.isoformat() if permit.expiry_date else None,
        "conditions": permit.conditions,
        "revision": permit.revision,
        "comments": permit.comments,
        "created_at": permit.created_at.isoformat() if permit.created_at else None,
    }


class PermitMatrixView(APIView):
    """GET /api/ftth/permits/projects/<pid>/permits/ — matrix for a project."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        status_filter = request.GET.get("status")
        qs = PermitMatrix.objects.filter(project_id=project_id).select_related(
            "authority", "rule"
        )
        if status_filter:
            qs = qs.filter(status=status_filter)
        # A project's matrix can hold thousands of rows (one per route feature
        # per rule) — the HLD/LLD maps colour every segment, so don't truncate.
        permits = [_serialize(pm) for pm in qs[:20000]]
        return JsonResponse({
            **project_summary(project_id),
            "permits": permits,
        })


class PermitAnalyzeView(APIView):
    """POST /api/ftth/permits/projects/<pid>/permits/analyze/ — run rules."""

    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        if not FtthProject.objects.filter(pk=project_id).exists():
            return JsonResponse(
                {"detail": "Project not found."}, status=404
            )
        summary = run_analysis(project_id, user=request.user)
        return JsonResponse(summary)


class PermitDetailView(APIView):
    """PATCH /api/ftth/permits/<permit_id>/ — review updates.

    Allowed fields: status, conditions, comments, municipality,
    submission_date, approval_date, expiry_date, evidence, documents.
    Date fields accept ISO strings.
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request, permit_id):
        try:
            permit = PermitMatrix.objects.get(pk=permit_id)
        except PermitMatrix.DoesNotExist:
            return JsonResponse({"detail": "Permit not found."}, status=404)

        data = request.data or {}
        simple_fields = (
            "status", "conditions", "comments", "municipality",
            "evidence", "documents", "analysis_notes",
        )
        updated = []
        for field in simple_fields:
            if field in data:
                setattr(permit, field, data[field])
                updated.append(field)

        for field in ("submission_date", "approval_date", "expiry_date"):
            if field in data and data[field]:
                setattr(permit, field, data[field])
                updated.append(field)

        if updated:
            permit.save(update_fields=[*updated, "updated_at"])
            PermitEvent.objects.create(
                permit=permit,
                event="STATUS_UPDATE" if "status" in updated else "DETAIL_UPDATE",
                detail={"fields": updated, "by": request.user.email if request.user else None},
            )
        return JsonResponse(_serialize(permit))


class PermitAllView(APIView):
    """GET /api/ftth/permits/ — every permit across projects (tracker feed).

    Optionally filtered: ``?status=``, ``?project_id=``, ``?q=`` (permit type
    / route section substring). Cap at 1000 rows.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = PermitMatrix.objects.select_related("authority", "rule", "project").all()
        status_filter = request.GET.get("status")
        project_filter = request.GET.get("project_id")
        q = (request.GET.get("q") or "").strip().lower()

        if status_filter:
            qs = qs.filter(status=status_filter)
        if project_filter:
            qs = qs.filter(project_id=project_filter)
        if q:
            qs = qs.filter(
                dj_models.Q(permit_type__icontains=q) | dj_models.Q(route_section__icontains=q)
            )

        permits = []
        for pm in qs[:1000]:
            item = _serialize(pm)
            item["project_name"] = pm.project.name or pm.project_id
            permits.append(item)
        return JsonResponse({"total": len(permits), "permits": permits})


class PermitSummaryView(APIView):
    """GET /api/ftth/permits/summary/ — cross-project KPI for the dashboard."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = PermitMatrix.objects.select_related("project").all()
        counts: dict[str, int] = {}
        per_project: dict[str, int] = {}
        for pm in rows:
            counts[pm.status] = counts.get(pm.status, 0) + 1
            per_project[pm.project_id] = per_project.get(pm.project_id, 0) + 1
        ready = counts.get(PermitMatrix.STATUS_READY, 0) + counts.get(
            PermitMatrix.STATUS_APPROVED, 0
        )
        names = {
            str(p.pk): p.name for p in FtthProject.objects.filter(pk__in=per_project.keys())
        }
        return JsonResponse({
            "total": len(rows),
            "by_status": counts,
            "ready": ready,
            "projects_with_permits": len(per_project),
            "projects": [
                {"project_id": pid, "name": names.get(pid, pid), "permits": n}
                for pid, n in per_project.items()
            ],
        })
