"""
URL routing for the FTTH HLD module.

All endpoints are prefixed with ``api/ftth/hld/`` and require JWT auth.
Matches the URL structure the frontend ``ftth-api.js`` expects.
"""

from django.urls import path

from .lld_api import (
    LldApprovedVersionView,
    LldChangeActionView,
    LldDownloadView,
    LldLayerView,
    LldProjectsView,
    LldReviewView,
    LldRunsView,
    LldRunStatusView,
    LldRunView,
    LldVersionsView,
)

from .api import (
    DeleteProjectView,
    DesignPackageView,
    DownloadFileView,
    FtthProjectAssignView,
    FtthProjectListView,
    LayerGeoJSONView,
    PipelineStatusView,
    RunPipelineView,
    SurveyPackageView,
)

urlpatterns = [
    # POST /api/ftth/hld/run/       — start pipeline (multipart upload)
    path("ftth/hld/run/", RunPipelineView.as_view(), name="ftth-run"),

    # GET  /api/ftth/hld/results/<id>/ — poll pipeline status & messages
    path("ftth/hld/results/<str:project_id>/", PipelineStatusView.as_view(), name="ftth-status"),

    # GET  /api/ftth/hld/results/<id>/layers/<name>/ — GeoJSON for one layer
    path("ftth/hld/results/<str:project_id>/layers/<str:layer_name>/",
         LayerGeoJSONView.as_view(), name="ftth-layer"),

    # GET  /api/ftth/hld/download/<id>/<path> — download an output file
    path("ftth/hld/download/<str:project_id>/<path:file_path>",
         DownloadFileView.as_view(), name="ftth-download"),

    # GET  /api/ftth/hld/results/<id>/survey-package/ — field-survey subset (ZIP)
    path("ftth/hld/results/<str:project_id>/survey-package/",
         SurveyPackageView.as_view(), name="ftth-survey-package"),

    # GET  /api/ftth/hld/results/<id>/design-package/ — full design package (ZIP)
    path("ftth/hld/results/<str:project_id>/design-package/",
         DesignPackageView.as_view(), name="ftth-design-package"),

    # GET  /api/ftth/hld/projects/ — list recent pipeline runs
    path("ftth/hld/projects/", FtthProjectListView.as_view(), name="ftth-projects"),

    # DELETE /api/ftth/hld/projects/<project_id>/ — delete a project
    path("ftth/hld/projects/<str:project_id>/", DeleteProjectView.as_view(), name="ftth-delete-project"),

    # POST  /api/ftth/hld/projects/<project_id>/assign/ — assign to engineer
    path("ftth/hld/projects/<str:project_id>/assign/",
         FtthProjectAssignView.as_view(), name="ftth-assign-project"),
]


# ======================================================================
# FTTH LLD — review workflow (backed by real survey data)
# ======================================================================

lld_urlpatterns = [
    path("ftth/lld/projects/",
         LldProjectsView.as_view(), name="ftth-lld-projects"),
    path("ftth/lld/runs/",
         LldRunsView.as_view(), name="ftth-lld-runs-all"),
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
]
urlpatterns += lld_urlpatterns
