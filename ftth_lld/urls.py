"""
URL routing for the FTTH LLD module.

All endpoints are prefixed with ``api/ftth/lld/`` and require JWT auth.
The LLD review/run workflow is isolated in its own app so it stays separate
from the HLD pipeline (``ftth_hld``) and the Survey app.
"""

from django.urls import path

from .views import (
    FeatureLineageView,
    LldApprovedVersionView,
    LldChangeActionView,
    LldDownloadView,
    LldLayerView,
    LldProjectsView,
    LldReviewView,
    LldRunsView,
    LldRunStatusView,
    LldRunView,
    LldVersionsDiffView,
    LldVersionsView,
    ProjectMemberRemoveView,
    ProjectMembersView,
    ProjectOverviewView,
)

urlpatterns = [
    path("ftth/lld/projects/",
         LldProjectsView.as_view(), name="ftth-lld-projects"),
    path("ftth/lld/runs/",
         LldRunsView.as_view(), name="ftth-lld-runs-all"),
    path("ftth/lld/projects/<str:project_id>/features/<str:feature_id>/lineage/",
         FeatureLineageView.as_view(), name="ftth-lld-feature-lineage"),
    path("ftth/lld/projects/<str:project_id>/overview/",
         ProjectOverviewView.as_view(), name="ftth-lld-project-overview"),
    path("ftth/lld/projects/<str:project_id>/members/",
         ProjectMembersView.as_view(), name="ftth-lld-project-members"),
    path("ftth/lld/projects/<str:project_id>/members/<str:member_id>/",
         ProjectMemberRemoveView.as_view(), name="ftth-lld-project-member-remove"),
    path("ftth/lld/projects/<str:project_id>/review/",
         LldReviewView.as_view(), name="ftth-lld-review"),
    path("ftth/lld/projects/<str:project_id>/changes/<str:change_id>/action/",
         LldChangeActionView.as_view(), name="ftth-lld-action"),
    path("ftth/lld/projects/<str:project_id>/approved-version/",
         LldApprovedVersionView.as_view(), name="ftth-lld-approved-version"),
    path("ftth/lld/projects/<str:project_id>/runs/",
         LldRunView.as_view(), name="ftth-lld-runs"),
    path("ftth/lld/projects/<str:project_id>/runs/<str:lld_version>/",
         LldRunStatusView.as_view(), name="ftth-lld-run-status"),
    path("ftth/lld/projects/<str:project_id>/runs/<str:lld_version>/layers/<str:layer>/",
         LldLayerView.as_view(), name="ftth-lld-run-layer"),
    path("ftth/lld/projects/<str:project_id>/runs/<str:lld_version>/download/",
         LldDownloadView.as_view(), name="ftth-lld-run-download"),
    path("ftth/lld/projects/<str:project_id>/versions/",
         LldVersionsView.as_view(), name="ftth-lld-versions"),
    path("ftth/lld/projects/<str:project_id>/versions/diff/",
         LldVersionsDiffView.as_view(), name="ftth-lld-versions-diff"),
]
