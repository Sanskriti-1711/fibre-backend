from django.urls import path

from assignments.api.views import (
    AssignmentJobDetailAPIView,
    AssignmentJobListCreateAPIView,
    AssignmentJobSummaryAPIView,
)

urlpatterns = [
    path("assignments/", AssignmentJobListCreateAPIView.as_view()),
    path("assignments/summary/", AssignmentJobSummaryAPIView.as_view()),
    path("assignments/<uuid:pk>/", AssignmentJobDetailAPIView.as_view()),
]
