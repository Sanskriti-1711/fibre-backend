"""API views for the FTTH Permit system.

Endpoints (all under ``/api/ftth/permits/``, JWT-authenticated):

* ``GET  /projects/<pid>/permits/``        — permit matrix for a project
* ``POST /projects/<pid>/permits/analyze/`` — (re)run the rule engine
* ``PATCH /permits/<permit_id>/``           — status/dates/conditions/comments
* ``GET  /summary/``                        — cross-project dashboard KPI
* ``POST /projects/<pid>/package/``         — generate the permit package
* ``GET  /projects/<pid>/package/``         — list generated package documents
* ``GET  /projects/<pid>/package/download/``— download the package zip
"""

from django.db import models as dj_models
from django.http import FileResponse, JsonResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from ftth_hld.models import FtthProject

from .generators.hld_package import generate_hld_package
from .generators.package import generate_package
from .models import PermitDocument, PermitEvent, PermitMatrix, PermitSubmission
from .rules.engine import project_summary, run_analysis
from .submissions import create_submissions, transition_submission


def _serialize(permit: PermitMatrix) -> dict:
    return {
        'permit_id': str(permit.permit_id),
        'project_id': permit.project_id,
        'route_section': permit.route_section,
        'layer': permit.layer,
        'permit_type': permit.permit_type,
        'municipality': permit.municipality,
        'permit_group': permit.permit_group,
        'authority': {
            'code': permit.authority.code if permit.authority else None,
            'name': permit.authority.name if permit.authority else None,
            'type': permit.authority.authority_type if permit.authority else None,
        },
        'rule': {
            'rule_id': permit.rule.rule_id if permit.rule else None,
            'version': permit.rule_version,
        },
        'required': permit.required,
        'blocks_construction': permit.blocks_construction,
        'status': permit.status,
        'readiness_pct': permit.readiness_pct,
        'evidence': permit.evidence,
        'documents': permit.documents,
        'analysis_notes': permit.analysis_notes,
        'submission_date': permit.submission_date.isoformat() if permit.submission_date else None,
        'approval_date': permit.approval_date.isoformat() if permit.approval_date else None,
        'expiry_date': permit.expiry_date.isoformat() if permit.expiry_date else None,
        'conditions': permit.conditions,
        'revision': permit.revision,
        'comments': permit.comments,
        'created_at': permit.created_at.isoformat() if permit.created_at else None,
    }


class PermitMatrixView(APIView):
    """GET /api/ftth/permits/projects/<pid>/permits/ — matrix for a project."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        status_filter = request.GET.get('status')
        qs = PermitMatrix.objects.filter(project_id=project_id).select_related('authority', 'rule')
        if status_filter:
            qs = qs.filter(status=status_filter)
        # A project's matrix can hold thousands of rows (one per route feature
        # per rule) — the HLD/LLD maps colour every segment, so don't truncate.
        permits = [_serialize(pm) for pm in qs[:20000]]
        return JsonResponse(
            {
                **project_summary(project_id),
                'permits': permits,
            }
        )


class PermitAnalyzeView(APIView):
    """POST /api/ftth/permits/projects/<pid>/permits/analyze/ — run rules."""

    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        if not FtthProject.objects.filter(pk=project_id).exists():
            return JsonResponse({'detail': 'Project not found.'}, status=404)
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
            return JsonResponse({'detail': 'Permit not found.'}, status=404)

        data = request.data or {}
        simple_fields = (
            'status',
            'conditions',
            'comments',
            'municipality',
            'evidence',
            'documents',
            'analysis_notes',
        )
        updated = []
        for field in simple_fields:
            if field in data:
                setattr(permit, field, data[field])
                updated.append(field)

        for field in ('submission_date', 'approval_date', 'expiry_date'):
            if field in data and data[field]:
                setattr(permit, field, data[field])
                updated.append(field)

        if updated:
            permit.save(update_fields=[*updated, 'updated_at'])
            PermitEvent.objects.create(
                permit=permit,
                event='STATUS_UPDATE' if 'status' in updated else 'DETAIL_UPDATE',
                detail={'fields': updated, 'by': request.user.email if request.user else None},
            )
        return JsonResponse(_serialize(permit))


def _serialize_submission(sub: PermitSubmission) -> dict:
    """Serialize a submission with its application-level state + row info."""
    rows = list(sub.permit_rows.all())
    row_statuses: dict[str, int] = {}
    for r in rows:
        row_statuses[r.status] = row_statuses.get(r.status, 0) + 1
    return {
        'submission_id': str(sub.id),
        'project_id': sub.project_id,
        'project_name': sub.project.name if sub.project_id else None,
        'authority': {
            'code': sub.authority.code if sub.authority else None,
            'name': sub.authority.name if sub.authority else None,
            'type': sub.authority.authority_type if sub.authority else None,
        },
        'permit_type': sub.permit_type,
        'permit_group': sub.permit_group,
        'label': sub.label,
        'status': sub.status,
        'submission_date': sub.submission_date.isoformat() if sub.submission_date else None,
        'reference': sub.reference,
        'notes': sub.notes,
        'conditions': sub.conditions,
        'approval_date': sub.approval_date.isoformat() if sub.approval_date else None,
        'expiry_date': sub.expiry_date.isoformat() if sub.expiry_date else None,
        'revision': sub.revision,
        'package_version': sub.package_version,
        'sync_source': sub.sync_source,
        'section_count': len(rows),
        'row_statuses': row_statuses,
        'permit_ids': [str(r.permit_id) for r in rows],
        'created_at': sub.created_at.isoformat() if sub.created_at else None,
    }


class PermitSubmissionListView(APIView):
    """Phase-3 submissions — the application records sent to authorities.

    * ``GET  /api/ftth/permits/submissions/`` — list (filter: ``project_id``,
      ``status``, ``authority``).
    * ``POST /api/ftth/permits/submissions/`` — submit READY permits as one
      submission per (project × authority × type × street). Body:
      ``{permit_ids: [...], reference?, notes?, package_version?}``.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = PermitSubmission.objects.select_related('project', 'authority').all()
        if request.GET.get('project_id'):
            qs = qs.filter(project_id=request.GET['project_id'])
        if request.GET.get('status'):
            qs = qs.filter(status=request.GET['status'])
        if request.GET.get('authority'):
            qs = qs.filter(authority__code=request.GET['authority'])
        submissions = [_serialize_submission(s) for s in qs[:2000]]
        return JsonResponse({'total': len(submissions), 'submissions': submissions})

    def post(self, request):
        data = request.data or {}
        permit_ids = data.get('permit_ids') or []
        if not permit_ids:
            return JsonResponse(
                {'detail': 'permit_ids is required — the READY permits to submit.'},
                status=400,
            )
        try:
            submissions = create_submissions(
                [str(pid) for pid in permit_ids],
                user=request.user,
                reference=(data.get('reference') or '').strip(),
                notes=(data.get('notes') or '').strip(),
                package_version=data.get('package_version'),
                sync_source=(data.get('sync_source') or 'manual'),
            )
        except ValueError as exc:
            return JsonResponse({'detail': str(exc)}, status=400)
        return JsonResponse(
            {
                'created': len(submissions),
                'submissions': [_serialize_submission(s) for s in submissions],
            },
            status=201,
        )


class PermitSubmissionDetailView(APIView):
    """GET /api/ftth/permits/submissions/<id>/ — one submission + its rows."""

    permission_classes = [IsAuthenticated]

    def get(self, request, submission_id):
        sub = (
            PermitSubmission.objects.select_related('project', 'authority')
            .filter(pk=submission_id)
            .first()
        )
        if sub is None:
            return JsonResponse({'detail': 'Submission not found.'}, status=404)
        return JsonResponse(_serialize_submission(sub))


class PermitSubmissionTransitionView(APIView):
    """POST /api/ftth/permits/submissions/<id>/transition/ — review action.

    Body: ``{status, reference?, notes?, conditions?, expiry_date?,
    sync_source?}``. Allowed edges:

    * ``submitted → under_review``   (authority acknowledged)
    * ``under_review → approved``    (capture conditions / expiry_date)
    * ``under_review → rejected``    (notes = rejection reason)
    * ``rejected → submitted``       (re-submission — revision + 1)
    * ``approved → closed``

    ``sync_source`` lets a future portal/API poller reuse this endpoint for
    automated status sync while keeping the audit trail honest.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, submission_id):
        sub = PermitSubmission.objects.filter(pk=submission_id).first()
        if sub is None:
            return JsonResponse({'detail': 'Submission not found.'}, status=404)
        data = request.data or {}
        to_status = data.get('status')
        if not to_status:
            return JsonResponse({'detail': 'status is required.'}, status=400)
        try:
            transition_submission(
                sub,
                to_status,
                user=request.user,
                reference=data.get('reference'),
                notes=data.get('notes'),
                conditions=data.get('conditions'),
                expiry_date=data.get('expiry_date'),
                sync_source=(data.get('sync_source') or 'manual'),
            )
        except ValueError as exc:
            return JsonResponse({'detail': str(exc)}, status=400)
        sub.refresh_from_db()
        return JsonResponse(_serialize_submission(sub))


class PermitAllView(APIView):
    """GET /api/ftth/permits/ — every permit across projects (tracker feed).

    Optionally filtered: ``?status=``, ``?project_id=``, ``?q=`` (permit type
    / route section substring). Cap at 20000 rows — the tracker page renders
    every row and its KPIs count from the feed, so truncating (as the old
    1000-row cap did) silently hid permits and under-counted stats.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = PermitMatrix.objects.select_related('authority', 'rule', 'project').all()
        status_filter = request.GET.get('status')
        project_filter = request.GET.get('project_id')
        q = (request.GET.get('q') or '').strip().lower()

        if status_filter:
            qs = qs.filter(status=status_filter)
        if project_filter:
            qs = qs.filter(project_id=project_filter)
        if q:
            qs = qs.filter(
                dj_models.Q(permit_type__icontains=q) | dj_models.Q(route_section__icontains=q)
            )

        permits = []
        for pm in qs[:20000]:
            item = _serialize(pm)
            item['project_name'] = pm.project.name or pm.project_id
            permits.append(item)
        return JsonResponse({'total': len(permits), 'permits': permits})


# Worst-first status priority — used to aggregate a street group's status
# to its least-favourable member (matches the tracker page's grouping).
_GROUP_STATUS_PRIORITY = {
    'rejected': 9,
    'under_review': 8,
    'submitted': 7,
    'evidence_required': 6,
    'identified': 5,
    'ready': 4,
    'approved': 3,
    'closed': 2,
    'not_required': 1,
}


class PermitSummaryView(APIView):
    """GET /api/ftth/permits/summary/ — cross-project KPI for the dashboard.

    Returns both raw segment counts (``total``) and the clubbed street-level
    group counts (``total_groups``, per-project ``groups``) so the dashboard
    can surface one permit per street instead of one per segment.

    Uses DB-level aggregates instead of materializing every PermitMatrix row
    in Python — important when the matrix holds tens of thousands of rows.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # ── Raw status counts (one aggregate query) ───────────────────
        from django.db.models import Count

        # PermitMatrix's primary key is ``permit_id`` (not ``id``), so the
        # aggregate must name it explicitly — ``Count("id")`` raises
        # FieldError and 500s the whole KPI endpoint.
        status_agg = (
            PermitMatrix.objects.values('status').annotate(n=Count('permit_id')).order_by('status')
        )
        counts = {row['status']: row['n'] for row in status_agg}
        total = sum(counts.values())

        # ── Per-project segment counts (one aggregate query) ──────────
        per_project_agg = (
            PermitMatrix.objects.values('project_id')
            .annotate(n=Count('permit_id'))
            .order_by('project_id')
        )
        per_project = {row['project_id']: row['n'] for row in per_project_agg}

        # ── Worst-first street-group status via a single raw SQL query ─
        # The dashboard groups one permit per (project, rule, route_section)
        # and picks the least-favourable status in each group. That "group
        # by + MIN(priority)" reduction is easier in SQL than in the ORM.
        # We map status → priority in SQL so the database does the heavy
        # lifting; the Python side only counts the resulting groups.
        #
        # Why cursor and not Model.objects.raw(): raw() requires the PK as the
        # first selected column (PermitMatrix PK is permit_id, not id) and the
        # window query returns one row per segment that we dedupe in Python.
        # Using a cursor avoids the "Raw query must include the primary key"
        # 500 that broke /permits/summary/ on any DB with permit rows.
        from django.db import connection as _conn  # local import to avoid cycle

        priority_case = (
            'CASE status '
            "WHEN 'rejected' THEN 9 "
            "WHEN 'under_review' THEN 8 "
            "WHEN 'submitted' THEN 7 "
            "WHEN 'evidence_required' THEN 6 "
            "WHEN 'identified' THEN 5 "
            "WHEN 'ready' THEN 4 "
            "WHEN 'approved' THEN 3 "
            "WHEN 'closed' THEN 2 "
            "WHEN 'not_required' THEN 1 "
            'ELSE 0 END'
        )
        group_sql = (
            'SELECT '
            '  pm.project_id, '
            '  pm.rule_id, '
            "  COALESCE(NULLIF(pm.permit_group, ''), pm.route_section) AS group_key, "
            '  FIRST_VALUE(pm.status) OVER ('
            "    PARTITION BY pm.project_id, COALESCE(NULLIF(pm.permit_group, ''), pm.route_section), pm.rule_id "
            '    ORDER BY ' + priority_case + ' DESC, pm.permit_id'
            '  ) AS group_status '
            'FROM ftth_permit_matrix pm'
        )
        try:
            with _conn.cursor() as cur:
                cur.execute(group_sql)
                _group_rows = cur.fetchall()  # (project_id, rule_id, group_key, group_status)
        except Exception:
            # Fallback: Python-side worst-first grouping — never 500 the KPI.
            _group_rows = []
            for pm in PermitMatrix.objects.values(
                'project_id', 'rule_id', 'permit_group', 'route_section', 'status', 'permit_id'
            ):
                gk = (pm['permit_group'] or '').strip() or pm['route_section']
                # priority lookup inline to avoid extra query
                prio = _GROUP_STATUS_PRIORITY.get(pm['status'], 0)
                _group_rows.append(
                    (pm['project_id'], pm['rule_id'], gk, pm['status'], prio, str(pm['permit_id']))
                )
            # Reduce to worst per group in Python (max prio, tie-break permit_id)
            _best: dict[tuple, tuple] = {}  # key -> (prio, permit_id, status)
            for pid, rid, gk, st, prio, perm_id in _group_rows:
                key = (str(pid), str(rid or ''), str(gk))
                cur_best = _best.get(key)
                if (
                    cur_best is None
                    or prio > cur_best[0]
                    or (prio == cur_best[0] and perm_id < cur_best[1])
                ):
                    _best[key] = (prio, perm_id, st)
            _group_rows = [(k[0], k[1], k[2], v[2]) for k, v in _best.items()]
            # mark as already-grouped so the loop below skips dedupe
            _already_grouped = True
        else:
            _already_grouped = False

        group_counts: dict[str, int] = {}
        per_project_groups: dict[str, int] = {}
        seen_groups: set[tuple] = set()
        if _already_grouped:
            for project_id, rule_id, group_key, group_status in _group_rows:
                key = (str(project_id), str(rule_id or ''), str(group_key))
                # rows are already one per group in fallback path
                seen_groups.add(key)
                group_counts[group_status] = group_counts.get(group_status, 0) + 1
                per_project_groups[str(project_id)] = per_project_groups.get(str(project_id), 0) + 1
        else:
            for project_id, rule_id, group_key, group_status in _group_rows:
                key = (str(project_id), str(rule_id or ''), str(group_key))
                if key in seen_groups:
                    continue
                seen_groups.add(key)
                group_counts[group_status] = group_counts.get(group_status, 0) + 1
                per_project_groups[str(project_id)] = per_project_groups.get(str(project_id), 0) + 1

        ready = group_counts.get(PermitMatrix.STATUS_READY, 0) + group_counts.get(
            PermitMatrix.STATUS_APPROVED, 0
        )

        # ── Project names (one bulk query) ────────────────────────────
        names = {
            str(p.pk): p.name for p in FtthProject.objects.filter(pk__in=list(per_project.keys()))
        }

        return JsonResponse(
            {
                'total': total,
                'total_groups': len(seen_groups),
                'by_status': counts,
                'group_by_status': group_counts,
                'ready': ready,
                'projects_with_permits': len(per_project),
                'projects': [
                    {
                        'project_id': pid,
                        'name': names.get(pid, pid),
                        'permits': n,
                        'groups': per_project_groups.get(pid, 0),
                    }
                    for pid, n in per_project.items()
                ],
            }
        )


class HldPermitPackageView(APIView):
    """Generate/list the preliminary HLD permit-planning package."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        if not FtthProject.objects.filter(pk=project_id).exists():
            return JsonResponse({'detail': 'Project not found.'}, status=404)
        latest_version = (
            PermitDocument.objects.filter(
                permit__project_id=project_id,
                name__startswith='HLD detailed preliminary permit package',
            )
            .order_by('-version')
            .values_list('version', flat=True)
            .first()
        )
        docs = (
            PermitDocument.objects.filter(
                permit__project_id=project_id,
                version=latest_version,
            ).order_by('kind', 'name')
            if latest_version is not None
            else PermitDocument.objects.none()
        )
        return JsonResponse(
            {
                'project_id': project_id,
                'package_type': 'HLD_PRELIMINARY',
                'latest_version': latest_version,
                'total_files': docs.count(),
                'files': [
                    {
                        'document_id': str(d.id),
                        'name': d.name,
                        'kind': d.kind,
                        'version': d.version,
                        'filename': (d.file.name or '').split('/')[-1],
                        'url': d.file.url if d.file else '',
                    }
                    for d in docs
                ],
            }
        )

    def post(self, request, project_id):
        ftth = FtthProject.objects.filter(pk=project_id).first()
        if ftth is None:
            return JsonResponse({'detail': 'Project not found.'}, status=404)
        try:
            return JsonResponse(
                generate_hld_package(project_id, ftth.name or project_id), status=201
            )
        except ValueError as exc:
            return JsonResponse({'detail': str(exc)}, status=400)
        except Exception as exc:
            return JsonResponse(
                {'detail': f'HLD preliminary package generation failed: {exc}'}, status=500
            )


class PermitPackageView(APIView):
    """Permit package for a project (Phase 2).

    * ``POST /projects/<pid>/package/``  — generate (drawings, cross-sections,
      forms, TMPs, reports) as a versioned zip + PermitDocument rows, then
      promote document evidence (readiness checker).
    * ``GET  /projects/<pid>/package/``  — list the generated package files.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        if not FtthProject.objects.filter(pk=project_id).exists():
            return JsonResponse({'detail': 'Project not found.'}, status=404)
        docs = PermitDocument.objects.filter(permit__project_id=project_id).order_by(
            '-version', 'kind', 'name'
        )
        files = [
            {
                'document_id': str(d.id),
                'name': d.name,
                'kind': d.kind,
                'version': d.version,
                'filename': (d.file.name or '').split('/')[-1],
                'url': d.file.url if d.file else '',
                'created_at': d.created_at.isoformat() if d.created_at else None,
            }
            for d in docs
        ]
        versions = sorted({d.version for d in docs})
        return JsonResponse(
            {
                'project_id': project_id,
                'versions': versions,
                'total_files': len(files),
                'files': files,
            }
        )

    def post(self, request, project_id):
        ftth = FtthProject.objects.filter(pk=project_id).first()
        if ftth is None:
            return JsonResponse({'detail': 'Project not found.'}, status=404)
        try:
            summary = generate_package(project_id, ftth.name or project_id)
        except ValueError as exc:
            return JsonResponse({'detail': str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return JsonResponse({'detail': f'Package generation failed: {exc}'}, status=500)
        return JsonResponse(summary, status=201)


class HldPermitPackageDownloadView(APIView):
    """Download the latest HLD preliminary planning package."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        if not FtthProject.objects.filter(pk=project_id).exists():
            return JsonResponse({'detail': 'Project not found.'}, status=404)
        doc = (
            PermitDocument.objects.filter(
                permit__project_id=project_id,
                name__startswith='HLD detailed preliminary permit package',
            )
            .order_by('-version')
            .first()
        )
        if doc is None or not doc.file:
            return JsonResponse({'detail': 'No HLD preliminary package generated yet.'}, status=404)
        try:
            return FileResponse(
                doc.file.open('rb'),
                content_type='application/zip',
                as_attachment=True,
                filename=f'hld_preliminary_permit_package_v{doc.version}_{project_id}.zip',
            )
        except FileNotFoundError:
            return JsonResponse({'detail': 'Package file missing on disk.'}, status=404)


class PermitPackageDownloadView(APIView):
    """GET /api/ftth/permits/projects/<pid>/package/download/ — the package zip.

    Serves the most recent zip document for the project.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        if not FtthProject.objects.filter(pk=project_id).exists():
            return JsonResponse({'detail': 'Project not found.'}, status=404)
        docs = PermitDocument.objects.filter(
            permit__project_id=project_id, name__startswith='permit_package_v'
        ).order_by('-version')
        doc = docs.first()
        if doc is None or not doc.file:
            return JsonResponse(
                {'detail': 'No permit package generated yet — POST …/package/ first.'},
                status=404,
            )
        try:
            response = FileResponse(
                doc.file.open('rb'),
                content_type='application/zip',
                as_attachment=True,
                filename=f'permit_package_v{doc.version}_{project_id}.zip',
            )
            return response
        except FileNotFoundError:
            return JsonResponse({'detail': 'Package file missing on disk.'}, status=404)
