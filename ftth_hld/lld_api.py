"""
FTTH LLD — Django API views (Review workflow).

Serves the LLD Review workspace backed by REAL data:

  GET  /api/ftth/lld/projects/<pid>/review/                  -> review payload
  POST /api/ftth/lld/projects/<pid>/changes/<cid>/action/    -> approve/reject/correction
  POST /api/ftth/lld/projects/<pid>/approved-version/        -> create immutable AS version
  POST /api/ftth/lld/projects/<pid>/runs/                    -> record an LLD run
  GET  /api/ftth/lld/projects/<pid>/versions/                -> version chain + run history

The HLD layer comes from the Survey copy's imported ``projects.Feature`` rows
(the frozen HLD baseline), and the review queue is built from
``survey.SurveyFeature`` rows (engineer edits with original/survey geometry
and attributes).  Statuses follow the frontend contract:
pending_review / approved / rejected / needs_correction.

LLD is only runnable once an immutable Approved Survey Version exists and
zero changes are still pending review (a change sent back for correction is
considered resolved and does not block LLD).
"""

from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from ftth_hld.models import FtthProject, ApprovedSurveyVersion, LldRun
from projects.models import Feature, Project
from survey.models import SurveyFeature


HLD_VERSION = "HLD-V1"
ALGORITHM_VERSION = "fiber-lld-2.4.1"

_STATUS_MAP = {
    SurveyFeature.SurveyStatus.NEW: "pending_review",
    SurveyFeature.SurveyStatus.MODIFIED: "pending_review",
    SurveyFeature.SurveyStatus.REMOVED: "pending_review",
    SurveyFeature.SurveyStatus.PENDING_REVIEW: "pending_review",
    SurveyFeature.SurveyStatus.NEEDS_CORRECTION: "needs_correction",
    SurveyFeature.SurveyStatus.REJECTED: "rejected",
    SurveyFeature.SurveyStatus.APPROVED: "approved",
    SurveyFeature.SurveyStatus.COMPLETED: "approved",
}

_ACTION_STATUS = {
    "approve": SurveyFeature.SurveyStatus.APPROVED,
    "reject": SurveyFeature.SurveyStatus.REJECTED,
    "correction": SurveyFeature.SurveyStatus.NEEDS_CORRECTION,
}

# Statuses considered "resolved" for LLD readiness: approved, rejected, and
# needs_correction (sent back for redo). Only unreviewed (NEW/MODIFIED/REMOVED/
# PENDING_REVIEW) changes block LLD.
_RESOLVED_STATUSES = [
    SurveyFeature.SurveyStatus.APPROVED,
    SurveyFeature.SurveyStatus.REJECTED,
    SurveyFeature.SurveyStatus.COMPLETED,
    SurveyFeature.SurveyStatus.NEEDS_CORRECTION,
]


def _survey_copy(ftth_project_id):
    """The survey copy Project linked to an HLD run, or None."""
    return Project.objects.filter(source_ftth_project_id=ftth_project_id).first()


def _fc(features):
    return {"type": "FeatureCollection", "features": features}


def _hld_feature_collection(survey_copy):
    """All HLD baseline features (frozen, from the survey copy)."""
    feats = []
    qs = Feature.objects.filter(project=survey_copy).only(
        "id", "layer_id", "layer_name", "geometry", "properties"
    )
    for f in qs.iterator(chunk_size=500):
        if not f.geometry:
            continue
        props = dict(f.properties or {})
        props["feature_id"] = props.get("_feature_id") or str(f.id)
        props["layer"] = f.layer_id or f.layer_name or "unknown"
        feats.append({"type": "Feature", "geometry": f.geometry, "properties": props})
    return _fc(feats)


def _attr_diff(sf):
    """Diff of original vs survey attributes, frontend shape."""
    orig = sf.original_attributes or {}
    surv = sf.survey_attributes or {}
    out = []
    keys = sorted(set(list(orig.keys()) + list(surv.keys())))
    for k in keys:
        ov, sv = orig.get(k), surv.get(k)
        if ov != sv:
            out.append({
                "field": str(k),
                "hld_value": "" if ov is None else str(ov),
                "survey_value": "" if sv is None else str(sv),
                "reason": sf.change_reason or "",
            })
    return out


def _change_type(sf):
    if sf.is_removal or sf.survey_status == SurveyFeature.SurveyStatus.REMOVED:
        return "removed_feature"
    if sf.original_hld_feature_id is None:
        return "new_feature"
    if (sf.survey_geometry or {}) != (sf.original_geometry or {}):
        return "geometry"
    if (sf.survey_attributes or {}) != (sf.original_attributes or {}):
        return "attribute"
    return "attribute"


def _change_payload(sf):
    comments = []
    if sf.review_notes:
        comments.append({
            "by": "LLD Reviewer",
            "text": sf.review_notes,
            "timestamp": (sf.updated_at or timezone.now()).isoformat(),
        })
    return {
        "change_id": str(sf.id),
        "feature_id": str(sf.original_hld_feature_id or sf.id),
        "layer": sf.layer_name or sf.layer_id or "unknown",
        "change_type": _change_type(sf),
        "status": _STATUS_MAP.get(sf.survey_status, "pending_review"),
        "original_geometry": sf.original_geometry,
        "survey_geometry": sf.survey_geometry,
        "attributes": _attr_diff(sf),
        "reason": sf.change_reason or "",
        "engineer": (
            sf.engineer.full_name
            if sf.engineer and sf.engineer.full_name
            else (sf.engineer.email if sf.engineer else "")
        ),
        "timestamp": (sf.updated_at or sf.created_at or timezone.now()).isoformat(),
        "evidence": {"photos": 1 if sf.photo else 0, "notes": ""},
        "comments": comments,
    }


def _survey_feature_collection(survey_copy):
    """Survey dataset: HLD baseline as edited by engineers + new features."""
    by_hld = {}
    for sf in SurveyFeature.objects.filter(project=survey_copy).select_related("engineer").iterator(chunk_size=500):
        by_hld.setdefault(str(sf.original_hld_feature_id), []).append(sf)

    feats = []
    qs = Feature.objects.filter(project=survey_copy).only("id", "layer_id", "layer_name", "geometry", "properties")
    for f in qs.iterator(chunk_size=500):
        if not f.geometry:
            continue
        group = by_hld.get(str(f.id)) or []
        geom = f.geometry
        change_id = None
        survey_status = None
        removal = False
        for sf in group:
            change_id = str(sf.id)
            survey_status = _STATUS_MAP.get(sf.survey_status, "pending_review")
            if sf.is_removal or sf.survey_status == SurveyFeature.SurveyStatus.REMOVED:
                removal = True
            if sf.survey_geometry:
                geom = sf.survey_geometry
        props = {"feature_id": str(f.id), "layer": f.layer_id or f.layer_name or "unknown"}
        if change_id:
            props["change_id"] = change_id
            props["status"] = survey_status
            props["survey_removal"] = removal
        feats.append({"type": "Feature", "geometry": geom, "properties": props})

    # New features created in the field (no HLD row).
    for sf in SurveyFeature.objects.filter(project=survey_copy, original_hld_feature__isnull=True).select_related("engineer").iterator(chunk_size=500):
        if not sf.survey_geometry:
            continue
        feats.append({
            "type": "Feature",
            "geometry": sf.survey_geometry,
            "properties": {
                "feature_id": str(sf.id),
                "layer": sf.layer_name or sf.layer_id or "unknown",
                "change_id": str(sf.id),
                "status": _STATUS_MAP.get(sf.survey_status, "pending_review"),
                "survey_removal": False,
            },
        })
    return _fc(feats)


def _approved_feature_collection(survey_copy):
    """Approved Survey dataset = HLD + approved changes - approved removals."""
    keep = {}
    qs = Feature.objects.filter(project=survey_copy).only("id", "layer_id", "layer_name", "geometry", "properties")
    for f in qs.iterator(chunk_size=500):
        if f.geometry:
            props = dict(f.properties or {})
            props["feature_id"] = props.get("_feature_id") or str(f.id)
            props["layer"] = f.layer_id or f.layer_name or "unknown"
            keep[str(f.id)] = {"type": "Feature", "geometry": f.geometry, "properties": props}

    for sf in SurveyFeature.objects.filter(project=survey_copy).select_related("engineer").iterator(chunk_size=500):
        if sf.survey_status not in (SurveyFeature.SurveyStatus.APPROVED, SurveyFeature.SurveyStatus.COMPLETED):
            continue
        if sf.original_hld_feature_id:
            key = str(sf.original_hld_feature_id)
            if key not in keep:
                continue
            if sf.is_removal or not sf.survey_geometry:
                keep.pop(key, None)
            else:
                keep[key]["geometry"] = sf.survey_geometry
                keep[key]["properties"]["approved"] = True
                keep[key]["properties"]["change_id"] = str(sf.id)
                for a in _attr_diff(sf):
                    keep[key]["properties"][a["field"]] = a["survey_value"]
        else:
            if sf.survey_geometry:
                keep[str(sf.id)] = {
                    "type": "Feature",
                    "geometry": sf.survey_geometry,
                    "properties": {
                        "feature_id": str(sf.id),
                        "layer": sf.layer_name or sf.layer_id or "unknown",
                        "approved": True,
                        "change_id": str(sf.id),
                    },
                }
    return _fc(list(keep.values()))


def _project_payload(ftth):
    d = (ftth.completed_at or ftth.created_at)
    return {
        "id": ftth.project_id,
        "name": ftth.name or ftth.project_id,
        "hld_version": HLD_VERSION,
        "hld_version_date": d.strftime("%Y-%m-%d") if d else "",
        "created_at": ftth.created_at.isoformat() if ftth.created_at else None,
    }


def _next_version(qs, prefix):
    """Compute the next AS-Vxx / LLD-Vxx label for a project."""
    field = "version" if qs.model is ApprovedSurveyVersion else "lld_version"
    n = 1
    for v in qs.values_list(field, flat=True):
        try:
            n = max(n, int(str(v).rsplit("-", 1)[-1]) + 1)
        except ValueError:
            pass
    return "%s-V%02d" % (prefix, n)



class LldReviewView(APIView):
    """GET /api/ftth/lld/projects/<pid>/review/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        ftth = get_object_or_404(FtthProject, pk=project_id)
        copy = _survey_copy(project_id)

        changes = []
        hld_fc = _fc([])
        survey_fc = _fc([])
        approved_fc = _fc([])
        if copy is not None:
            hld_fc = _hld_feature_collection(copy)
            survey_fc = _survey_feature_collection(copy)
            approved_fc = _approved_feature_collection(copy)
            changes = [
                _change_payload(sf)
                for sf in SurveyFeature.objects.filter(project=copy).select_related("engineer").iterator(chunk_size=500)
            ]

        asv = ApprovedSurveyVersion.objects.filter(ftth_project=ftth).first()
        proj = _project_payload(ftth)
        return JsonResponse({
            "demo": False,
            "project": proj,
            "changes": changes,
            "layers": {"hld": hld_fc, "survey": survey_fc},
            "approved": approved_fc,
            "approved_survey_version": asv.version if asv else None,
            "approved_survey_created_at": asv.created_at.isoformat() if asv and asv.created_at else None,
            "version_chain": {
                "hld": {"id": HLD_VERSION, "date": proj["hld_version_date"], "by": "HLD Pipeline"},
                "approved_survey": (
                    {"id": asv.version, "date": asv.created_at.isoformat(), "by": "LLD Reviewer"}
                    if asv else None
                ),
            },
        })


class LldProjectsView(APIView):
    """GET /api/ftth/lld/projects/ — survey changes clubbed by project.

    Returns every HLD run that has a survey copy, with its survey-change
    queue (change payloads) and a per-project LLD readiness summary so the
    frontend can render one block per project and enable Run LLD only when
    zero changes remain unresolved.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        projects = []
        for ftth in FtthProject.objects.all().order_by("-created_at"):
            copy = _survey_copy(ftth.project_id)
            if copy is None:
                continue
            changes = [
                _change_payload(sf)
                for sf in SurveyFeature.objects.filter(project=copy)
                .select_related("engineer")
                .iterator(chunk_size=500)
            ]
            if not changes:
                continue
            statuses = [c["status"] for c in changes]
            pending = statuses.count("pending_review")
            correction = statuses.count("needs_correction")
            asv = ApprovedSurveyVersion.objects.filter(ftth_project=ftth).first()
            projects.append({
                "project_id": ftth.project_id,
                "name": ftth.name or ftth.project_id,
                "hld_version": HLD_VERSION,
                "total": len(changes),
                "pending": pending,
                "approved": statuses.count("approved"),
                "rejected": statuses.count("rejected"),
                "needs_correction": correction,
                "ready": pending == 0,
                "approved_survey_version": asv.version if asv else None,
                "changes": changes,
            })
        return JsonResponse({"projects": projects})


class LldChangeActionView(APIView):
    """POST /api/ftth/lld/projects/<pid>/changes/<cid>/action/"""
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id, change_id):
        if getattr(request.user, "role", None) != "SUBADMIN":
            return JsonResponse({"detail": "Only planners (SUBADMIN) can review survey changes."}, status=403)

        ftth = get_object_or_404(FtthProject, pk=project_id)
        copy = _survey_copy(project_id)
        if copy is None:
            return JsonResponse({"detail": "No survey copy for this project."}, status=400)

        if ApprovedSurveyVersion.objects.filter(ftth_project=ftth).exists():
            return JsonResponse({"detail": "Review is locked - the Approved Survey Version is immutable."}, status=400)

        sf = get_object_or_404(SurveyFeature, id=change_id, project=copy)
        action = request.data.get("action")
        if action not in _ACTION_STATUS:
            return JsonResponse({"detail": "action must be approve | reject | correction"}, status=400)

        was_removal = sf.survey_status == SurveyFeature.SurveyStatus.REMOVED
        sf.survey_status = _ACTION_STATUS[action]
        if was_removal:
            sf.is_removal = True
        comment = (request.data.get("comment") or "").strip()
        if comment:
            sf.review_notes = comment
        sf.save(update_fields=["survey_status", "is_removal", "review_notes", "updated_at"])

        return JsonResponse({
            "change_id": str(sf.id),
            "status": _STATUS_MAP.get(sf.survey_status, "pending_review"),
            "comment": comment or None,
            "approved": _approved_feature_collection(copy),
        })

class LldApprovedVersionView(APIView):
    """POST /api/ftth/lld/projects/<pid>/approved-version/"""
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        if getattr(request.user, "role", None) != "SUBADMIN":
            return JsonResponse({"detail": "Only planners (SUBADMIN) can create the Approved Survey Version."}, status=403)

        ftth = get_object_or_404(FtthProject, pk=project_id)
        copy = _survey_copy(project_id)
        if copy is None:
            return JsonResponse({"detail": "No survey copy for this project."}, status=400)

        existing = ApprovedSurveyVersion.objects.filter(ftth_project=ftth).first()
        if existing:
            return JsonResponse({
                "approved_survey_version": existing.version,
                "created_at": existing.created_at.isoformat(),
                "features": _approved_feature_collection(copy),
            })

        unresolved = SurveyFeature.objects.filter(project=copy).exclude(
            survey_status__in=_RESOLVED_STATUSES
        ).count()
        if unresolved:
            return JsonResponse({"detail": "Cannot create Approved Survey Version while %d change(s) are unresolved." % unresolved}, status=400)

        version = _next_version(ApprovedSurveyVersion.objects.filter(ftth_project=ftth), "AS")
        dataset = _approved_feature_collection(copy)
        asv = ApprovedSurveyVersion.objects.create(
            ftth_project=ftth,
            version=version,
            hld_version=HLD_VERSION,
            dataset=dataset,
            summary={"features": len(dataset.get("features", [])), "created_at": timezone.now().isoformat()},
            created_by=request.user if request.user.is_authenticated else None,
        )
        return JsonResponse({
            "approved_survey_version": asv.version,
            "created_at": asv.created_at.isoformat(),
            "features": dataset,
        })


class LldRunView(APIView):
    """POST /api/ftth/lld/projects/<pid>/runs/"""
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        if getattr(request.user, "role", None) != "SUBADMIN":
            return JsonResponse({"detail": "Only planners (SUBADMIN) can run LLD."}, status=403)

        ftth = get_object_or_404(FtthProject, pk=project_id)
        asv = ApprovedSurveyVersion.objects.filter(ftth_project=ftth).first()
        if asv is None:
            return JsonResponse({"detail": "Create an Approved Survey Version before running LLD."}, status=400)

        copy = _survey_copy(project_id)
        unresolved = 0
        if copy is not None:
            unresolved = SurveyFeature.objects.filter(project=copy).exclude(
                survey_status__in=_RESOLVED_STATUSES
            ).count()
        if unresolved:
            return JsonResponse({"detail": "LLD not ready - %d change(s) unresolved." % unresolved}, status=400)

        version = _next_version(LldRun.objects.filter(ftth_project=ftth), "LLD")
        run = LldRun.objects.create(
            ftth_project=ftth,
            lld_version=version,
            hld_version=asv.hld_version or HLD_VERSION,
            approved_survey_version=asv,
            algorithm_version=ALGORITHM_VERSION,
            input_dataset_version=asv.version,
            status=LldRun.STATUS_COMPLETED,
            outputs=len(asv.dataset.get("features", [])) if isinstance(asv.dataset, dict) else 0,
            run_by=request.user if request.user.is_authenticated else None,
        )
        return JsonResponse({"lld_version": run.lld_version, "status": run.status, "project_id": run.ftth_project_id})


class LldVersionsView(APIView):
    """GET /api/ftth/lld/projects/<pid>/versions/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        ftth = get_object_or_404(FtthProject, pk=project_id)
        asv = ApprovedSurveyVersion.objects.filter(ftth_project=ftth).first()
        runs = []
        for r in LldRun.objects.filter(ftth_project=ftth).select_related("approved_survey_version", "run_by"):
            runs.append({
                "lld_version": r.lld_version,
                "project_id": r.ftth_project_id,
                "hld_version": r.hld_version or HLD_VERSION,
                "approved_survey_version": r.approved_survey_version.version if r.approved_survey_version else None,
                "run_date": r.run_date.isoformat() if r.run_date else None,
                "run_by": (r.run_by.full_name if r.run_by and r.run_by.full_name else (r.run_by.email if r.run_by else "LLD Pipeline (auto)")),
                "algorithm_version": r.algorithm_version or ALGORITHM_VERSION,
                "input_dataset_version": r.input_dataset_version or (asv.version if asv else ""),
                "status": r.status,
                "outputs": r.outputs,
            })

        proj = _project_payload(ftth)
        return JsonResponse({
            "demo": False,
            "project": proj,
            "approved_survey_version": asv.version if asv else None,
            "approved_survey_created_at": asv.created_at.isoformat() if asv and asv.created_at else None,
            "hld_version": HLD_VERSION,
            "version_chain": {
                "hld": {"id": HLD_VERSION, "date": proj["hld_version_date"], "by": "HLD Pipeline"},
                "approved_survey": (
                    {"id": asv.version, "date": asv.created_at.isoformat(), "by": "LLD Reviewer"}
                    if asv else None
                ),
            },
            "runs": runs,
        })
