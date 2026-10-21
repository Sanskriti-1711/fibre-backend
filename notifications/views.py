"""REST endpoints behind the notification bell.

Four routes, all JWT-authenticated and **scoped to ``request.user``** — there is
no "read somebody else's inbox" endpoint and no role that can mark another
person's rows read:

* ``GET  /api/notifications/``           — the inbox page (filters + unread count)
* ``GET  /api/notifications/summary/``   — badge poll (counts only, tiny)
* ``POST /api/notifications/<id>/read/`` — mark one read
* ``POST /api/notifications/read-all/``  — mark every unread row read

Marking an already-read row read is a no-op that still answers 200: a poller
can retry without inventing an error, and the bell never gets stuck.
"""

from __future__ import annotations

from django.http import JsonResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from . import service
from .models import Notification


def _serialize(row: Notification) -> dict:
    return {
        'id': str(row.pk),
        'kind': row.kind,
        'kind_label': dict(Notification.KIND_CHOICES).get(row.kind, row.kind),
        'category': row.category,
        'severity': row.severity,
        'title': row.title,
        'body': row.body,
        'project_id': row.project_id or None,
        'project_name': row.project_name or None,
        'entity_type': row.entity_type or None,
        'entity_id': row.entity_id or None,
        'url': row.url or None,
        'meta': row.meta or {},
        'read': row.read_at is not None,
        'read_at': row.read_at.isoformat() if row.read_at else None,
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'actor': (
            {
                'id': str(row.actor_id),
                'name': row.actor.full_name or row.actor.email,
            }
            if row.actor_id and row.actor is not None
            else None
        ),
    }


class NotificationListView(APIView):
    """``GET /api/notifications/`` — newest first, paginated by ``limit``/``offset``."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            limit = int(request.GET.get('limit') or 25)
        except ValueError:
            return JsonResponse({'detail': 'limit must be an integer.'}, status=400)
        try:
            offset = int(request.GET.get('offset') or 0)
        except ValueError:
            return JsonResponse({'detail': 'offset must be an integer.'}, status=400)
        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        rows = Notification.objects.filter(recipient=request.user).select_related('actor')
        unread_only = (request.GET.get('unread') or '').lower() in ('1', 'true', 'yes')
        if unread_only:
            rows = rows.filter(read_at__isnull=True)
        category = (request.GET.get('category') or '').strip()
        if category:
            if category not in dict(Notification.CATEGORY_CHOICES):
                return JsonResponse({'detail': f'Unknown category: {category}'}, status=400)
            rows = rows.filter(category=category)
        project_id = (request.GET.get('project') or '').strip()
        if project_id:
            rows = rows.filter(project_id=project_id)

        total = rows.count()
        page = list(rows[offset : offset + limit])
        return JsonResponse(
            {
                'total': total,
                'limit': limit,
                'offset': offset,
                'unread': service.unread_summary(request.user)['unread'],
                'notifications': [_serialize(row) for row in page],
                'kinds': [choice[0] for choice in Notification.KIND_CHOICES],
                'categories': [choice[0] for choice in Notification.CATEGORY_CHOICES],
            }
        )


class NotificationSummaryView(APIView):
    """``GET /api/notifications/summary/`` — the badge poll."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return JsonResponse(service.unread_summary(request.user))


class NotificationReadView(APIView):
    """``POST /api/notifications/<id>/read/`` — mark one row read."""

    permission_classes = [IsAuthenticated]

    def post(self, request, notification_id):
        row = Notification.objects.filter(pk=notification_id, recipient=request.user).first()
        if row is None:
            return JsonResponse({'detail': 'Notification not found.'}, status=404)
        changed = row.mark_read()
        return JsonResponse(
            {
                'id': str(row.pk),
                'read': True,
                'changed': changed,
                'unread': service.unread_summary(request.user)['unread'],
            }
        )


class NotificationReadAllView(APIView):
    """``POST /api/notifications/read-all/`` — mark every unread row read.

    An optional ``ids`` list narrows it to one page of the inbox.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        rows = Notification.objects.filter(recipient=request.user, read_at__isnull=True)
        ids = (request.data or {}).get('ids')
        if isinstance(ids, list) and ids:
            rows = rows.filter(pk__in=[str(value) for value in ids])
        from django.utils import timezone

        updated = rows.update(read_at=timezone.now())
        return JsonResponse(
            {
                'marked': updated,
                'unread': service.unread_summary(request.user)['unread'],
            }
        )
