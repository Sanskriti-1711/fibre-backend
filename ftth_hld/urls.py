"""
URL routing for the FTTH HLD module.

All endpoints are prefixed with ``api/ftth/hld/`` and require JWT auth.
Matches the URL structure the frontend ``ftth-api.js`` expects.
"""

from django.urls import path

from .api import (
    DownloadFileView,
    FtthProjectListView,
    LayerGeoJSONView,
    PipelineProgressView,
    PipelineStatusView,
    RunPipelineView,
    RunStepView,
    SurveyPackageView,
    ValidateInputsView,
)

urlpatterns = [
    # POST /api/ftth/hld/validate/  — pre-flight validation
    path("ftth/hld/validate/", ValidateInputsView.as_view(), name="ftth-validate"),

    # POST /api/ftth/hld/run/       — start pipeline (multipart upload)
    path("ftth/hld/run/", RunPipelineView.as_view(), name="ftth-run"),

    # POST /api/ftth/hld/run/step/<step_name>/ — run a single pipeline step
    path("ftth/hld/run/step/<str:step_name>/", RunStepView.as_view(), name="ftth-run-step"),

    # GET  /api/ftth/hld/results/<id>/ — poll pipeline status & messages
    path("ftth/hld/results/<str:project_id>/", PipelineStatusView.as_view(), name="ftth-status"),

    # GET  /api/ftth/hld/progress/<id>/ — step-by-step progress for resume
    path("ftth/hld/progress/<str:project_id>/", PipelineProgressView.as_view(), name="ftth-progress"),

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
]
