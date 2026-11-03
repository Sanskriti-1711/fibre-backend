from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from assignments.models import AssignmentJob
from projects.models import Feature, Project
from projects.models.import_session import ImportSession
from users.models import User

LIST_LIMIT = 8


def _start_of_today(tz_name):
    """Midnight today in the caller's timezone, so "today" matches the admin's
    calendar day rather than the server's UTC one."""
    try:
        tz = ZoneInfo(tz_name) if tz_name else timezone.get_current_timezone()
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.get_current_timezone()
    today = timezone.now().astimezone(tz).date()
    return datetime.combine(today, time.min, tzinfo=tz)


def _plan_id(feature):
    # Business ID carried over from the imported GIS file. `custom_id` is what
    # the importer writes today; `id`/`ID` are older imports.
    props = feature.properties or {}
    return props.get('custom_id') or props.get('id') or props.get('ID') or ''


class DashboardSummaryAPIView(APIView):
    """
    GET /api/dashboard/summary/?tz=Asia/Kolkata

    Everything the admin dashboard shows, in one response.
    """

    def get(self, request):
        today_start = _start_of_today(request.query_params.get('tz'))

        project_counts = Project.objects.aggregate(
            total=Count('id'),
            active=Count('id', filter=Q(status__in=['active', 'in_progress'])),
            completed=Count('id', filter=Q(status='completed')),
        )

        feature_counts = Feature.objects.aggregate(
            total=Count('id'),
            **{key: Count('id', filter=Q(status=key)) for key, _ in Feature.STATUS_CHOICES},
        )
        total_features = feature_counts['total']
        approved = feature_counts[Feature.STATUS_APPROVED]
        overall_completion = round(approved / total_features * 100, 2) if total_features else 0.0

        # An engineer counts as active once they submit a feature today. A
        # rejection clears submitted_at, so a same-day reject drops them out.
        active_today = (
            AssignmentJob.objects.filter(
                scope=AssignmentJob.SCOPE_FEATURE,
                feature__submitted_at__gte=today_start,
            )
            .values('assignee_id')
            .distinct()
            .count()
        )
        total_engineers = User.objects.filter(role=User.Role.ENGINEER, is_active=True).count()

        uploads = ImportSession.objects.select_related('project').order_by('-created_at')[
            :LIST_LIMIT
        ]

        return Response(
            {
                'generated_at': timezone.now().isoformat(),
                'today_start': today_start.isoformat(),
                'projects': project_counts,
                'features': feature_counts,
                'overall_completion': overall_completion,
                'engineers': {'active_today': active_today, 'total': total_engineers},
                'latest_uploads': [
                    {
                        'id': str(s.id),
                        'project_id': str(s.project_id),
                        'project_name': s.project.name,
                        'file_name': s.original_filename,
                        'status': s.status,
                        'created_at': s.created_at.isoformat(),
                    }
                    for s in uploads
                ],
                'recent_reviews': self._recent_reviews(),
            }
        )

    def _recent_reviews(self):
        approved = list(
            Feature.objects.select_related('project')
            .filter(status=Feature.STATUS_APPROVED, approved_at__isnull=False)
            .order_by('-approved_at')[:LIST_LIMIT]
        )
        # Rejections are not timestamped; updated_at is the closest record of
        # when the feature was sent back for redo.
        redo = list(
            Feature.objects.select_related('project')
            .filter(status=Feature.STATUS_REDO)
            .order_by('-updated_at')[:LIST_LIMIT]
        )

        rows = [(f, 'approved', f.approved_at) for f in approved]
        rows += [(f, 'redo', f.updated_at) for f in redo]
        rows.sort(key=lambda r: r[2], reverse=True)
        rows = rows[:LIST_LIMIT]

        engineers = {
            job.feature_id: job.assignee
            for job in AssignmentJob.objects.select_related('assignee').filter(
                scope=AssignmentJob.SCOPE_FEATURE,
                feature_id__in=[f.id for f, _, _ in rows],
            )
        }

        result = []
        for feature, outcome, at in rows:
            engineer = engineers.get(feature.id)
            result.append(
                {
                    'feature_id': str(feature.id),
                    'custom_id': _plan_id(feature),
                    'layer_name': feature.layer_name,
                    'project_id': str(feature.project_id),
                    'project_name': feature.project.name,
                    'result': outcome,
                    'notes': feature.approval_notes
                    if outcome == 'approved'
                    else feature.comparison_notes,
                    'engineer': (engineer.full_name or engineer.email) if engineer else None,
                    'reviewed_at': at.isoformat(),
                }
            )
        return result
