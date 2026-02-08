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

urlpatterns = [
    path("projects/", ProjectListCreateAPIView.as_view()),
    path("projects/latest/", LatestProjectUpdatesAPIView.as_view()),
    path("projects/<uuid:project_id>/", ProjectDetailAPIView.as_view()),
    # Import endpoints
    path("projects/<uuid:project_id>/import/upload/", GpkgUploadView.as_view()),
    path("projects/<uuid:project_id>/import/discover/", GpkgDiscoverView.as_view()),
    path("projects/<uuid:project_id>/import/import/", GpkgImportView.as_view()),
    path("projects/<uuid:project_id>/import/status/", ImportStatusView.as_view()),
]
