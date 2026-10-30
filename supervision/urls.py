from django.urls import path

from supervision.dashboard import DashboardSummaryAPIView
from supervision.export import ProjectExportAPIView
from supervision.views import (
    FeaturePhotoDetailView,
    FeaturePhotosView,
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
    path('features/<uuid:feature_id>/photos/', FeaturePhotosView.as_view()),
    path(
        'features/<uuid:feature_id>/photos/<uuid:photo_id>/',
        FeaturePhotoDetailView.as_view(),
    ),
]
