"""
URL routing for the FTTH HLD module.

All endpoints are prefixed with ``api/ftth/hld/`` and require JWT auth.
Matches the URL structure the frontend ``ftth-api.js`` expects.
"""

from django.urls import path

from .api import (
    DeleteProjectView,
    DownloadFileView,
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

    # GET  /api/ftth/hld/results/<id>/survey-package/ — ZIP of all GPKGs + BOQ + BOM
    path("ftth/hld/results/<str:project_id>/survey-package/",
         SurveyPackageView.as_view(), name="ftth-survey-package"),

    # GET  /api/ftth/hld/projects/ — list recent pipeline runs
    path("ftth/hld/projects/", FtthProjectListView.as_view(), name="ftth-projects"),

    # DELETE /api/ftth/hld/projects/<project_id>/ — delete a project
    path("ftth/hld/projects/<str:project_id>/", DeleteProjectView.as_view(), name="ftth-delete-project"),
]
