from django.urls import path
from .survey_views import (
    SurveyChangeListCreateView,
    SurveyChangeDetailView,
    SurveyChangeReviewView,
    SurveyChangeBulkReviewView,
    SurveyChangeStatsView,
    SurveyChangeReadinessView,
    ApprovedSurveyVersionListCreateView,
    ApprovedSurveyVersionDetailView,
    ApprovedSurveyVersionLatestView,
)

urlpatterns = [
    # Survey Changes
    path(
        "projects/<uuid:project_id>/survey-changes/",
        SurveyChangeListCreateView.as_view(),
        name="survey-change-list-create"
    ),
    path(
        "projects/<uuid:project_id>/survey-changes/<uuid:pk>/",
        SurveyChangeDetailView.as_view(),
        name="survey-change-detail"
    ),
    path(
        "projects/<uuid:project_id>/survey-changes/<uuid:change_id>/review/",
        SurveyChangeReviewView.as_view(),
        name="survey-change-review"
    ),
    path(
        "projects/<uuid:project_id>/survey-changes/bulk-review/",
        SurveyChangeBulkReviewView.as_view(),
        name="survey-change-bulk-review"
    ),
    path(
        "projects/<uuid:project_id>/survey-changes/stats/",
        SurveyChangeStatsView.as_view(),
        name="survey-change-stats"
    ),
    path(
        "projects/<uuid:project_id>/survey-changes/readiness/",
        SurveyChangeReadinessView.as_view(),
        name="survey-change-readiness"
    ),
    
    # Approved Survey Versions
    path(
        "projects/<uuid:project_id>/approved-survey/",
        ApprovedSurveyVersionListCreateView.as_view(),
        name="approved-survey-list-create"
    ),
    path(
        "projects/<uuid:project_id>/approved-survey/<uuid:pk>/",
        ApprovedSurveyVersionDetailView.as_view(),
        name="approved-survey-detail"
    ),
    path(
        "projects/<uuid:project_id>/approved-survey/latest/",
        ApprovedSurveyVersionLatestView.as_view(),
        name="approved-survey-latest"
    ),
]
