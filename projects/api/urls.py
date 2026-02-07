from django.urls import path
from .projects import (
    ProjectListCreateAPIView,
    ProjectDetailAPIView,
    LatestProjectUpdatesAPIView,
)
from ..services.imports import ImportGeoPackageAPIView

urlpatterns = [
    path("projects/", ProjectListCreateAPIView.as_view()),
    path("projects/latest/", LatestProjectUpdatesAPIView.as_view()),
    path("projects/<uuid:project_id>/", ProjectDetailAPIView.as_view()),
]
