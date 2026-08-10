from django.urls import path
from .projects import (
    ProjectAcceptAPIView,
    ProjectSubmitAPIView,
    ProjectReviewAPIView,
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
    ProjectMapDataAPIView,
    ProjectLayerListAPIView,
    ProjectLayerDetailAPIView,
    ProjectFeatureDetailAPIView,
    ProjectFeatureUpdateAPIView,
    FeaturePhotoUploadView,
)
from .completion import ProjectCompletionAPIView
from .layer_weights import ProjectLayerWeightsAPIView
from .layer_field_config import LayerFieldConfigAPIView

urlpatterns = [
    path("projects/", ProjectListCreateAPIView.as_view()),
    path("projects/latest/", LatestProjectUpdatesAPIView.as_view()),
    path("projects/<uuid:project_id>/", ProjectDetailAPIView.as_view()),
    path("projects/<uuid:project_id>/accept/", ProjectAcceptAPIView.as_view()),
    path("projects/<uuid:project_id>/submit/", ProjectSubmitAPIView.as_view()),
    path("projects/<uuid:project_id>/review/", ProjectReviewAPIView.as_view()),
    path("projects/<uuid:project_id>/map-data/", ProjectMapDataAPIView.as_view()),
    path("projects/<uuid:project_id>/layers/", ProjectLayerListAPIView.as_view()),
    path(
        "projects/<uuid:project_id>/layers/<str:layer_id>/field-config/",
        LayerFieldConfigAPIView.as_view(),
    ),
    path(
        "projects/<uuid:project_id>/layers/<str:layer_id>/",
        ProjectLayerDetailAPIView.as_view(),
    ),
    path(
        "projects/<uuid:project_id>/features/<uuid:feature_id>/",
        ProjectFeatureDetailAPIView.as_view(),
    ),
    path(
        "projects/<uuid:project_id>/features/<uuid:feature_id>/update/",
        ProjectFeatureUpdateAPIView.as_view(),
    ),
    # Feature photo upload endpoint
    path(
        "features/<uuid:feature_id>/upload-photo/",
        FeaturePhotoUploadView.as_view(),
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
