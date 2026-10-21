"""URL routing for in-app notifications (``api/notifications/``)."""

from django.urls import path

from .views import (
    NotificationListView,
    NotificationReadAllView,
    NotificationReadView,
    NotificationSummaryView,
)

urlpatterns = [
    path('notifications/', NotificationListView.as_view(), name='notifications-list'),
    path('notifications/summary/', NotificationSummaryView.as_view(), name='notifications-summary'),
    path(
        'notifications/read-all/',
        NotificationReadAllView.as_view(),
        name='notifications-read-all',
    ),
    path(
        'notifications/<uuid:notification_id>/read/',
        NotificationReadView.as_view(),
        name='notifications-read',
    ),
]
