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

import copy
import json
import logging
import re
import threading
import time

logger = logging.getLogger(__name__)

from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.http import Http404, HttpResponse, JsonResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from ftth_hld.models import FtthProject
from ftth_lld.models import ApprovedSurveyVersion, LldLayer, LldRun
from permits.rules.engine import run_analysis as run_permit_analysis
from projects.models import Feature, Project, ProjectMember, ProjectLayer, StageEvent
from survey.models import SurveyFeature, ApprovalRecord
from users.models import User
from users.permissions import IsSubadmin

from .engine import (
    lld_run as engine_lld_run,
    lld_replan as engine_lld_replan,
    lld_status as engine_lld_status,
    lld_layer_geojson as engine_lld_layer,
    lld_download_zip as engine_lld_download,
)


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
_RESOLVED_STATUSES = [
    SurveyFeature.SurveyStatus.APPROVED,
    SurveyFeature.SurveyStatus.REJECTED,
    SurveyFeature.SurveyStatus.COMPLETED,
    # NOTE: NEEDS_CORRECTION is intentionally NOT resolved — per the LLD
    # spec, LLD must not run while corrections are outstanding. A correction
    # only becomes resolved when the engineer re-edits the feature (which
    # flips it back to MODIFIED / pending_review) and the reviewer approves.
]


def _survey_copy(ftth_project_id):
    """The survey copy Project linked to an HLD run, or None."""
    return Project.objects.filter(source_ftth_project_id=ftth_project_id).first()


def _team_members(ftth, copy):
    """Build the team list for an HLD run.

    The planner (HLD creator) and field engineer (assigned) are derived from
    the FtthProject itself and can't be removed here. Explicit ``ProjectMember``
    rows (reviewer / contractor / extra planner / observer, or additional
    engineers) are managed and expose their ``member_id`` for removal.
    """
    members = []
    if ftth.created_by:
        members.append({
            "role": "planner",
            "user": ftth.created_by.email,
            "full_name": ftth.created_by.full_name or "",
            "user_id": str(ftth.created_by.id),
            "managed": False,
            "member_id": None,
        })
    if ftth.assigned_engineer:
        members.append({
            "role": "engineer",
            "user": ftth.assigned_engineer.email,
            "full_name": ftth.assigned_engineer.full_name or "",
            "user_id": str(ftth.assigned_engineer.id),
            "managed": False,
            "member_id": None,
        })
    if copy is not None:
        for m in ProjectMember.objects.filter(project=copy).select_related("user"):
            members.append({
                "role": m.role,
                "user": m.user.email,
                "full_name": m.user.full_name or "",
                "user_id": str(m.user.id),
                "managed": True,
                "member_id": str(m.id),
            })
    return members


def _record_event(project, stage, event, actor=None, entity_id="", metadata=None):
    """Write a StageEvent to the unified project timeline (never raises)."""
    if project is None:
        return
    try:
        StageEvent.objects.create(
            project=project,
            stage=stage,
            event=event,
            actor=actor,
            entity_id=entity_id or "",
            metadata=metadata or {},
        )
    except Exception:
        pass


def _fc(features):
    return {"type": "FeatureCollection", "features": features}


def _normalize_layer(name):
    """Strip GeoPackage-import artifacts from a layer name.

    The GPKG import path names layers "X (Imported)" with an "imp-" layer_id
    prefix. Normalize both so LLD output groups cleanly (no stray
    "objects (Imported)" layer sitting next to "objects").
    """
    if not name:
        return name
    name = str(name).strip()
    name = re.sub(r"\s*\(imported\)\s*$", "", name, flags=re.IGNORECASE)
    if name.lower().startswith("imp-"):
        name = name[4:]
    return name.strip() or name


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
        props["layer"] = _normalize_layer(f.layer_id or f.layer_name or "unknown")
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


def _change_payload(sf, risk=None):
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
        "layer": _normalize_layer(sf.layer_name or sf.layer_id or "unknown"),
        "change_type": _change_type(sf),
        "status": _STATUS_MAP.get(sf.survey_status, "pending_review"),
        "original_geometry": sf.original_geometry,
        "survey_geometry": sf.survey_geometry,
        "original_attributes": sf.original_attributes or {},
        "survey_attributes": sf.survey_attributes or {},
        "attributes": _attr_diff(sf),
        "reason": sf.change_reason or "",
        "engineer": (
            sf.engineer.full_name
            if sf.engineer and sf.engineer.full_name
            else (sf.engineer.email if sf.engineer else "")
        ),
        "timestamp": (sf.updated_at or sf.created_at or timezone.now()).isoformat(),
        "evidence": {"photos": 1 if sf.photo else 0, "notes": ""},
        "risk": risk or {
            "score": 0, "band": "unknown", "severity": "none",
            "likelihood": 0, "lld_impact": 0, "factors": [],
        },
        "comments": comments,
        "approval_history": [
            {
                "decision": a.decision,
                "comment": a.comment,
                "reviewer": (
                    (a.reviewer.full_name or a.reviewer.email)
                    if a.reviewer else None
                ),
                "created_at": a.created_at.isoformat(),
            }
            for a in sf.approval_records.all()
        ],
    }


def _risk_band_counts(changes):
    """{critical: n, high: n, medium: n, low: n} across a change payload list."""
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for c in changes:
        band = (c.get("risk") or {}).get("band") or "low"
        counts[band] = counts.get(band, 0) + 1
    return counts


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
        props = {"feature_id": str(f.id), "layer": _normalize_layer(f.layer_id or f.layer_name or "unknown")}
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
                "layer": _normalize_layer(sf.layer_name or sf.layer_id or "unknown"),
                "change_id": str(sf.id),
                "status": _STATUS_MAP.get(sf.survey_status, "pending_review"),
                "survey_removal": False,
            },
        })
    return _fc(feats)


def _approved_feature_collection(survey_copy):
    """Approved Survey dataset — the LLD's sole input (survey as ground truth).

    This is a SELF-CONTAINED snapshot of the surveyed, planner-approved field
    reality: the project's full layer set (HLD geometry stands in for features
    the engineer did not touch — "full area + approved deltas") with every
    approved survey change overlaid. It is what LLD Mode A runs on directly
    and what LLD Mode B feeds back as brownfield; the LLD engine NEVER reads
    HLD layers, routing or topology separately.

    Composition:
        approved dataset = full layer set + approved changes - approved removals

    Each approved change carries its own before/after pair:
        - current geometry  = the engineer's survey geometry (ground truth)
        - original_geometry = the survey change's own frozen pre-edit record
          (== the HLD geometry as captured on the SurveyFeature at edit time),
          used ONLY by the engine's relay/purge passes to re-lay dependents
          onto the new path — it is surveyed data, never a live HLD lookup.
    """
    keep = {}
    qs = Feature.objects.filter(project=survey_copy).only("id", "layer_id", "layer_name", "geometry", "properties")
    for f in qs.iterator(chunk_size=500):
        if f.geometry:
            props = dict(f.properties or {})
            props["feature_id"] = props.get("_feature_id") or str(f.id)
            props["layer"] = _normalize_layer(f.layer_id or f.layer_name or "unknown")
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
                # Before/after pair FOR THE ENGINE'S WITHIN-DATASET RELAY:
                # the engineer's survey geometry is the new path; the survey
                # change's own frozen original_geometry (captured from HLD at
                # edit time, i.e. surveyed data) is the old path. The relay
                # pass needs both sides of the reroute to re-lay dependents
                # onto the new path and purge the old one. Sourced from the
                # SurveyFeature record — no live HLD reference at LLD time.
                if keep[key]["geometry"] != sf.survey_geometry:
                    keep[key]["properties"].setdefault(
                        "original_geometry", sf.original_geometry or keep[key]["geometry"]
                    )
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
                        "layer": _normalize_layer(sf.layer_name or sf.layer_id or "unknown"),
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
    n = 0
    for v in qs.values_list(field, flat=True):
        m = re.search(r"(\d+)\s*$", str(v))
        if m:
            n = max(n, int(m.group(1)))
    return "%s-V%02d" % (prefix, n + 1)



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
            from survey.risk_scoring import score_changes

            sfs = list(
                SurveyFeature.objects.filter(project=copy)
                .select_related("engineer")
                .iterator(chunk_size=500)
            )
            risk_by_id = score_changes(sfs)
            changes = [_change_payload(sf, risk_by_id.get(str(sf.id))) for sf in sfs]
            # Highest-risk changes first — the review queue priority order.
            changes.sort(key=lambda c: -c["risk"]["score"])

        asv = (
            ApprovedSurveyVersion.objects.filter(ftth_project=ftth)
            .order_by("-created_at")
            .first()
        )
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

    Counters are annotated at the DB level (no Python iteration over
    SurveyFeature rows just to count statuses). The full ``changes``
    payload is still built for consumers that render them (e.g.
    ftth-survey-changes.html).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Count

        # Bulk-fetch approved survey versions so we don't do one query per
        # project inside the loop.
        asv_map = {
            a.ftth_project_id: a
            for a in ApprovedSurveyVersion.objects.filter(
                ftth_project__in=FtthProject.objects.all()
            )
            # DISTINCT ON (ftth_project_id) requires ORDER BY to start with
            # the same expression (PostgreSQL) — latest version per project.
            .order_by("ftth_project_id", "-created_at")
            .distinct("ftth_project_id")
        }

        projects = []
        for ftth in FtthProject.objects.all().order_by("-created_at"):
            copy = _survey_copy(ftth.project_id)
            if copy is None:
                continue

            # DB-level status counts (single query, no Python loop over
            # SurveyFeature rows just to count statuses).
            sf_qs = SurveyFeature.objects.filter(project=copy)
            total = sf_qs.count()
            if total == 0:
                continue
            counts = (
                sf_qs
                .values("survey_status")
                .annotate(n=Count("id"))
            )
            c = {row["survey_status"]: row["n"] for row in counts}
            pending = c.get(SurveyFeature.SurveyStatus.PENDING_REVIEW, 0) + c.get(SurveyFeature.SurveyStatus.NEW, 0) + c.get(SurveyFeature.SurveyStatus.MODIFIED, 0) + c.get(SurveyFeature.SurveyStatus.REMOVED, 0)
            correction = c.get(SurveyFeature.SurveyStatus.NEEDS_CORRECTION, 0)
            approved = c.get(SurveyFeature.SurveyStatus.APPROVED, 0) + c.get(SurveyFeature.SurveyStatus.COMPLETED, 0)
            rejected = c.get(SurveyFeature.SurveyStatus.REJECTED, 0)

            asv = asv_map.get(ftth.project_id)
            sfs = list(sf_qs.select_related("engineer").iterator(chunk_size=500))
            # Tier-1 A5 — risk-rank this project's queue (severity × likelihood
            # × LLD impact); deterministic rules, no model involved.
            from survey.risk_scoring import score_changes

            risk_by_id = score_changes(sfs)
            changes = [_change_payload(sf, risk_by_id.get(str(sf.id))) for sf in sfs]
            # Highest-risk changes first — the review queue priority order.
            changes.sort(key=lambda c: -c["risk"]["score"])
            projects.append({
                "project_id": ftth.project_id,
                "name": ftth.name or ftth.project_id,
                "hld_version": HLD_VERSION,
                "total": total,
                "pending": pending,
                "approved": approved,
                "rejected": rejected,
                "needs_correction": correction,
                "ready": pending == 0 and correction == 0,
                "approved_survey_version": asv.version if asv else None,
                "risk_bands": _risk_band_counts(changes),
                "changes": changes,
            })
        return JsonResponse({"projects": projects})


class LldRunsView(APIView):
    """GET /api/ftth/lld/runs/ — every LLD run across all projects.

    Feeds the LLD Outputs page (a cross-project view of the final LLD design
    runs, mirroring the HLD Outputs page). Each entry carries its provenance
    (HLD + Approved Survey versions), status, progress and layer list so the
    frontend can render View Output / Download actions.

    Uses ``prefetch_related('layers')`` so the layer list is fetched in one
    extra query instead of one query per run.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        runs = []
        qs = (
            LldRun.objects
            .select_related("ftth_project", "approved_survey_version", "run_by")
            .prefetch_related("layers")
            .order_by("-run_date")
        )
        for r in qs:
            layers = [
                {"name": l.name, "feature_count": l.feature_count}
                for l in r.layers.all()
            ]
            runs.append({
                "project_id": r.ftth_project_id,
                "project_name": (r.ftth_project.name or r.ftth_project_id),
                "lld_version": r.lld_version,
                "mode": r.mode,
                "approved_survey_version": (
                    r.approved_survey_version.version
                    if r.approved_survey_version else None
                ),
                "hld_version": r.hld_version or HLD_VERSION,
                "run_date": r.run_date.isoformat() if r.run_date else None,
                "run_by": (
                    r.run_by.full_name
                    if r.run_by and r.run_by.full_name
                    else (r.run_by.email if r.run_by else "LLD Pipeline (auto)")
                ),
                "status": r.status,
                "outputs": r.outputs,
                "progress": r.progress,
                "validation": r.validation or {},
                "layers": layers,
            })
        return JsonResponse({"runs": runs})


class ProjectOverviewView(APIView):
    """GET /api/ftth/lld/projects/<pid>/overview/

    One aggregated endpoint for the hierarchical project view: team, business
    & technical metadata, per-layer stats, survey approval summary, latest LLD
    run, and the recent lifecycle events.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        ftth = get_object_or_404(FtthProject, pk=project_id)
        copy = _survey_copy(project_id)

        # ── Team ──────────────────────────────────────────────────────
        team = _team_members(ftth, copy)

        # ── Business / technical ──────────────────────────────────────
        business = {}
        technical = {}
        if copy is not None:
            business = {
                "client_name": copy.client_name,
                "contract_ref": copy.contract_ref,
                "priority": copy.priority,
                "region": copy.region,
                "meta": copy.business_meta or {},
            }
            technical = dict(copy.technical_meta or {})

        # ── Layers ────────────────────────────────────────────────────
        layers = []
        if copy is not None:
            feature_counts = {}
            for layer_id, layer_name, cnt in Feature.objects.filter(project=copy).values_list(
                "layer_id", "layer_name", "id"
            ):
                feature_counts[layer_id] = feature_counts.get(layer_id, {"layer_name": layer_name, "count": 0})
                feature_counts[layer_id]["count"] += 1

            sf_counts = {}
            for layer_id, status in SurveyFeature.objects.filter(project=copy).values_list(
                "layer_id", "survey_status"
            ):
                entry = sf_counts.setdefault(layer_id, {"approved": 0, "total": 0})
                entry["total"] += 1
                if status in (SurveyFeature.SurveyStatus.APPROVED, SurveyFeature.SurveyStatus.COMPLETED):
                    entry["approved"] += 1

            for layer_id, info in feature_counts.items():
                sfs = sf_counts.get(layer_id, {"approved": 0, "total": 0})
                layers.append({
                    "layer_id": layer_id,
                    "layer_name": _normalize_layer(info["layer_name"] or layer_id),
                    "feature_count": info["count"],
                    "survey_changes": sfs["total"],
                    "approved_changes": sfs["approved"],
                })

        # ── Approval summary ──────────────────────────────────────────
        approval_summary = {"total": 0, "approved": 0, "rejected": 0, "needs_correction": 0, "pending": 0}
        if copy is not None:
            for status in SurveyFeature.objects.filter(project=copy).values_list("survey_status", flat=True):
                approval_summary["total"] += 1
                key = _STATUS_MAP.get(status, "pending_review")
                approval_summary[key] = approval_summary.get(key, 0) + 1

        # ── LLD ───────────────────────────────────────────────────────
        run = LldRun.objects.filter(ftth_project=ftth).order_by("-run_date").first()
        lld = None
        if run:
            lld = {
                "run": run.lld_version,
                "status": run.status,
                "progress": run.progress,
                "outputs": run.outputs,
                "run_date": run.run_date.isoformat() if run.run_date else None,
                "validation": run.validation or {},
            }

        # ── Timeline events ───────────────────────────────────────────
        events = []
        if copy is not None:
            for e in StageEvent.objects.filter(project=copy).select_related("actor")[:20]:
                events.append({
                    "stage": e.stage,
                    "event": e.event,
                    "entity_id": e.entity_id,
                    "actor": (e.actor.full_name or e.actor.email) if e.actor else None,
                    "created_at": e.created_at.isoformat(),
                })

        return JsonResponse({
            "project_id": project_id,
            "name": ftth.name or project_id,
            "hld": {
                "status": ftth.status,
                "progress": ftth.progress,
                "created_at": ftth.created_at.isoformat() if ftth.created_at else None,
                "completed_at": ftth.completed_at.isoformat() if ftth.completed_at else None,
            },
            "team": team,
            "business": business,
            "technical": technical,
            "layers": layers,
            "approval_summary": approval_summary,
            "lld": lld,
            "events": events,
        })


class ProjectMembersView(APIView):
    """GET/POST /api/ftth/lld/projects/<pid>/members/

    List the team for an HLD run and add a managed member (reviewer,
    contractor, observer, extra planner/engineer). Members are stored on the
    survey copy Project, which is what links back to the HLD run.
    """

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsSubadmin()]
        return [IsAuthenticated()]

    def get(self, request, project_id):
        ftth = get_object_or_404(FtthProject, pk=project_id)
        copy = _survey_copy(project_id)
        return JsonResponse({
            "project_id": project_id,
            "members": _team_members(ftth, copy),
            "roles": [{"value": v, "label": l} for v, l in ProjectMember.Role.choices],
        })

    def post(self, request, project_id):
        ftth = get_object_or_404(FtthProject, pk=project_id)
        copy = _survey_copy(project_id)
        if copy is None:
            return JsonResponse(
                {"detail": "Assign this project to an engineer first — no survey copy exists yet."},
                status=400,
            )

        data = request.data or {}
        user_id = data.get("user_id")
        role = data.get("role")
        if not user_id or not role:
            return JsonResponse({"detail": "user_id and role are required."}, status=400)
        if role not in dict(ProjectMember.Role.choices):
            return JsonResponse({"detail": f"Invalid role: {role}"}, status=400)

        user = get_object_or_404(User, pk=user_id)
        member, created = ProjectMember.objects.get_or_create(
            project=copy,
            user=user,
            role=role,
            defaults={"added_by": request.user},
        )
        if created:
            _record_event(copy, "survey", "member_added", request.user, user.email, {"role": role})

        return JsonResponse(
            {
                "member_id": str(member.id),
                "role": role,
                "user": user.email,
                "full_name": user.full_name or "",
                "created": created,
            },
            status=201 if created else 200,
        )


class ProjectMemberRemoveView(APIView):
    """DELETE /api/ftth/lld/projects/<pid>/members/<member_id>/"""

    permission_classes = [IsSubadmin]

    def delete(self, request, project_id, member_id):
        ftth = get_object_or_404(FtthProject, pk=project_id)
        copy = _survey_copy(project_id)
        if copy is None:
            raise Http404("No survey copy for this project")
        member = get_object_or_404(ProjectMember, pk=member_id, project=copy)
        email, role = member.user.email, member.role
        member.delete()
        _record_event(copy, "survey", "member_removed", request.user, email, {"role": role})
        return JsonResponse({"deleted": member_id})


class FeatureLineageView(APIView):
    """GET /api/ftth/lld/projects/<pid>/features/<feature_id>/lineage/

    Returns the full lifecycle of a single feature across every stage:

        HLD      -> baseline geometry + attributes (projects.Feature)
        Survey   -> the engineer's change (why, original vs survey, who/when)
        Approval -> decision status + reviewer notes
        LLD      -> final output layer(s) + geometry + attributes

    Links are resolved through the stable ``feature_id`` that the LLD engine
    carries through from the HLD Feature id, so a reviewer can trace exactly
    what happened to this feature at each stage.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id, feature_id):
        ftth = get_object_or_404(FtthProject, pk=project_id)
        copy = _survey_copy(project_id)

        lineage = {
            "project_id": project_id,
            "feature_id": feature_id,
            "hld": None,
            "survey": None,
            "approval": None,
            "lld": None,
        }

        # ── HLD baseline ───────────────────────────────────────────────
        if copy is not None:
            feature = Feature.objects.filter(project=copy, id=feature_id).first()
            if feature:
                lineage["hld"] = {
                    "feature_id": str(feature.id),
                    "layer": _normalize_layer(feature.layer_id or feature.layer_name or "unknown"),
                    "layer_id": feature.layer_id,
                    "layer_name": feature.layer_name,
                    "geometry": feature.geometry,
                    "attributes": feature.properties or {},
                }

            # ── Survey change + approval ───────────────────────────────
            sf = (
                SurveyFeature.objects.filter(
                    project=copy, original_hld_feature_id=feature_id
                )
                .select_related("engineer")
                .order_by("-updated_at")
                .first()
            )
            if sf:
                lineage["survey"] = _change_payload(sf)
                records = list(sf.approval_records.all())
                latest = records[-1] if records else None
                lineage["approval"] = {
                    "status": _STATUS_MAP.get(sf.survey_status, "pending_review"),
                    "review_notes": sf.review_notes or "",
                    "reviewed_at": latest.created_at.isoformat() if latest else None,
                    "reviewed_by": (
                        (latest.reviewer.full_name or latest.reviewer.email)
                        if latest and latest.reviewer else None
                    ),
                    "history": [
                        {
                            "decision": r.decision,
                            "comment": r.comment,
                            "reviewer": (
                                (r.reviewer.full_name or r.reviewer.email)
                                if r.reviewer else None
                            ),
                            "created_at": r.created_at.isoformat(),
                        }
                        for r in records
                    ],
                }

        # ── LLD final output ───────────────────────────────────────────
        run = (
            LldRun.objects.filter(ftth_project=ftth, status=LldRun.STATUS_COMPLETED)
            .order_by("-run_date")
            .first()
        )
        if run:
            layers = []
            final = None
            for l in LldLayer.objects.filter(lld_run=run):
                for f in l.geojson.get("features", []):
                    props = f.get("properties", {}) or {}
                    if props.get("feature_id") == feature_id:
                        final = {
                            "layer": l.name,
                            "geometry": f.get("geometry"),
                            "attributes": props,
                        }
                        layers.append(l.name)
            lineage["lld"] = {
                "run": run.lld_version,
                "approved_survey_version": (
                    run.approved_survey_version.version
                    if run.approved_survey_version else None
                ),
                "status": run.status,
                "run_date": run.run_date.isoformat() if run.run_date else None,
                "layers": layers,
                "final": final,
            }

        return JsonResponse(lineage)


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

        sf = get_object_or_404(SurveyFeature, id=change_id, project=copy)

        # Immutability rule: the frozen Approved Survey Version dataset is
        # never modified. A NEW review cycle (engineer re-edits after a
        # version was created) is allowed — it produces a NEW version
        # (AS-V02) when complete. Only changes that are already resolved
        # (approved / rejected / completed) are locked, because flipping
        # them would silently contradict the frozen version.
        asv = ApprovedSurveyVersion.objects.filter(ftth_project=ftth).order_by("-created_at").first()
        if asv is not None and sf.survey_status in (
            SurveyFeature.SurveyStatus.APPROVED,
            SurveyFeature.SurveyStatus.REJECTED,
            SurveyFeature.SurveyStatus.COMPLETED,
        ):
            return JsonResponse({
                "detail": (
                    "This change is already resolved in Approved Survey Version %s. "
                    "The engineer must re-edit the feature to start a new review cycle."
                ) % asv.version,
            }, status=400)

        action = request.data.get("action")
        new_status = SurveyFeature.status_for_decision(action)
        if new_status is None:
            return JsonResponse({"detail": "action must be approve | reject | correction"}, status=400)

        was_removal = sf.survey_status == SurveyFeature.SurveyStatus.REMOVED
        sf.survey_status = new_status
        if was_removal:
            sf.is_removal = True
        comment = (request.data.get("comment") or "").strip()
        if comment:
            sf.review_notes = comment
        sf.save(update_fields=["survey_status", "is_removal", "review_notes", "updated_at"])

        # First-class approval history — one record per decision.
        decision_value = getattr(new_status, "value", str(new_status))
        if decision_value in ("approved", "rejected", "needs_correction"):
            ApprovalRecord.objects.create(
                survey_feature=sf,
                decision=decision_value,
                comment=comment,
                reviewer=request.user if request.user.is_authenticated else None,
            )
            _record_event(copy, "survey", f"change_{decision_value}", request.user, str(sf.id), {"comment": comment})

        # Survey-stage permit hooks (fire-and-forget — never allowed to
        # break the review flow):
        # 1. Variation permits: an approved route change supersedes any
        #    APPROVED/CLOSED permit on the affected route section — the
        #    permit is re-opened as a new revision. Runs BEFORE the evidence
        #    hook so the re-fed evidence re-readies the variation.
        # 2. Evidence: feed the permit matrix evidence for this route
        #    section (crossings, surface, utility reuse, photos).
        if decision_value == "approved":
            try:
                from permits.rules.variation import create_variations
                create_variations(project_id, sf, user=request.user)
            except Exception:
                pass
            try:
                from permits.rules.survey_hook import ensure_survey_evidence
                ensure_survey_evidence(project_id)
            except Exception:
                pass

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

        existing = ApprovedSurveyVersion.objects.filter(ftth_project=ftth).order_by("-created_at").first()
        if existing:
            # New review cycle? Any survey feature edited after the last
            # version was frozen means the approved dataset changed.
            newer = SurveyFeature.objects.filter(
                project=copy, updated_at__gt=existing.created_at
            ).exists()
            if not newer:
                return JsonResponse({
                    "approved_survey_version": existing.version,
                    "created_at": existing.created_at.isoformat(),
                    "features": _approved_feature_collection(copy),
                })
            # else: fall through and create the NEXT version (AS-V02) — the
            # frozen V1 is never modified, exactly as the immutability rule
            # requires.

        unresolved = SurveyFeature.objects.filter(project=copy).exclude(
            survey_status__in=_RESOLVED_STATUSES
        ).count()
        if unresolved:
            return JsonResponse({"detail": "Cannot create Approved Survey Version while %d change(s) are unresolved." % unresolved}, status=400)

        version = _next_version(ApprovedSurveyVersion.objects.filter(ftth_project=ftth), "AS")
        dataset = _approved_feature_collection(copy)

        # A reroute is only valid LLD input when both sides of the change are
        # present: the engineer's approved geometry and the frozen HLD
        # geometry it replaces. Without the original path, the LLD engine
        # cannot relay dependent trench/duct/cable layers or purge the old
        # route safely. Fail the version creation instead of silently freezing
        # an incomplete transfer.
        missing_reroute_baseline = []
        for feature in dataset.get("features", []):
            props = feature.get("properties") or {}
            if not props.get("approved") or not props.get("change_id"):
                continue
            original = props.get("original_geometry")
            current = feature.get("geometry")
            if original and current and original != current:
                continue
            sf = SurveyFeature.objects.filter(pk=props.get("change_id"), project=copy).first()
            if sf and sf.original_geometry and sf.survey_geometry and sf.original_geometry != sf.survey_geometry:
                missing_reroute_baseline.append(str(sf.id))
        if missing_reroute_baseline:
            return JsonResponse({
                "detail": (
                    "Cannot create Approved Survey Version: approved reroute "
                    "geometry is missing its original HLD baseline for feature(s): "
                    + ", ".join(missing_reroute_baseline[:10])
                )
            }, status=400)

        asv = ApprovedSurveyVersion.objects.create(
            ftth_project=ftth,
            version=version,
            hld_version=HLD_VERSION,
            dataset=dataset,
            summary={"features": len(dataset.get("features", [])), "created_at": timezone.now().isoformat()},
            created_by=request.user if request.user.is_authenticated else None,
        )
        _record_event(copy, "survey", "approved_survey_created", request.user, asv.version)
        return JsonResponse({
            "approved_survey_version": asv.version,
            "created_at": asv.created_at.isoformat(),
            "features": dataset,
        })


def _run_lld_job(project_id: str, run_id, submit: bool = True) -> None:
    """Background job: submit the LLD run to the engine, poll it, and persist
    the final output layers. Runs on a daemon thread so the POST returns
    immediately and the frontend polls progress via the run status.

    ``submit=False`` attaches a poller to an engine task that is ALREADY
    running (e.g. after a Django restart or after the previous poller died
    on the short verify-only deadline) without re-submitting it.
    """
    from django.db import close_old_connections

    try:
        run = LldRun.objects.get(pk=run_id)
    except LldRun.DoesNotExist:
        return

    try:
        asv = run.approved_survey_version
        # IMMUTABILITY: never mutate the frozen Approved Survey dataset in
        # place. Deep-copy it here — the enrichment below adds per-change
        # before/after metadata for the engine's within-dataset relay.
        dataset = copy.deepcopy(
            asv.dataset if asv and isinstance(asv.dataset, dict)
            else {"type": "FeatureCollection", "features": []}
        )

        # Enrich the working copy with each approved change's before/after
        # pair (the survey change's own frozen original geometry). The engine
        # needs the old path to detect a reroute and re-lay the duct/cable
        # onto the new one. Only the working copy is touched — the stored AS
        # version is never modified.
        try:
            # NOTE: never assign a local named ``copy`` here — it shadows the
            # module-level ``import copy`` used above for deepcopy, making
            # ``copy`` local for the whole function (UnboundLocalError).
            copy_project = _survey_copy(project_id)
            if copy_project is not None:
                fresh = _approved_feature_collection(copy_project)
                by_change = {
                    (f.get("properties") or {}).get("change_id"): f
                    for f in fresh.get("features", [])
                    if (f.get("properties") or {}).get("change_id")
                }
                for feat in dataset.get("features", []):
                    props = feat.get("properties") or {}
                    cid = props.get("change_id")
                    if not cid:
                        continue
                    fresh_feat = by_change.get(cid)
                    if fresh_feat and not props.get("original_geometry"):
                        props["original_geometry"] = (fresh_feat.get("properties") or {}).get("original_geometry")
        except Exception as exc:
            logger.warning("LLD dataset enrichment failed for %s: %s", project_id, exc)

        # 1. Submit to the engine (returns immediately). Mode B (replan)
        #    re-runs the full design algorithm with the ASV as brownfield;
        #    Mode A (verify) applies the survey changes to the HLD design.
        #    Skip submission when resuming an engine task already in flight.
        if submit:
            if run.mode == LldRun.MODE_REPLAN:
                engine_lld_replan(project_id, run.lld_version, dataset)
            else:
                engine_lld_run(project_id, run.lld_version, dataset)

        # 2. Poll the engine until it completes/fails (bounded). Mode B
        #    (replan) re-runs the full QGIS design pipeline and legitimately
        #    takes 10-40 minutes — far longer than the ~1 min Mode A verify.
        #    Give replan runs a 60-minute budget, keep verify at 5.
        poll_budget = 3600 if run.mode == LldRun.MODE_REPLAN else 300
        deadline = time.time() + poll_budget
        while time.time() < deadline:
            status = engine_lld_status(project_id, run.lld_version)
            if status is None:
                time.sleep(1)
                continue
            engine_status = status.get("status")
            progress = int(status.get("progress") or 0)
            if progress != run.progress:
                run.progress = progress
                run.save(update_fields=["progress"])
            if engine_status == "completed":
                layer_list = status.get("layers") or []
                persisted = 0
                for meta in layer_list:
                    name = meta.get("name")
                    if not name:
                        continue
                    raw = engine_lld_layer(project_id, run.lld_version, name)
                    if raw is None:
                        continue
                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    feats = data.get("features", []) if isinstance(data, dict) else []
                    LldLayer.objects.update_or_create(
                        lld_run=run,
                        name=name,
                        defaults={"geojson": data, "feature_count": len(feats)},
                    )
                    persisted += 1
                run.status = LldRun.STATUS_COMPLETED
                run.progress = 100
                run.outputs = persisted
                run.validation = status.get("validation") or {}
                run.save()
                _record_event(_survey_copy(project_id), "lld", "lld_completed", None, run.lld_version)

                # BOQ/BOM regeneration: recompute quantities from the LLD
                # layers so the BOQ reflects the final (survey-corrected)
                # design, including reuse savings and rerouted lengths.
                try:
                    from ftth_hld.boq import generate_snapshot, clear_lld_cache
                    clear_lld_cache(project_id)
                    generate_snapshot(project_id, force=True)
                    logger.info(
                        "BOQ/BOM regenerated after %s/%s (LLD layers)",
                        project_id, run.lld_version,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("BOQ/BOM regeneration failed for %s: %s", project_id, exc)

                # Permit auto-analysis: keep the permit matrix (and the LLD
                # map's permit-status colours) in sync with the freshly
                # completed run. Never let a permit-engine failure break the
                # LLD completion path.
                try:
                    summary = run_permit_analysis(project_id)
                    logger.info(
                        "Permit analysis auto-run after %s/%s: %s rules fired, %s rows",
                        project_id, run.lld_version,
                        len(summary.get("rules_fired", [])), summary.get("rows_created", 0),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Permit auto-analysis failed for %s: %s", project_id, exc)

                # Permit package auto-generation (Phase 2): bundle drawings,
                # cross-sections, forms, TMPs and reports for the freshly
                # completed run so the package is ready the moment the LLD
                # finishes. Fire-and-forget like the analysis above.
                try:
                    from permits.generators.package import generate_package

                    pkg = generate_package(project_id, run.ftth_project.name or project_id)
                    logger.info(
                        "Permit package auto-generated after %s/%s: v%s, %s files, %s zip KB",
                        project_id, run.lld_version,
                        pkg.get("version"), len(pkg.get("files", [])),
                        round((pkg.get("zip_size") or 0) / 1024),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Permit package auto-generation failed for %s: %s", project_id, exc)
                return
            if engine_status == "failed":
                run.status = LldRun.STATUS_FAILED
                run.error_message = status.get("error") or "LLD engine failed"
                run.save()
                return
            time.sleep(1)

        run.status = LldRun.STATUS_FAILED
        run.error_message = "LLD run timed out"
        run.save()
    except Exception as exc:
        try:
            import traceback
            run.status = LldRun.STATUS_FAILED
            run.error_message = str(exc) + "\n" + traceback.format_exc()
            run.save(update_fields=["status", "error_message"])
        except Exception:
            pass
    finally:
        close_old_connections()


class LldRunView(APIView):
    """POST /api/ftth/lld/projects/<pid>/runs/"""
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        if getattr(request.user, "role", None) != "SUBADMIN":
            return JsonResponse({"detail": "Only planners (SUBADMIN) can run LLD."}, status=403)

        ftth = get_object_or_404(FtthProject, pk=project_id)
        asv = (
            ApprovedSurveyVersion.objects.filter(ftth_project=ftth)
            .order_by("-created_at")
            .first()
        )
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

        mode = (request.data or {}).get("mode") or LldRun.MODE_VERIFY
        if mode not in (LldRun.MODE_VERIFY, LldRun.MODE_REPLAN):
            return JsonResponse({"detail": "mode must be verify | replan"}, status=400)

        version = _next_version(LldRun.objects.filter(ftth_project=ftth), "LLD")
        run = LldRun.objects.create(
            ftth_project=ftth,
            lld_version=version,
            hld_version=asv.hld_version or HLD_VERSION,
            approved_survey_version=asv,
            algorithm_version=ALGORITHM_VERSION,
            input_dataset_version=asv.version,
            mode=mode,
            status=LldRun.STATUS_RUNNING,
            progress=0,
            run_by=request.user if request.user.is_authenticated else None,
        )

        # Orchestrate the engine asynchronously; the frontend polls progress.
        threading.Thread(
            target=_run_lld_job,
            args=(project_id, run.id),
            daemon=True,
        ).start()

        return JsonResponse({
            "lld_version": run.lld_version,
            "status": run.status,
            "mode": run.mode,
            "project_id": run.ftth_project_id,
        })


class LldVersionsView(APIView):
    """GET /api/ftth/lld/projects/<pid>/versions/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        ftth = get_object_or_404(FtthProject, pk=project_id)
        asv = (
            ApprovedSurveyVersion.objects.filter(ftth_project=ftth)
            .order_by("-created_at")
            .first()
        )
        runs = []
        for r in LldRun.objects.filter(ftth_project=ftth).select_related("approved_survey_version", "run_by"):
            run_layers = [
                {"name": l.name, "feature_count": l.feature_count}
                for l in LldLayer.objects.filter(lld_run=r)
            ]
            runs.append({
                "lld_version": r.lld_version,
                "project_id": r.ftth_project_id,
                "mode": r.mode,
                "hld_version": r.hld_version or HLD_VERSION,
                "approved_survey_version": r.approved_survey_version.version if r.approved_survey_version else None,
                "run_date": r.run_date.isoformat() if r.run_date else None,
                "run_by": (r.run_by.full_name if r.run_by and r.run_by.full_name else (r.run_by.email if r.run_by else "LLD Pipeline (auto)")),
                "algorithm_version": r.algorithm_version or ALGORITHM_VERSION,
                "input_dataset_version": r.input_dataset_version or (asv.version if asv else ""),
                "status": r.status,
                "outputs": r.outputs,
                "progress": r.progress,
                "validation": r.validation or {},
                "layers": run_layers,
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


class LldVersionsDiffView(APIView):
    """GET /api/ftth/lld/projects/<pid>/versions/diff/?from=LLD-V05&to=LLD-V06

    Tier-1 A24 — "what changed between two LLD runs" (layers, counts,
    lengths). Both versions are optional: with none given the two most
    recent completed runs are compared.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        from .diff import diff_runs, latest_pair

        get_object_or_404(FtthProject, pk=project_id)
        from_v = (request.GET.get("from") or "").strip()
        to_v = (request.GET.get("to") or "").strip()
        if not from_v or not to_v:
            pair = latest_pair(project_id)
            if pair is None:
                return JsonResponse(
                    {"detail": "At least two completed LLD runs are required for a diff."},
                    status=400,
                )
            pair_a, pair_b = pair
            from_v = from_v or pair_a.lld_version
            to_v = to_v or pair_b.lld_version
        try:
            result = diff_runs(project_id, from_v, to_v)
        except ValueError as exc:
            return JsonResponse({"detail": str(exc)}, status=404)
        return JsonResponse(result)


class LldRunStatusView(APIView):
    """GET /api/ftth/lld/projects/<pid>/runs/<lld_version>/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id, lld_version):
        ftth = get_object_or_404(FtthProject, pk=project_id)
        run = LldRun.objects.filter(ftth_project=ftth, lld_version=lld_version).order_by("-run_date").first()
        if run is None:
            return JsonResponse({"detail": "LLD run not found."}, status=404)
        layers = [
            {"name": l.name, "feature_count": l.feature_count}
            for l in LldLayer.objects.filter(lld_run=run)
        ]
        return JsonResponse({
            "lld_version": run.lld_version,
            "project_id": run.ftth_project_id,
            "status": run.status,
            "progress": run.progress,
            "outputs": run.outputs,
            "validation": run.validation or {},
            "error_message": run.error_message,
            "layers": layers,
            "run_date": run.run_date.isoformat() if run.run_date else None,
        })


class LldLayerView(APIView):
    """GET /api/ftth/lld/projects/<pid>/runs/<lld_version>/layers/<layer>/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id, lld_version, layer):
        ftth = get_object_or_404(FtthProject, pk=project_id)
        run = LldRun.objects.filter(ftth_project=ftth, lld_version=lld_version).order_by("-run_date").first()
        if run is None:
            return JsonResponse({"detail": "LLD run not found."}, status=404)
        row = LldLayer.objects.filter(lld_run=run, name=layer).first()
        if row is None:
            return JsonResponse(
                {"detail": f"Layer '{layer}' not found for this LLD run."},
                status=404,
            )
        return JsonResponse(row.geojson)


class LldDownloadView(APIView):
    """GET /api/ftth/lld/projects/<pid>/runs/<lld_version>/download/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id, lld_version):
        ftth = get_object_or_404(FtthProject, pk=project_id)
        run = LldRun.objects.filter(ftth_project=ftth, lld_version=lld_version).order_by("-run_date").first()
        if run is None:
            return JsonResponse({"detail": "LLD run not found."}, status=404)
        if run.status != LldRun.STATUS_COMPLETED:
            return JsonResponse(
                {"detail": "LLD run is not completed yet."},
                status=400,
            )
        data = engine_lld_download(project_id, lld_version)
        if data is None:
            return JsonResponse({"detail": "LLD zip not found."}, status=404)
        return HttpResponse(
            data,
            content_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{project_id}_{lld_version}_lld.zip"'
                ),
                "Content-Length": str(len(data)),
            },
        )
