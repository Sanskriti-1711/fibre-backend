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
            entity_id=str(job.feature_id or job.layer_id or job.project_id),
            metadata={'scope': job.scope, 'assignee': str(job.assignee_id)},
        )


class AssignmentJobDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    queryset = AssignmentJob.objects.select_related('project', 'feature', 'assignee')

    def get_serializer_class(self):
        if self.request.method == 'GET':
            return AssignmentJobDetailSerializer
        return AssignmentJobSerializer


class AssignmentJobSummaryAPIView(APIView):
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

        serializer = AssignmentJobDetailSerializer(assignments, many=True)
        grouped: dict[str, list] = {'project': [], 'layer': [], 'feature': []}
        for record in serializer.data:
            grouped[record['scope']].append(record)

        return Response(
            {
                'project_id': project_id,
                'counts': {k: len(v) for k, v in grouped.items()},
                'assignments': grouped,
            }
        )


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

        # `stats` keeps describing every job the query matches, so the counters
        # do not move when a status tab is selected.
        stats = self._stats(jobs, counts)

        rows = [self._job_row(job, counts, layer_names) for job in jobs]
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
        """Status counts per project and per (project, layer), in one query.

        A project job's `feature_count`/status is the whole project; a layer
        job's is its layer. Features whose layer_id differs between rows are
        summed per (project, layer) pair, so a layer job cannot pick up another
        layer's features.
        """
        project_ids = {job.project_id for job in jobs if job.scope == AssignmentJob.SCOPE_PROJECT}
        layer_pairs = {
            (job.project_id, job.layer_id) for job in jobs if job.scope == AssignmentJob.SCOPE_LAYER
        }
        by_project: dict = {}
        by_layer: dict = {}
        layer_names: dict = {}

        if not project_ids and not layer_pairs:
            return {'project': by_project, 'layer': by_layer}, layer_names

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

        return {'project': by_project, 'layer': by_layer}, layer_names

    def _job_row(self, job, counts, layer_names):
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
            base['status'] = feature.status
            base['status_display'] = self._get_status_display(feature.status)
            return base

        if job.scope == AssignmentJob.SCOPE_PROJECT:
            base['project']['status'] = job.project.status
            status_counts = counts['project'].get(job.project_id, {})
        else:
            layer_key = (job.project_id, job.layer_id)
            status_counts = counts['layer'].get(layer_key, {})
            base['layer'] = {
                'id': job.layer_id,
                'name': layer_names.get(layer_key) or job.layer_id,
            }

        aggregate_status = self._get_aggregate_status(status_counts)
        base['feature_count'] = sum(status_counts.values())
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
    """Update field measurements for a feature."""

    def patch(self, request, pk):
        try:
            feature = Feature.objects.get(pk=pk)
        except Feature.DoesNotExist:
            return Response(
                {'detail': 'Feature not found'},
                status=status.HTTP_404_NOT_FOUND,
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
    """Submit feature(s) for review."""

    def post(self, request):
        feature_ids = request.data.get('feature_ids', [])
        engineer_id = request.data.get('engineer')

        if not feature_ids:
            return Response(
                {'detail': 'feature_ids is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not engineer_id:
            return Response(
                {'detail': 'engineer is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ``engineer_feature_queryset`` expands all three scopes. The old join on
        # ``assignment_jobs__assignee_id`` only matched feature-scope jobs, so a
        # supervision job covering the whole project or one layer always answered
        # 403 — the same gap the layers endpoint had.
        features = engineer_feature_queryset(engineer_id).filter(id__in=feature_ids)

        if features.count() != len(feature_ids):
            found_ids = {str(f.id) for f in features}
            missing = set(feature_ids) - found_ids
            return Response(
                {'detail': f'Features not found or not assigned: {missing}'},
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
    """Approve feature(s) - admin only."""

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
