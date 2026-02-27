from django.urls import path

from assignments.api.views import (
    AssignmentJobDetailAPIView,
    AssignmentJobListCreateAPIView,
    AssignmentJobSummaryAPIView,
    EngineerActivityAPIView,
    EngineerStatsAPIView,
    FeatureFieldMeasurementsAPIView,
    FeatureSubmitAPIView,
    JobAssignmentsListAPIView,
)

urlpatterns = [
    path("assignments/", AssignmentJobListCreateAPIView.as_view()),
    path("assignments/summary/", AssignmentJobSummaryAPIView.as_view()),
    path("assignments/<uuid:pk>/", AssignmentJobDetailAPIView.as_view()),
    path("assignments/jobs/", JobAssignmentsListAPIView.as_view()),
    path("engineer/activity/", EngineerActivityAPIView.as_view()),
    path("engineer/stats/", EngineerStatsAPIView.as_view()),
    path("features/<uuid:pk>/field-measurements/", FeatureFieldMeasurementsAPIView.as_view()),
    path("features/submit/", FeatureSubmitAPIView.as_view()),
]
