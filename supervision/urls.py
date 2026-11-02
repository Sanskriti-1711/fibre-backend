from django.urls import path

from supervision.dashboard import DashboardSummaryAPIView
from supervision.export import ProjectExportAPIView
from supervision.report import ProjectAsBuiltReportAPIView
from supervision.views import (
    FeatureDecisionListView,
    FeaturePhotoDetailView,
    FeaturePhotosView,
    ProjectDecisionListView,
    ProjectFeatureStatusesAPIView,
)

# Included under `api/` in config/urls.py, so these keep the URLs the delivered
# frontend already calls. The legacy single-photo route
# (`features/<id>/upload-photo/`) stays in projects/api/urls.py because it writes
# the deprecated Feature.photo column, which older clients still read.
urlpatterns = [
    path('dashboard/summary/', DashboardSummaryAPIView.as_view()),
    path(
        'projects/<uuid:project_id>/feature-statuses/',
        ProjectFeatureStatusesAPIView.as_view(),
    ),
    path('projects/<uuid:project_id>/export/', ProjectExportAPIView.as_view()),
    # Stage 8: per-layer planned-vs-as-built totals and the BOQ reconciliation
    # the handover review reads. Read-only.
    path(
        'projects/<uuid:project_id>/as-built-report/',
        ProjectAsBuiltReportAPIView.as_view(),
    ),
    # Stage 6: the append-only record of approval-with-deviation. Read only —
    # records are written by the approval path, never by a client.
    path('features/<uuid:feature_id>/decisions/', FeatureDecisionListView.as_view()),
    path('projects/<uuid:project_id>/decisions/', ProjectDecisionListView.as_view()),
    path('features/<uuid:feature_id>/photos/', FeaturePhotosView.as_view()),
    path(
        'features/<uuid:feature_id>/photos/<uuid:photo_id>/',
        FeaturePhotoDetailView.as_view(),
    ),
]
