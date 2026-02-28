from django.urls import path
from .projects import (
    ProjectListCreateAPIView,
    ProjectDetailAPIView,
    LatestProjectUpdatesAPIView,
)
from .import_views import (
    GpkgUploadView,
    GpkgDiscoverView,
    GpkgImportView,
    ImportStatusView,
)
from .layers import (
    ProjectLayerListAPIView,
    ProjectLayerDetailAPIView,
    ProjectFeatureDetailAPIView,
)
from .completion import ProjectCompletionAPIView
from .layer_weights import ProjectLayerWeightsAPIView

urlpatterns = [
    path("projects/", ProjectListCreateAPIView.as_view()),
    path("projects/latest/", LatestProjectUpdatesAPIView.as_view()),
    path("projects/<uuid:project_id>/", ProjectDetailAPIView.as_view()),
    path("projects/<uuid:project_id>/layers/", ProjectLayerListAPIView.as_view()),
    path(
        "projects/<uuid:project_id>/layers/<str:layer_id>/",
        ProjectLayerDetailAPIView.as_view(),
    ),
    path(
        "projects/<uuid:project_id>/features/<uuid:feature_id>/",
        ProjectFeatureDetailAPIView.as_view(),
    ),
    # Completion endpoints
    path(
        "projects/<uuid:project_id>/completion/",
        ProjectCompletionAPIView.as_view(),
    ),
    path(
        "projects/<uuid:project_id>/layers/weights/",
        ProjectLayerWeightsAPIView.as_view(),
    ),
    # Import endpoints
    path("projects/<uuid:project_id>/import/upload/", GpkgUploadView.as_view()),
    path("projects/<uuid:project_id>/import/discover/", GpkgDiscoverView.as_view()),
    path("projects/<uuid:project_id>/import/import/", GpkgImportView.as_view()),
    path("projects/<uuid:project_id>/import/status/", ImportStatusView.as_view()),
]
