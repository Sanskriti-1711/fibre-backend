import contextlib
import logging
from datetime import timedelta

from django.db.models import (
    Case,
    Count,
    IntegerField,
    Q,
    When,
)
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from assignments.api.serializers import AssignmentJobDetailSerializer, AssignmentJobSerializer
from assignments.models import AssignmentJob
from projects.models import Feature
from supervision.models import FeaturePhoto
from users.models import User
from users.permissions import IsSubadmin

logger = logging.getLogger(__name__)


def engineer_feature_queryset(engineer_id):
    """Every feature an engineer is responsible for, across all three scopes.

    A project-scope job is the normal supervision assignment and grants the
    whole project — that is also what ``_engineer_can_access`` checks. Joining on
    ``assignment_jobs__assignee_id`` alone only matches feature-scope jobs, so it
    reports zero work for a project the engineer can actually open. Layer-scope
    jobs expand to their layer, feature-scope jobs to their feature.
    """
    jobs = AssignmentJob.objects.filter(assignee_id=engineer_id)

    condition = Q(
        project_id__in=jobs.filter(scope=AssignmentJob.SCOPE_PROJECT).values('project_id')
    )
    condition |= Q(id__in=jobs.filter(scope=AssignmentJob.SCOPE_FEATURE).values('feature_id'))
    for pair in jobs.filter(scope=AssignmentJob.SCOPE_LAYER).values('project_id', 'layer_id'):
        condition |= Q(project_id=pair['project_id'], layer_id=pair['layer_id'])

    # A polygon job is already resolved to feature ids when the job is saved
    # (see assignments.coverage); expanding the stored snapshot here keeps the
    # access rule identical to what the map paints as editable.
    # A span job is resolved to its components the same way, so both scopes
    # expand from the snapshot rather than from a second walk of the layers.
    polygon_feature_ids: set[str] = set()
    for feature_ids in jobs.filter(
        scope__in=[AssignmentJob.SCOPE_POLYGON, AssignmentJob.SCOPE_SPAN]
    ).values_list('covered_feature_ids', flat=True):
        polygon_feature_ids.update(feature_ids or [])
    if polygon_feature_ids:
        condition |= Q(id__in=polygon_feature_ids)

    return Feature.objects.filter(condition).distinct()


def record_stage_events(project_ids, event, actor=None, entity_id='', metadata=None):
    """Write one supervision StageEvent per project a bulk action touched.

    Never raises: the action itself has already succeeded, and a timeline row is
    not worth failing an approval over (the same rule as `ftth_lld._record_event`).
    """
    from projects.models.stage_event import StageEvent

    for project_id in {str(pid) for pid in project_ids if pid}:
        with contextlib.suppress(Exception):
            StageEvent.objects.create(
                project_id=project_id,
                stage=StageEvent.Stage.SUPERVISION,
                event=event,
                actor=actor if getattr(actor, 'is_authenticated', False) else None,
                entity_id=entity_id or '',
                metadata=metadata or {},
            )


class AssignmentJobListCreateAPIView(generics.ListCreateAPIView):
    queryset = AssignmentJob.objects.select_related('project', 'feature', 'assignee')

    def get_permissions(self):
        # Reading the job list is how the planners' board and the engineers'
        # "My projects" stay in sync; creating one is an assignment, which only
        # a planner makes.
        if self.request.method == 'POST':
            return [IsSubadmin()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.request.method == 'GET':
            return AssignmentJobDetailSerializer
        return AssignmentJobSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        project_id = self.request.query_params.get('project')
        scope = self.request.query_params.get('scope')
        layer_id = self.request.query_params.get('layer_id')
        assignee_id = self.request.query_params.get('assignee')

        if project_id:
            qs = qs.filter(project_id=project_id)
        if scope:
            qs = qs.filter(scope=scope)
        if layer_id:
            qs = qs.filter(layer_id=layer_id)
        if assignee_id:
            qs = qs.filter(assignee_id=assignee_id)

        return qs.order_by('scope', 'layer_id', 'created_at')

    def perform_create(self, serializer):
        job = serializer.save()
        # The assignment is the supervision stage's first event: it is what puts
        # a feature in front of an engineer (INTEGRATION_PLAN.md §3.4).
        record_stage_events(
            [job.project_id],
            'feature_assigned',
            actor=self.request.user,
            entity_id=str(job.feature_id or job.layer_id or job.span_id or job.project_id),
            metadata={'scope': job.scope, 'assignee': str(job.assignee_id)},
        )


class AssignmentJobDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    queryset = AssignmentJob.objects.select_related('project', 'feature', 'assignee')

    def get_permissions(self):
        # Reassigning or deleting a job is a planner action. Without this the
        # view was a plain RetrieveUpdateDestroyAPIView over every job, so any
        # engineer could delete another planner's assignment by uuid.
        if self.request.method in ('PUT', 'PATCH', 'DELETE'):
            return [IsSubadmin()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.request.method == 'GET':
            return AssignmentJobDetailSerializer
        return AssignmentJobSerializer


class AssignmentJobSummaryAPIView(APIView):
    """Aggregated assignment counts, with the features each job covers.

    Counts alone say how many supervisions exist, not how much work is behind
    them: a project job and a single-span job both counted as "1". Each record
    now carries its features (id, layer and status) plus a status breakdown, and
    the payload carries the totals for the whole response.
    """

    def get(self, request):
        project_id = request.query_params.get('project')
        assignee_id = request.query_params.get('assignee')
        if not project_id and not assignee_id:
            return Response(
                {'detail': 'Either project or assignee query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        assignments = AssignmentJob.objects.select_related('project', 'feature', 'assignee')

        if project_id:
            assignments = assignments.filter(project_id=project_id)

        if assignee_id:
            assignments = assignments.filter(assignee_id=assignee_id)

        assignments = assignments.order_by('scope', 'layer_id', 'created_at')

        jobs = list(assignments)
        serializer = AssignmentJobDetailSerializer(jobs, many=True)
        covered = self._covered_features(jobs)

        grouped: dict[str, list] = {
            'project': [],
            'layer': [],
            'feature': [],
            'polygon': [],
            'span': [],
        }
        status_by_id: dict[str, str] = {}
        for job, record in zip(jobs, serializer.data):
            features = covered.get(str(job.id), [])
            record['feature_count'] = len(features)
            record['features'] = features
            record['feature_status_counts'] = self._status_counts(features)
            for feature in features:
                status_by_id[feature['id']] = feature['status']
            grouped.setdefault(record['scope'], []).append(record)

        stats: dict[str, int] = {'total': len(status_by_id)}
        for feature_status in status_by_id.values():
            stats[feature_status] = stats.get(feature_status, 0) + 1

        return Response(
            {
                'project_id': project_id,
                'counts': {k: len(v) for k, v in grouped.items()},
                'feature_total': len(status_by_id),
                'stats': stats,
                'assignments': grouped,
            }
        )

    @staticmethod
    def _covered_features(jobs):
        """The feature rows each assignment covers, from one query.

        A project job covers its whole project, a layer job its layer, a feature
        job that row, and a polygon job the snapshot taken when it was saved —
        the same expansion ``engineer_feature_queryset`` uses for access, so the
        summary cannot disagree with what an engineer can open.
        """
        project_ids: set = set()
        layer_pairs: set = set()
        feature_ids: set = set()
        polygon_ids: set = set()
        for job in jobs:
            if job.scope == AssignmentJob.SCOPE_PROJECT:
                project_ids.add(job.project_id)
            elif job.scope == AssignmentJob.SCOPE_LAYER and job.layer_id:
                layer_pairs.add((job.project_id, job.layer_id))
            elif job.scope == AssignmentJob.SCOPE_FEATURE and job.feature_id:
                feature_ids.add(job.feature_id)
            elif job.scope in (AssignmentJob.SCOPE_POLYGON, AssignmentJob.SCOPE_SPAN):
                polygon_ids.update(job.covered_feature_ids or [])

        if not (project_ids or layer_pairs or feature_ids or polygon_ids):
            return {}

        condition = Q(pk__in=[])
        if project_ids:
            condition |= Q(project_id__in=project_ids)
        for project_key, layer_key in layer_pairs:
            condition |= Q(project_id=project_key, layer_id=layer_key)
        if feature_ids:
            condition |= Q(id__in=feature_ids)
        if polygon_ids:
            condition |= Q(id__in=polygon_ids)

        rows = Feature.objects.filter(condition).values(
            'id', 'project_id', 'layer_id', 'layer_name', 'status'
        )
        by_project: dict = {}
        by_layer: dict = {}
        by_feature: dict = {}
        by_id: dict = {}
        for row in rows:
            entry = {
                'id': str(row['id']),
                'layer_id': row['layer_id'],
                'layer_name': row['layer_name'],
                'status': row['status'],
            }
            by_project.setdefault(row['project_id'], []).append(entry)
            by_layer.setdefault((row['project_id'], row['layer_id']), []).append(entry)
            by_feature.setdefault(row['id'], []).append(entry)
            by_id[row['id']] = entry

        covered: dict[str, list] = {}
        for job in jobs:
            if job.scope == AssignmentJob.SCOPE_PROJECT:
                covered[str(job.id)] = by_project.get(job.project_id, [])
            elif job.scope == AssignmentJob.SCOPE_LAYER:
                covered[str(job.id)] = by_layer.get((job.project_id, job.layer_id), [])
            elif job.scope == AssignmentJob.SCOPE_FEATURE:
                covered[str(job.id)] = by_feature.get(job.feature_id, [])
            else:
                covered[str(job.id)] = [
                    by_id[feature_id]
                    for feature_id in (job.covered_feature_ids or [])
                    if feature_id in by_id
                ]
        return covered

    @staticmethod
    def _status_counts(features):
        counts: dict[str, int] = {}
        for feature in features:
            counts[feature['status']] = counts.get(feature['status'], 0) + 1
        return counts


class JobAssignmentsListAPIView(APIView):
    """One row per assignment job, whatever its scope.

    Built from `AssignmentJob`, not from `Feature`. The previous version walked
    every imported feature (13,952 on the current database) and ran one
    aggregate query per project/layer job inside that loop, which is what made a
    bare request take ~14 s. It also only included project-scope jobs when
    `engineer` was passed, so "All jobs" came back empty (`count: 0`) while
    `stats` counted the whole feature table — the page and its counters
    disagreed.

    Aggregates for project/layer jobs now come from one grouped query, and
    `stats` describes the features those jobs actually cover, so an empty job
    list reports zeros.
    """

    def get(self, request):
        params = request.query_params
        search = params.get('search', '').strip()
        project_id = params.get('project')
        status_filter = params.get('status')
        engineer_id = params.get('engineer')
        scope_filter = params.get('scope')
        layer_id = params.get('layer')

        # Every job that matches the non-status filters. Status is filtered
        # after the fact because, for a project/layer job, it is an aggregate of
        # the features under it rather than a column.
        jobs = list(self._job_queryset(project_id, layer_id, engineer_id, scope_filter, search))
        counts, layer_names = self._feature_counts(jobs)
        progress = self._field_progress(jobs)

        # `stats` keeps describing every job the query matches, so the counters
        # do not move when a status tab is selected.
        stats = self._stats(jobs, counts)

        rows = [self._job_row(job, counts, layer_names, progress) for job in jobs]
        if status_filter:
            rows = [row for row in rows if row['status'] == status_filter]

        rows.sort(key=lambda row: row['created_at'], reverse=True)

        page = int(params.get('page', 1))
        page_size = int(params.get('page_size', 20))
        start = (page - 1) * page_size

        return Response(
            {
                'count': len(rows),
                'page': page,
                'page_size': page_size,
                'results': rows[start : start + page_size],
                'stats': stats,
            }
        )

    @staticmethod
    def _job_queryset(project_id, layer_id, engineer_id, scope_filter, search):
        qs = AssignmentJob.objects.select_related('project', 'assignee', 'feature')
        if project_id:
            qs = qs.filter(project_id=project_id)
        if layer_id:
            qs = qs.filter(layer_id=layer_id)
        if engineer_id:
            qs = qs.filter(assignee_id=engineer_id)
        if scope_filter:
            qs = qs.filter(scope=scope_filter)
        if search:
            qs = qs.filter(
                Q(id__icontains=search)
                | Q(project__name__icontains=search)
                | Q(assignee__email__icontains=search)
            )
        return qs

    @staticmethod
    def _feature_counts(jobs):
        """Status counts per project, per (project, layer) and per polygon job.

        A project job's `feature_count`/status is the whole project; a layer
        job's is its layer. A polygon job counts exactly the features its area
        covered when it was saved. Features whose layer_id differs between rows
        are summed per (project, layer) pair, so a layer job cannot pick up
        another layer's features.
        """
        project_ids = {job.project_id for job in jobs if job.scope == AssignmentJob.SCOPE_PROJECT}
        layer_pairs = {
            (job.project_id, job.layer_id)
            for job in jobs
            if job.scope in (AssignmentJob.SCOPE_LAYER, AssignmentJob.SCOPE_SPAN)
            and job.layer_id
        }
        by_project: dict = {}
        by_layer: dict = {}
        by_polygon: dict = {}
        layer_names: dict = {}

        for job in jobs:
            if job.scope not in (AssignmentJob.SCOPE_POLYGON, AssignmentJob.SCOPE_SPAN) or not job.covered_feature_ids:
                continue
            rows = (
                Feature.objects.filter(id__in=job.covered_feature_ids)
                .values('status')
                .annotate(count=Count('id'))
            )
            by_polygon[job.id] = {row['status']: row['count'] for row in rows}

        if not project_ids and not layer_pairs:
            return {'project': by_project, 'layer': by_layer, 'polygon': by_polygon}, layer_names

        rows = (
            Feature.objects.filter(project_id__in=project_ids | {p for p, _ in layer_pairs})
            .values('project_id', 'layer_id', 'layer_name', 'status')
            .annotate(count=Count('id'))
        )
        for row in rows:
            project_key = row['project_id']
            layer_key = (project_key, row['layer_id'])
            count = row['count']
            if project_key in project_ids:
                per_status = by_project.setdefault(project_key, {})
                per_status[row['status']] = per_status.get(row['status'], 0) + count
            if layer_key in layer_pairs:
                per_status = by_layer.setdefault(layer_key, {})
                per_status[row['status']] = per_status.get(row['status'], 0) + count
                layer_names[layer_key] = row['layer_name']

        return {'project': by_project, 'layer': by_layer, 'polygon': by_polygon}, layer_names

    @staticmethod
    def _expected_readings(feature):
        """How many readings the layer's field schema asks for."""
        schema = feature.field_schema
        if isinstance(schema, (list, dict)):
            return len(schema)
        properties = feature.properties if isinstance(feature.properties, dict) else {}
        return len(properties)

    @classmethod
    def _field_progress(cls, jobs):
        """Readings and site photos recorded, per project, layer and feature.

        A layer or project job has no single set of readings to report, so its
        row says how many of the features it covers carry evidence at all. All
        of it comes from grouped queries — one for the aggregate readings, one
        for their photos, one for the single-feature jobs on the page — rather
        than a query per row.
        """
        project_ids = {
            job.project_id for job in jobs if job.scope == AssignmentJob.SCOPE_PROJECT
        }
        layer_pairs = {
            (job.project_id, job.layer_id)
            for job in jobs
            if job.scope == AssignmentJob.SCOPE_LAYER
        }
        feature_ids = [
            job.feature_id
            for job in jobs
            if job.scope == AssignmentJob.SCOPE_FEATURE and job.feature_id
        ]

        progress = {'project': {}, 'layer': {}, 'feature': {}, 'polygon': {}}
        measured = Q(field_measurements__isnull=False) & ~Q(field_measurements={})
        scoped_project_ids = project_ids | {project_id for project_id, _ in layer_pairs}

        def bucket(kind, key):
            return progress[kind].setdefault(
                key, {'features': 0, 'measured_features': 0, 'photos': 0}
            )

        if scoped_project_ids:
            rows = (
                Feature.objects.filter(project_id__in=scoped_project_ids)
                .values('project_id', 'layer_id')
                .annotate(total=Count('id'), measured=Count('id', filter=measured))
            )
            for row in rows:
                if row['project_id'] in project_ids:
                    entry = bucket('project', row['project_id'])
                    entry['features'] += row['total']
                    entry['measured_features'] += row['measured']
                layer_key = (row['project_id'], row['layer_id'])
                if layer_key in layer_pairs:
                    entry = bucket('layer', layer_key)
                    entry['features'] += row['total']
                    entry['measured_features'] += row['measured']

            photo_rows = (
                FeaturePhoto.objects.filter(feature__project_id__in=scoped_project_ids)
                .values('feature__project_id', 'feature__layer_id')
                .annotate(photos=Count('id'))
            )
            for row in photo_rows:
                project_key = row['feature__project_id']
                if project_key in project_ids:
                    bucket('project', project_key)['photos'] += row['photos']
                layer_key = (project_key, row['feature__layer_id'])
                if layer_key in layer_pairs:
                    bucket('layer', layer_key)['photos'] += row['photos']

        for job in jobs:
            if job.scope not in (AssignmentJob.SCOPE_POLYGON, AssignmentJob.SCOPE_SPAN) or not job.covered_feature_ids:
                continue
            polygon_features = Feature.objects.filter(id__in=job.covered_feature_ids)
            progress['polygon'][job.id] = {
                'features': polygon_features.count(),
                'measured_features': polygon_features.filter(measured).count(),
                'photos': FeaturePhoto.objects.filter(
                    feature_id__in=job.covered_feature_ids
                ).count(),
            }

        if feature_ids:
            photo_counts = dict(
                FeaturePhoto.objects.filter(feature_id__in=feature_ids)
                .values_list('feature_id')
                .annotate(count=Count('id'))
            )
            for feature in Feature.objects.filter(id__in=feature_ids):
                readings = feature.field_measurements if isinstance(feature.field_measurements, dict) else {}
                counted = len(readings)
                progress['feature'][feature.id] = {
                    'features': 1,
                    'measured_features': 1 if counted else 0,
                    'readings': counted,
                    'expected_readings': cls._expected_readings(feature),
                    'photos': photo_counts.get(feature.id, 0),
                }

        return progress

    def _job_row(self, job, counts, layer_names, progress):
        base = {
            'id': str(job.id),
            'project': {
                'id': str(job.project.id),
                'name': job.project.name,
            },
            'scope': job.scope,
            'scope_display': job.get_scope_display(),
            'assignee': {
                'id': str(job.assignee.id),
                'email': job.assignee.email,
                'full_name': job.assignee.full_name,
            },
            'created_at': job.created_at.isoformat(),
        }

        if job.scope == AssignmentJob.SCOPE_FEATURE and job.feature:
            feature = job.feature
            base['feature'] = {
                'id': str(feature.id),
                'status': feature.status,
                'layer_id': feature.layer_id,
                'layer_name': feature.layer_name,
            }
            base['feature_count'] = 1
            base['field_progress'] = progress['feature'].get(feature.id) or {
                'features': 1,
                'measured_features': 0,
                'readings': 0,
                'expected_readings': self._expected_readings(feature),
                'photos': 0,
            }
            base['status'] = feature.status
            base['status_display'] = self._get_status_display(feature.status)
            return base

        if job.scope == AssignmentJob.SCOPE_SPAN:
            base['span'] = {
                'id': job.span_id or '',
                'name': job.span_id or 'Assigned span',
                'layer_id': job.layer_id or '',
                'feature_count': len(job.covered_feature_ids or []),
            }
            if job.layer_id:
                layer_key = (job.project_id, job.layer_id)
                base['layer'] = {
                    'id': job.layer_id,
                    'name': layer_names.get(layer_key) or job.layer_id,
                }
            status_counts = counts['polygon'].get(job.id, {})
            covered = progress['polygon'].get(job.id, {})
        elif job.scope == AssignmentJob.SCOPE_POLYGON:
            base['polygon'] = {
                'id': job.polygon_id or '',
                'name': job.polygon_name or job.polygon_id or 'Assigned area',
                'geometry': job.polygon,
                'feature_count': len(job.covered_feature_ids or []),
            }
            status_counts = counts['polygon'].get(job.id, {})
            covered = progress['polygon'].get(job.id, {})
        elif job.scope == AssignmentJob.SCOPE_PROJECT:
            base['project']['status'] = job.project.status
            status_counts = counts['project'].get(job.project_id, {})
            covered = progress['project'].get(job.project_id, {})
        else:
            layer_key = (job.project_id, job.layer_id)
            status_counts = counts['layer'].get(layer_key, {})
            covered = progress['layer'].get(layer_key, {})
            base['layer'] = {
                'id': job.layer_id,
                'name': layer_names.get(layer_key) or job.layer_id,
            }

        aggregate_status = self._get_aggregate_status(status_counts)
        base['feature_count'] = sum(status_counts.values())
        base['field_progress'] = {
            'features': base['feature_count'],
            'measured_features': covered.get('measured_features', 0),
            'readings': None,
            'expected_readings': None,
            'photos': covered.get('photos', 0),
        }
        base['status'] = aggregate_status
        base['status_display'] = self._get_status_display(aggregate_status)
        return base

    @staticmethod
    def _stats(jobs, counts):
        """Status counts over exactly the features the matching jobs cover.

        Project jobs cover their whole project, layer jobs their layer (unless a
        project job already covers it), feature jobs their feature. Summing that
        union is what keeps `stats.total` equal to what the table is showing.

        The project/layer half is read from `_feature_counts`, which the rows
        need anyway — so a page of project jobs costs no extra query, where the
        previous version ran a second aggregate over the same features.
        """
        project_ids = {job.project_id for job in jobs if job.scope == AssignmentJob.SCOPE_PROJECT}
        layer_pairs = {
            (job.project_id, job.layer_id)
            for job in jobs
            if job.scope == AssignmentJob.SCOPE_LAYER and job.project_id not in project_ids
        }
        polygon_jobs = [
            job
            for job in jobs
            if job.scope in (AssignmentJob.SCOPE_POLYGON, AssignmentJob.SCOPE_SPAN)
            and job.covered_feature_ids
        ]
        feature_ids = {
            job.feature_id
            for job in jobs
            if job.scope == AssignmentJob.SCOPE_FEATURE and job.feature_id
        }

        stats = {
            'total': 0,
            Feature.STATUS_UNDER_REVIEW: 0,
            Feature.STATUS_APPROVED: 0,
            Feature.STATUS_REDO: 0,
            Feature.STATUS_PENDING: 0,
            Feature.STATUS_ASSIGNED: 0,
        }

        def absorb(status_counts):
            for feature_status, count in status_counts.items():
                stats['total'] += count
                if feature_status in stats:
                    stats[feature_status] += count

        for project_key in project_ids:
            absorb(counts['project'].get(project_key, {}))
        for layer_key in layer_pairs:
            absorb(counts['layer'].get(layer_key, {}))
        for job in polygon_jobs:
            absorb(counts['polygon'].get(job.id, {}))
        if feature_ids:
            rows = (
                Feature.objects.filter(id__in=feature_ids)
                .values('status')
                .annotate(count=Count('id'))
            )
            for row in rows:
                absorb({row['status']: row['count']})
        return stats

    def _get_aggregate_status(self, statuses):
        """Determine aggregate status with priority: redo > under_review > approved > pending > assigned"""
        if statuses.get(Feature.STATUS_REDO, 0) > 0:
            return Feature.STATUS_REDO
        if statuses.get(Feature.STATUS_UNDER_REVIEW, 0) > 0:
            return Feature.STATUS_UNDER_REVIEW
        if statuses.get(Feature.STATUS_APPROVED, 0) > 0:
            return Feature.STATUS_APPROVED
        if statuses.get(Feature.STATUS_PENDING, 0) > 0:
            return Feature.STATUS_PENDING
        return Feature.STATUS_ASSIGNED

    def _get_status_display(self, status):
        """Get human-readable status label"""
        display_map = {
            Feature.STATUS_PENDING: 'Pending',
            Feature.STATUS_ASSIGNED: 'Assigned',
            Feature.STATUS_UNDER_REVIEW: 'Under Review',
            Feature.STATUS_APPROVED: 'Approved',
            Feature.STATUS_REDO: 'Redo',
        }
        return display_map.get(status, status)


class EngineerActivityAPIView(APIView):
    """Returns timeline of engineer activity including assignments and status changes."""

    def get(self, request):
        engineer_id = request.query_params.get('engineer')
        days = int(request.query_params.get('days', 30))

        if not engineer_id:
            return Response(
                {'detail': 'engineer query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        since = timezone.now() - timedelta(days=days)

        # Get recent assignments for this engineer
        assignments = (
            AssignmentJob.objects.select_related('project', 'feature')
            .filter(assignee_id=engineer_id, created_at__gte=since)
            .order_by('-created_at')
        )

        # Get features assigned to this engineer with recent updates
        features = (
            engineer_feature_queryset(engineer_id)
            .select_related('project')
            .prefetch_related('assignment_jobs')
            .filter(updated_at__gte=since)
            .order_by('-updated_at')
        )

        # Build activity timeline
        activities = []

        # Assignment events
        for job in assignments[:50]:
            activities.append(
                {
                    'type': 'assignment',
                    'timestamp': job.created_at.isoformat(),
                    'project': {
                        'id': str(job.project.id),
                        'name': job.project.name,
                    },
                    'scope': job.scope,
                    'scope_display': job.get_scope_display(),
                    'target': self._get_assignment_target(job),
                }
            )

        # Feature update events (status changes)
        seen_features = set()
        for feature in features[:50]:
            if feature.id in seen_features:
                continue
            seen_features.add(feature.id)

            activities.append(
                {
                    'type': 'feature_update',
                    'timestamp': feature.updated_at.isoformat(),
                    'feature': {
                        'id': str(feature.id),
                        'layer_name': feature.layer_name,
                        'status': feature.status,
                        'status_display': self._get_status_display(feature.status),
                    },
                    'project': {
                        'id': str(feature.project.id),
                        'name': feature.project.name,
                    },
                }
            )

        # Sort by timestamp desc
        activities.sort(key=lambda x: x['timestamp'], reverse=True)

        return Response(
            {
                'engineer_id': engineer_id,
                'period_days': days,
                'activities': activities[:100],
                'total_count': len(activities),
            }
        )

    def _get_assignment_target(self, job):
        if job.scope == AssignmentJob.SCOPE_PROJECT:
            return {'type': 'project', 'name': job.project.name}
        if job.scope == AssignmentJob.SCOPE_LAYER:
            return {'type': 'layer', 'layer_id': job.layer_id}
        if job.scope == AssignmentJob.SCOPE_POLYGON:
            return {
                'type': 'polygon',
                'id': job.polygon_id,
                'name': job.polygon_name or job.polygon_id,
            }
        if job.scope == AssignmentJob.SCOPE_SPAN:
            return {'type': 'span', 'id': job.span_id, 'layer_id': job.layer_id}
        return {'type': 'feature', 'id': str(job.feature.id) if job.feature else None}

    def _get_status_display(self, status):
        display_map = {
            Feature.STATUS_PENDING: 'Pending',
            Feature.STATUS_ASSIGNED: 'Assigned',
            Feature.STATUS_UNDER_REVIEW: 'Under Review',
            Feature.STATUS_APPROVED: 'Approved',
            Feature.STATUS_REDO: 'Redo',
        }
        return display_map.get(status, status)


class EngineerStatsAPIView(APIView):
    """Returns performance statistics for an engineer."""

    def get(self, request):
        engineer_id = request.query_params.get('engineer')
        days = int(request.query_params.get('days', 30))

        if not engineer_id:
            return Response(
                {'detail': 'engineer query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        since = timezone.now() - timedelta(days=days)

        # Get features assigned to this engineer (project, layer and feature scope)
        features = engineer_feature_queryset(engineer_id)

        # Overall stats
        overall = features.aggregate(
            total=Count('id'),
            approved=Count(
                Case(When(status=Feature.STATUS_APPROVED, then=1), output_field=IntegerField())
            ),
            under_review=Count(
                Case(When(status=Feature.STATUS_UNDER_REVIEW, then=1), output_field=IntegerField())
            ),
            redo=Count(Case(When(status=Feature.STATUS_REDO, then=1), output_field=IntegerField())),
            assigned=Count(
                Case(When(status=Feature.STATUS_ASSIGNED, then=1), output_field=IntegerField())
            ),
            pending=Count(
                Case(When(status=Feature.STATUS_PENDING, then=1), output_field=IntegerField())
            ),
        )

        # Recent activity (last N days)
        recent_features = features.filter(updated_at__gte=since)
        recent = recent_features.aggregate(
            total=Count('id'),
            approved=Count(
                Case(When(status=Feature.STATUS_APPROVED, then=1), output_field=IntegerField())
            ),
        )

        # Daily breakdown for the period, in one grouped query. The previous
        # version ran `days` aggregates (30 by default) to fill this array,
        # which is most of the endpoint's latency against a remote database.
        daily_stats = []
        daily_rows = (
            recent_features.annotate(day=TruncDate('updated_at'))
            .values('day')
            .annotate(
                updated=Count('id'),
                approved=Count(
                    Case(When(status=Feature.STATUS_APPROVED, then=1), output_field=IntegerField())
                ),
            )
        )
        by_day = {row['day']: row for row in daily_rows}
        for day_offset in range(days):
            day = (since + timedelta(days=day_offset)).date()
            row = by_day.get(day)
            daily_stats.append(
                {
                    'date': day.isoformat(),
                    'updated': row['updated'] if row else 0,
                    'approved': row['approved'] if row else 0,
                }
            )

        # Project breakdown
        project_stats = (
            features.values('project__id', 'project__name')
            .annotate(
                total=Count('id'),
                approved=Count(
                    Case(When(status=Feature.STATUS_APPROVED, then=1), output_field=IntegerField())
                ),
                under_review=Count(
                    Case(
                        When(status=Feature.STATUS_UNDER_REVIEW, then=1),
                        output_field=IntegerField(),
                    )
                ),
                redo=Count(
                    Case(When(status=Feature.STATUS_REDO, then=1), output_field=IntegerField())
                ),
            )
            .order_by('-total')
        )

        return Response(
            {
                'engineer_id': engineer_id,
                'period_days': days,
                'overall': {
                    'total': overall['total'],
                    'approved': overall['approved'],
                    'under_review': overall['under_review'],
                    'redo': overall['redo'],
                    'assigned': overall['assigned'],
                    'pending': overall['pending'],
                    'approval_rate': (
                        round((overall['approved'] / overall['total'] * 100), 2)
                        if overall['total'] > 0
                        else 0
                    ),
                },
                'recent_period': {
                    'total': recent['total'],
                    'approved': recent['approved'],
                },
                'daily_breakdown': daily_stats,
                'project_breakdown': [
                    {
                        'project': {
                            'id': str(p['project__id']),
                            'name': p['project__name'],
                        },
                        'total': p['total'],
                        'approved': p['approved'],
                        'under_review': p['under_review'],
                        'redo': p['redo'],
                    }
                    for p in project_stats
                ],
            }
        )


class FeatureFieldMeasurementsAPIView(APIView):
    """Update field measurements for a feature.

    An engineer may only write readings for work they are assigned — the same
    project/layer/feature scopes ``engineer_feature_queryset`` expands.
    """

    def patch(self, request, pk):
        try:
            feature = Feature.objects.get(pk=pk)
        except Feature.DoesNotExist:
            return Response(
                {'detail': 'Feature not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if getattr(request.user, 'role', None) == User.Role.ENGINEER and not (
            engineer_feature_queryset(request.user.id).filter(pk=feature.pk).exists()
        ):
            return Response(
                {'detail': 'You do not have access to this feature.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        field_measurements = request.data.get('field_measurements')
        comparison_notes = request.data.get('comparison_notes')
        # Where the crew actually laid it. Submitted beside the readings so one
        # field visit writes one record; it lands in its own column and never
        # touches the design geometry.
        as_built_geometry = request.data.get('as_built_geometry')

        if field_measurements is not None:
            if not isinstance(field_measurements, dict):
                return Response(
                    {'detail': 'field_measurements must be a JSON object'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            feature.field_measurements = field_measurements

        if comparison_notes is not None:
            feature.comparison_notes = comparison_notes

        if as_built_geometry is not None:
            if not isinstance(as_built_geometry, dict) or not as_built_geometry.get('type'):
                return Response(
                    {'detail': 'as_built_geometry must be a GeoJSON geometry object'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            coordinates = as_built_geometry.get('coordinates')
            if not isinstance(coordinates, list | tuple):
                return Response(
                    {'detail': 'as_built_geometry must contain a coordinates array'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            feature.as_built_geometry = as_built_geometry

        feature.save()

        return Response(
            {
                'id': str(feature.id),
                'field_measurements': feature.field_measurements,
                'as_built_geometry': feature.as_built_geometry,
                'comparison_notes': feature.comparison_notes,
                'updated_at': feature.updated_at.isoformat(),
            }
        )


class FeatureSubmitAPIView(APIView):
    """Submit feature(s) for review.

    The engineer is the caller, not a body field: the previous version trusted
    ``engineer`` from the request, so it checked whether *that other person* had
    a job on these features instead of whether the caller may submit them. A
    SUBADMIN may still name an engineer, which is the legitimate
    "submit on their behalf" case.
    """

    def post(self, request):
        feature_ids = request.data.get('feature_ids', [])
        engineer = request.user

        if not feature_ids:
            return Response(
                {'detail': 'feature_ids is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        requested_engineer = request.data.get('engineer')
        if requested_engineer and getattr(request.user, 'role', None) == User.Role.SUBADMIN:
            engineer = User.objects.filter(id=requested_engineer).first() or request.user

        # ``engineer_feature_queryset`` expands all three scopes. The old join on
        # ``engineer_feature_queryset`` expands every scope (project, layer,
        # feature and polygon). Joining ``assignment_jobs__assignee_id``
        # instead missed project/layer jobs and matched twice when a feature
        # had two jobs for the same engineer, so a valid submission could
        # answer 403 with an empty "missing" set.
        features = engineer_feature_queryset(engineer.id).filter(id__in=feature_ids)

        found_ids = {str(feature.id) for feature in features}
        missing = {str(feature_id) for feature_id in feature_ids} - found_ids
        if missing:
            return Response(
                {'detail': f'Features not found or not assigned: {sorted(missing)}'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Only allow submission from certain statuses. Supervision features are
        # created ``pending`` by the LLD handoff, so they become submittable once
        # the engineer has recorded at least one reading; without a reading there
        # is nothing for the planner to review.
        valid_statuses = [Feature.STATUS_PENDING, Feature.STATUS_ASSIGNED, Feature.STATUS_REDO]
        invalid_features = [
            {'id': str(f.id), 'status': f.status}
            for f in features
            if f.status not in valid_statuses
            or (f.status == Feature.STATUS_PENDING and not f.field_measurements)
        ]

        if invalid_features:
            return Response(
                {
                    'detail': 'Some features cannot be submitted',
                    'invalid_features': invalid_features,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Capture the identity before the update: the rows are about to leave the
        # statuses this queryset is read through, so re-reading it afterwards is
        # how the approve/reject response used to come back empty.
        project_ids = list(features.values_list('project_id', flat=True).distinct())
        submitted_ids = [str(fid) for fid in features.values_list('id', flat=True)]

        # Update status to under_review and set submitted_at
        now = timezone.now()
        updated_count = features.filter(status__in=valid_statuses).update(
            status=Feature.STATUS_UNDER_REVIEW, submitted_at=now
        )

        record_stage_events(
            project_ids,
            'feature_submitted',
            actor=request.user,
            metadata={'feature_ids': submitted_ids, 'count': updated_count},
        )

        return Response(
            {
                'submitted_count': updated_count,
                'feature_ids': [str(f.id) for f in features],
                'new_status': Feature.STATUS_UNDER_REVIEW,
                'status_display': 'Under Review',
            }
        )


class FeatureApproveAPIView(APIView):
    """Approve feature(s) - admin only.

    The docstring promised a rule nothing enforced: the class had no
    ``permission_classes``, so any engineer — including the one who submitted
    the work — could approve it.
    """

    permission_classes = [IsSubadmin]

    def post(self, request):
        feature_ids = request.data.get('feature_ids', [])
        reviewer_id = request.data.get('reviewer')
        notes = request.data.get('notes', '')

        if not feature_ids:
            return Response(
                {'detail': 'feature_ids is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get features that are under_review
        features = Feature.objects.filter(id__in=feature_ids, status=Feature.STATUS_UNDER_REVIEW)

        if features.count() == 0:
            return Response(
                {'detail': "No features found in 'under_review' status"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Update status to approved, set approved_at, and keep the note. The
        # note used to be echoed back and dropped, so "approve with deviation"
        # left no record of why.
        # Capture ids before the update: the queryset is filtered on the old
        # status, so re-reading it after the update returns nothing.
        # Materialise the rows before the update: the deviation comparison needs
        # each feature's own readings, and this queryset is filtered on the
        # status the rows are about to leave.
        reviewed_features = list(features.select_related('project'))
        approved_ids = [str(feature.id) for feature in reviewed_features]
        project_ids = sorted({feature.project_id for feature in reviewed_features}, key=str)
        now = timezone.now()
        updated_count = features.update(
            status=Feature.STATUS_APPROVED,
            approved_at=now,
            approval_notes=notes or '',
        )

        # Every approval is a review decision, not just a status change: write
        # the ledger row (INTEGRATION_PLAN.md §3 Stage 6). Never fatal — the
        # approval itself has already been committed.
        decisions = {'records': [], 'deviations': []}
        try:
            from supervision.decisions import record_approval_decisions

            decisions = record_approval_decisions(
                reviewed_features, actor=request.user, notes=notes
            )
        except Exception:
            logger.exception('Could not record supervision decisions on approval')

        deviations = decisions['deviations']

        record_stage_events(
            project_ids,
            'feature_approved',
            actor=request.user,
            metadata={
                'feature_ids': approved_ids,
                'count': updated_count,
                'notes': notes or '',
            },
        )

        return Response(
            {
                'approved_count': updated_count,
                'feature_ids': approved_ids,
                'reviewer_id': reviewer_id,
                'notes': notes,
                'new_status': Feature.STATUS_APPROVED,
                'status_display': 'Approved',
                'approved_at': now.isoformat(),
                'decision_records_written': len(decisions['records']),
                'deviation_count': len(deviations),
                'deviations_recorded': deviations,
            }
        )


class FeatureRejectAPIView(APIView):
    """Reject feature(s) and send back to redo - admin only."""

    permission_classes = [IsSubadmin]

    def post(self, request):
        feature_ids = request.data.get('feature_ids', [])
        reviewer_id = request.data.get('reviewer')
        rejection_reason = request.data.get('rejection_reason', '')

        if not feature_ids:
            return Response(
                {'detail': 'feature_ids is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get features that are under_review
        features = Feature.objects.filter(id__in=feature_ids, status=Feature.STATUS_UNDER_REVIEW)

        if features.count() == 0:
            return Response(
                {'detail': "No features found in 'under_review' status"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Materialise before the update (see FeatureApproveAPIView): the redo
        # ledger needs each feature's measurements as the reviewer saw them.
        reviewed_features = list(features.select_related('project'))
        rejected_ids = [str(feature.id) for feature in reviewed_features]
        project_ids = sorted({feature.project_id for feature in reviewed_features}, key=str)
        # Update status to redo and clear submitted_at (for re-submission)
        updated_count = features.update(
            status=Feature.STATUS_REDO, submitted_at=None, comparison_notes=rejection_reason
        )

        redo_records = []
        try:
            from supervision.decisions import record_redo_decisions

            redo_records = record_redo_decisions(
                reviewed_features, actor=request.user, reason=rejection_reason
            )
        except Exception:
            logger.exception('Could not record supervision redo decisions')

        record_stage_events(
            project_ids,
            'feature_redo',
            actor=request.user,
            metadata={
                'feature_ids': rejected_ids,
                'count': updated_count,
                'reason': rejection_reason,
            },
        )

        return Response(
            {
                'rejected_count': updated_count,
                'feature_ids': rejected_ids,
                'reviewer_id': reviewer_id,
                'rejection_reason': rejection_reason,
                'decision_records_written': len(redo_records),
                'new_status': Feature.STATUS_REDO,
                'status_display': 'Redo',
            }
        )
