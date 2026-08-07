"""URL configuration for the survey app."""

from django.urls import path
from .views import (
    GPSTraceListCreateAPIView,
    GPSTraceDetailAPIView,
    GPSPointBatchAPIView,
    TrenchSurveyListCreateAPIView,
    TrenchSurveyDetailAPIView,
    ExistingAssetListCreateAPIView,
    RiskAssessmentListCreateAPIView,
    RiskAssessmentDetailAPIView,
    HazardListCreateAPIView,
    FieldEvidenceListCreateAPIView,
    SurveyChangeListAPIView,
    SurveyStatusAPIView,
    SyncQueueListCreateAPIView,
    SyncQueueProcessAPIView,
    SurveyFeatureListCreateAPIView,
    SurveyFeatureDetailAPIView,
    SurveyFeatureUpsertAPIView,
    SurveyFeaturePhotoUploadView,
)

urlpatterns = [
    # GPS Traces
    path('gps-traces/', GPSTraceListCreateAPIView.as_view()),
    path('gps-traces/<uuid:trace_id>/', GPSTraceDetailAPIView.as_view()),
    path('gps-traces/<uuid:trace_id>/points/', GPSPointBatchAPIView.as_view()),

    # Trench Surveys
    path('trenches/', TrenchSurveyListCreateAPIView.as_view()),
    path('trenches/<uuid:trench_id>/', TrenchSurveyDetailAPIView.as_view()),

    # Existing Assets
    path('assets/', ExistingAssetListCreateAPIView.as_view()),

    # Risk Assessments
    path('risks/', RiskAssessmentListCreateAPIView.as_view()),
    path('risks/<uuid:risk_id>/', RiskAssessmentDetailAPIView.as_view()),

    # Hazards
    path('hazards/', HazardListCreateAPIView.as_view()),

    # Field Evidence
    path('evidence/', FieldEvidenceListCreateAPIView.as_view()),

    # Survey Changes & Status
    path('changes/', SurveyChangeListAPIView.as_view()),
    path('status/', SurveyStatusAPIView.as_view()),

    # Sync Queue
    path('sync/', SyncQueueListCreateAPIView.as_view()),
    path('sync/process/', SyncQueueProcessAPIView.as_view()),

    # Survey Features (HLD/Survey Separation)
    path('survey-features/', SurveyFeatureListCreateAPIView.as_view()),
    path('survey-features/upsert/', SurveyFeatureUpsertAPIView.as_view()),
    path('survey-features/<uuid:feature_id>/upload-photo/', SurveyFeaturePhotoUploadView.as_view()),
    path('survey-features/<uuid:feature_id>/', SurveyFeatureDetailAPIView.as_view()),
]
