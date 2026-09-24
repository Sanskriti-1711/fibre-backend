"""AI advisory endpoints — never mutate PermitMatrix.

All responses carry `is_ai_generated` + `disclaimer`. Deterministic
findings (blocking/level/days/checklists) come from `advisory` heuristics;
LLM text is additive and stored on PermitAiDraft.
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from django.http import JsonResponse

from ftth_hld.models import FtthProject

from ..models import PermitAiDraft, PermitMatrix
from .advisory import (
    authority_requirements,
    completeness_check,
    CompletenessInput,
    enrich_requirements_with_ai,
    estimate_timeline,
    project_risk_overview,
    risk_for_row,
)
from .drafting import draft_cover_text, draft_narrative, extract_requirements_from_text


def _get_project_or_404(project_id: str):
    ftth = FtthProject.objects.filter(pk=project_id).first()
    return ftth


class PermitAiDraftView(APIView):
    """POST /api/ftth/permits/projects/<pid>/ai/draft/ — draft cover/narrative.

    Body: {kind: cover|narrative, permit_id? , group? , permit_type? }
    For street-level grouping, pass permit_id of any row in the group.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        ftth = _get_project_or_404(project_id)
        if ftth is None:
            return JsonResponse({"detail": "Project not found."}, status=404)
        data = request.data or {}
        kind = (data.get("kind") or "").strip().lower()
        if kind not in ("cover", "narrative"):
            return JsonResponse({"detail": "kind must be cover or narrative."}, status=400)

        pm = None
        permit_id = data.get("permit_id")
        if permit_id:
            pm = PermitMatrix.objects.filter(permit_id=permit_id, project_id=project_id).select_related("authority", "rule").first()
            if pm is None:
                return JsonResponse({"detail": "Permit not found for this project."}, status=404)
        else:
            # Fallback: pick first row of permit_type/group if supplied.
            qs = PermitMatrix.objects.filter(project_id=project_id).select_related("authority", "rule")
            pt = (data.get("permit_type") or "").strip()
            grp = (data.get("group") or data.get("permit_group") or "").strip()
            if pt:
                qs = qs.filter(permit_type=pt)
            if grp:
                qs = qs.filter(permit_group=grp)
            pm = qs.first()
            if pm is None:
                return JsonResponse({"detail": "No matching permit row for drafting context."}, status=400)

        project_name = ftth.name or project_id
        permit_group = pm.permit_group or pm.route_section
        authority_name = pm.authority.name if pm.authority else ""
        # Count street siblings for context.
        group_count = PermitMatrix.objects.filter(project_id=project_id, permit_group=pm.permit_group, permit_type=pm.permit_type).count() if pm.permit_group else 1
        trench_summary = ""
        try:
            from ..generators.data import trench_stats

            ts = trench_stats(project_id)
            if ts.get("total_length_m"):
                trench_summary = f"Total trench ~{ts['total_length_m']:.0f} m. "
            by_surf = ", ".join(f"{k} {v} m" for k, v in (ts.get("by_surface") or {}).items()) or ""
            if by_surf:
                trench_summary += f"By surface: {by_surf}."
        except Exception:
            pass

        if kind == "cover":
            res = draft_cover_text(project_name, project_id, pm.permit_type, permit_group, authority_name, pm.municipality or "", group_count, trench_summary)
            draft_type = PermitAiDraft.DRAFT_COVER
        else:
            try:
                from ..generators.data import trench_stats

                ts = trench_stats(project_id)
            except Exception:
                ts = None
            res = draft_narrative(project_name, pm.permit_type, permit_group, pm.evidence, ts)
            draft_type = PermitAiDraft.DRAFT_NARRATIVE

        draft = PermitAiDraft.objects.create(
            project_id=project_id,
            permit=pm,
            permit_group=pm.permit_group or "",
            permit_type=pm.permit_type or "",
            draft_type=draft_type,
            content=res.get("text") or "",
            deterministic_fallback=res.get("deterministic_fallback") or "",
            is_ai_generated=bool(res.get("is_ai_generated")),
            disclaimer=res.get("disclaimer") or "",
            meta={"permit_id": str(pm.permit_id), "group_count": group_count},
            created_by=request.user if request.user and request.user.is_authenticated else None,
        )
        return JsonResponse(
            {
                "draft_id": str(draft.id),
                "project_id": project_id,
                "permit_id": str(pm.permit_id),
                "kind": kind,
                "text": draft.content,
                "deterministic_fallback": draft.deterministic_fallback,
                "is_ai_generated": draft.is_ai_generated,
                "disclaimer": draft.disclaimer,
                "created_at": draft.created_at.isoformat() if draft.created_at else None,
            },
            status=201,
        )


class PermitAiRequirementsView(APIView):
    """GET/POST /api/ftth/permits/projects/<pid>/ai/requirements/.

    GET  ?permit_type=Road+Opening[&permit_id=…] → deterministic checklist (+ ai_notes when configured).
    POST {raw_text} → extract checklist from pasted authority guidance.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        ftth = _get_project_or_404(project_id)
        if ftth is None:
            return JsonResponse({"detail": "Project not found."}, status=404)
        permit_type = (request.GET.get("permit_type") or "").strip()
        permit_id = (request.GET.get("permit_id") or "").strip()
        authority_name = ""
        evidence = None
        project_name = ftth.name or project_id
        if permit_id:
            pm = PermitMatrix.objects.filter(permit_id=permit_id, project_id=project_id).select_related("authority").first()
            if pm:
                permit_type = permit_type or pm.permit_type or ""
                authority_name = pm.authority.name if pm.authority else ""
                evidence = pm.evidence
        if not permit_type:
            return JsonResponse({"detail": "permit_type is required (or supply permit_id)."}, status=400)
        # Deterministic baseline + optional AI enrichment.
        enriched = enrich_requirements_with_ai(permit_type, authority_name, project_name, evidence)
        # Persist as draft for audit.
        PermitAiDraft.objects.create(
            project_id=project_id,
            permit_id=permit_id if permit_id else None,
            permit_type=permit_type,
            permit_group="",
            draft_type=PermitAiDraft.DRAFT_REQUIREMENTS,
            content="\n".join(enriched.get("items") or []),
            is_ai_generated=bool(enriched.get("is_ai_generated")),
            disclaimer=enriched.get("disclaimer") or "",
            meta={"authority": authority_name, "ai_notes": enriched.get("ai_notes") or ""},
            created_by=request.user if request.user and request.user.is_authenticated else None,
        )
        return JsonResponse({"project_id": project_id, **enriched})

    def post(self, request, project_id):
        ftth = _get_project_or_404(project_id)
        if ftth is None:
            return JsonResponse({"detail": "Project not found."}, status=404)
        raw = (request.data or {}).get("raw_text") or (request.data or {}).get("text") or ""
        res = extract_requirements_from_text(str(raw))
        PermitAiDraft.objects.create(
            project_id=project_id,
            draft_type=PermitAiDraft.DRAFT_REQUIREMENTS,
            content="\n".join(res.get("items") or []),
            is_ai_generated=bool(res.get("is_ai_generated")),
            disclaimer=res.get("disclaimer") or "",
            meta={"raw_excerpt": (res.get("raw_excerpt") or "")[:1200], "heuristic_items": res.get("heuristic_items") or []},
            created_by=request.user if request.user and request.user.is_authenticated else None,
        )
        return JsonResponse({"project_id": project_id, **res}, status=201)


class PermitAiCompletenessView(APIView):
    """GET /api/ftth/permits/permits/<permit_id>/ai/completeness/ — explain why not Ready."""

    permission_classes = [IsAuthenticated]

    def get(self, request, permit_id):
        pm = PermitMatrix.objects.filter(permit_id=permit_id).select_related("authority", "rule").first()
        if pm is None:
            return JsonResponse({"detail": "Permit not found."}, status=404)
        inp = CompletenessInput(
            permit_type=pm.permit_type or "",
            status=pm.status or "",
            readiness_pct=int(pm.readiness_pct or 0),
            required_keys=list((pm.rule.evidence_required if pm.rule else []) or []),
            evidence=pm.evidence or {},
            municipality=pm.municipality or "",
            authority_name=pm.authority.name if pm.authority else "",
            permit_group=pm.permit_group or "",
        )
        res = completeness_check(inp)
        # Record draft (project from permit).
        PermitAiDraft.objects.create(
            project_id=pm.project_id,
            permit=pm,
            permit_type=pm.permit_type or "",
            permit_group=pm.permit_group or "",
            draft_type=PermitAiDraft.DRAFT_COMPLETENESS,
            content=res.get("summary") or "",
            deterministic_fallback="",
            is_ai_generated=bool(res.get("is_ai_generated")),
            disclaimer=res.get("disclaimer") or "",
            meta={"missing": res.get("missing") or [], "ai_explanation": res.get("ai_explanation") or ""},
            created_by=request.user if request.user and request.user.is_authenticated else None,
        )
        return JsonResponse({"permit_id": str(pm.permit_id), "project_id": pm.project_id, **res})


class PermitAiRiskView(APIView):
    """GET /api/ftth/permits/projects/<pid>/ai/risk/?permit_id=… OR project overview when no permit_id."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        permit_id = (request.GET.get("permit_id") or "").strip()
        if permit_id:
            pm = PermitMatrix.objects.filter(permit_id=permit_id, project_id=project_id).first()
            if pm is None:
                return JsonResponse({"detail": "Permit not found for this project."}, status=404)
            res = risk_for_row(pm.permit_type or "", pm.status or "", int(pm.readiness_pct or 0), bool(pm.required), bool(pm.blocks_construction), pm.permit_group or "")
            PermitAiDraft.objects.create(
                project_id=project_id,
                permit=pm,
                permit_type=pm.permit_type or "",
                permit_group=pm.permit_group or "",
                draft_type=PermitAiDraft.DRAFT_RISK,
                content=res.get("summary") or "",
                is_ai_generated=bool(res.get("is_ai_generated")),
                disclaimer=res.get("disclaimer") or "",
                meta={"level": res.get("level"), "ai_paragraph": res.get("ai_paragraph") or ""},
                created_by=request.user if request.user and request.user.is_authenticated else None,
            )
            return JsonResponse({"permit_id": str(pm.permit_id), "project_id": project_id, **res})
        # Project overview
        ftth = _get_project_or_404(project_id)
        if ftth is None:
            return JsonResponse({"detail": "Project not found."}, status=404)
        rows = list(
            PermitMatrix.objects.filter(project_id=project_id).values(
                "permit_type", "status", "readiness_pct", "required", "blocks_construction"
            )
        )
        overview = project_risk_overview(rows)
        PermitAiDraft.objects.create(
            project_id=project_id,
            draft_type=PermitAiDraft.DRAFT_RISK,
            content=overview.get("summary") or "",
            is_ai_generated=False,
            disclaimer=overview.get("summary") and "Deterministic project rollup." or "",
            meta={"counts": overview.get("counts") or {}},
            created_by=request.user if request.user and request.user.is_authenticated else None,
        )
        return JsonResponse({"project_id": project_id, **overview})


class PermitAiTimelineView(APIView):
    """GET /api/ftth/permits/projects/<pid>/ai/timeline/ — review-time window estimate."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        ftth = _get_project_or_404(project_id)
        if ftth is None:
            return JsonResponse({"detail": "Project not found."}, status=404)
        # Optional filter by status — default: all active (not approved/closed).
        rows = list(PermitMatrix.objects.filter(project_id=project_id).values("permit_type", "status"))
        permit_types = [r["permit_type"] or "Unknown" for r in rows]
        statuses = [r["status"] or "" for r in rows]
        res = estimate_timeline(permit_types, statuses)
        PermitAiDraft.objects.create(
            project_id=project_id,
            draft_type=PermitAiDraft.DRAFT_TIMELINE,
            content=f"{res.get('min_days')}–{res.get('max_days')} working days",
            is_ai_generated=bool(res.get("is_ai_generated")),
            disclaimer=res.get("disclaimer") or "",
            meta={"per_type": res.get("per_type") or {}, "ai_note": res.get("ai_note") or ""},
            created_by=request.user if request.user and request.user.is_authenticated else None,
        )
        return JsonResponse({"project_id": project_id, **res})


class PermitAiDraftReviewView(APIView):
    """GET list + POST review an AI draft. PATCH review flag only."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = PermitAiDraft.objects.select_related("project").all()
        project_id = (request.GET.get("project_id") or "").strip()
        draft_type = (request.GET.get("draft_type") or "").strip()
        if project_id:
            qs = qs.filter(project_id=project_id)
        if draft_type:
            qs = qs.filter(draft_type=draft_type)
        items = []
        for d in qs[:200]:
            items.append(
                {
                    "draft_id": str(d.id),
                    "project_id": d.project_id,
                    "permit_id": str(d.permit_id) if d.permit_id else None,
                    "permit_type": d.permit_type,
                    "permit_group": d.permit_group,
                    "draft_type": d.draft_type,
                    "content": d.content,
                    "deterministic_fallback": d.deterministic_fallback,
                    "is_ai_generated": d.is_ai_generated,
                    "disclaimer": d.disclaimer,
                    "meta": d.meta or {},
                    "reviewed": d.reviewed,
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                }
            )
        return JsonResponse({"total": len(items), "drafts": items})

    def post(self, request, draft_id):
        draft = PermitAiDraft.objects.filter(pk=draft_id).first()
        if draft is None:
            return JsonResponse({"detail": "Draft not found."}, status=404)
        draft.reviewed = True
        draft.reviewed_at = timezone.now()
        draft.reviewed_by = request.user if request.user and request.user.is_authenticated else None
        draft.save(update_fields=["reviewed", "reviewed_at", "reviewed_by"])
        return JsonResponse(
            {
                "draft_id": str(draft.id),
                "reviewed": True,
                "reviewed_at": draft.reviewed_at.isoformat() if draft.reviewed_at else None,
                "is_ai_generated": draft.is_ai_generated,
            }
        )
