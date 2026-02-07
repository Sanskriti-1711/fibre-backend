from django.urls import path
from .projects import (
    ProjectListCreateAPIView,
    ProjectDetailAPIView,
    LatestProjectUpdatesAPIView,
)

urlpatterns = [
    path("projects/", ProjectListCreateAPIView.as_view()),
    path("projects/latest/", LatestProjectUpdatesAPIView.as_view()),
    path("projects/<uuid:project_id>/", ProjectDetailAPIView.as_view()),
    path("projects/<uuid:project_id>/import/", ImportGeoPackageAPIView.as_view(),),
]
