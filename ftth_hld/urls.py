"""
URL routing for the FTTH HLD module.

All endpoints are prefixed with ``api/ftth/hld/`` and require JWT auth.
Matches the URL structure the frontend ``ftth-api.js`` expects.

The LLD review/run workflow lives in the ``ftth_lld`` app
(``api/ftth/lld/...``).
"""

from django.urls import path

from .api import (
    AreaFetchView,
    BoqDownloadView,
    BoqRegenerateView,
    BoqView,
    CountriesView,
    DeleteProjectView,
    DesignPackageView,
    DownloadFileView,
    FtthProjectAssignView,
    FtthProjectListView,
    InputLayerView,
    LayerGeoJSONView,
    OsmStatusView,
    PipelineStatusView,
    PlacesView,
    ResolveAreaView,
    RunFromAreaView,
    RunPipelineView,
    SurfaceAIImageryView,
    SurfaceAIPointClassifyView,
    SurfaceAIReviewView,
    SurveyPackageView,
)
from .design_api import RunTrenchDesignView, TrenchDesignView

urlpatterns = [
    # POST /api/ftth/hld/run/       — start pipeline (multipart upload)
    path('ftth/hld/run/', RunPipelineView.as_view(), name='ftth-run'),
    # POST /api/ftth/hld/resolve-area/   — area name -> boundary (+ counts)
    path('ftth/hld/resolve-area/', ResolveAreaView.as_view(), name='ftth-resolve-area'),
    # GET  /api/ftth/hld/area-fetch/    — progress of the area's OSM download
    path('ftth/hld/area-fetch/', AreaFetchView.as_view(), name='ftth-area-fetch'),
    # POST /api/ftth/hld/input-layers/ — pre-run OSM/HLD input layer
    path('ftth/hld/input-layers/', InputLayerView.as_view(), name='ftth-input-layer'),
    # POST /api/ftth/hld/run-from-area/  — area name -> a full HLD run
    path('ftth/hld/run-from-area/', RunFromAreaView.as_view(), name='ftth-run-from-area'),
    # GET  /api/ftth/hld/countries/      — country options for the area input
    path('ftth/hld/countries/', CountriesView.as_view(), name='ftth-countries'),
    # GET  /api/ftth/hld/places/         — city suggestions for the area input
    path('ftth/hld/places/', PlacesView.as_view(), name='ftth-places'),
    # GET  /api/ftth/hld/osm-status/     — what the local OSM store holds
    path('ftth/hld/osm-status/', OsmStatusView.as_view(), name='ftth-osm-status'),
    # GET  /api/ftth/hld/results/<id>/ — poll pipeline status & messages
    path('ftth/hld/results/<str:project_id>/', PipelineStatusView.as_view(), name='ftth-status'),
    # GET  /api/ftth/hld/results/<id>/surface-ai-review/ — review-only suggestions
    path(
        'ftth/hld/results/<str:project_id>/surface-ai-review/',
        SurfaceAIReviewView.as_view(),
        name='ftth-surface-ai-review',
    ),
    # POST /api/ftth/hld/results/<id>/surface-ai-review/classify/ — one clicked point
    # or one opted-in span (advisory only)
    path(
        'ftth/hld/results/<str:project_id>/surface-ai-review/classify/',
        SurfaceAIPointClassifyView.as_view(),
        name='ftth-surface-ai-classify',
    ),
    # POST /api/ftth/hld/results/<id>/surface-ai-review/imagery/ — patch preview,
    # imagery only, no model call
    path(
        'ftth/hld/results/<str:project_id>/surface-ai-review/imagery/',
        SurfaceAIImageryView.as_view(),
        name='ftth-surface-ai-imagery',
    ),
    # GET  /api/ftth/hld/results/<id>/layers/<name>/ — GeoJSON for one layer
    path(
        'ftth/hld/results/<str:project_id>/layers/<str:layer_name>/',
        LayerGeoJSONView.as_view(),
        name='ftth-layer',
    ),
    # GET  /api/ftth/hld/download/<id>/<path> — download an output file
    path(
        'ftth/hld/download/<str:project_id>/<path:file_path>',
        DownloadFileView.as_view(),
        name='ftth-download',
    ),
    # GET  /api/ftth/hld/results/<id>/survey-package/ — field-survey subset (ZIP)
    path(
        'ftth/hld/results/<str:project_id>/survey-package/',
        SurveyPackageView.as_view(),
        name='ftth-survey-package',
    ),
    # GET  /api/ftth/hld/results/<id>/trench-design/ — designed trench network
    path(
        'ftth/hld/results/<str:project_id>/trench-design/',
        TrenchDesignView.as_view(),
        name='ftth-trench-design',
    ),
    # POST /api/ftth/hld/results/<id>/trench-design/run/ — (re)run the designer
    path(
        'ftth/hld/results/<str:project_id>/trench-design/run/',
        RunTrenchDesignView.as_view(),
        name='ftth-trench-design-run',
    ),
    # GET  /api/ftth/hld/results/<id>/design-package/ — full design package (ZIP)
    path(
        'ftth/hld/results/<str:project_id>/design-package/',
        DesignPackageView.as_view(),
        name='ftth-design-package',
    ),
    # GET  /api/ftth/hld/projects/ — list recent pipeline runs
    path('ftth/hld/projects/', FtthProjectListView.as_view(), name='ftth-projects'),
    # DELETE /api/ftth/hld/projects/<project_id>/ — delete a project
    path(
        'ftth/hld/projects/<str:project_id>/',
        DeleteProjectView.as_view(),
        name='ftth-delete-project',
    ),
    # POST  /api/ftth/hld/projects/<project_id>/assign/ — assign to engineer
    path(
        'ftth/hld/projects/<str:project_id>/assign/',
        FtthProjectAssignView.as_view(),
        name='ftth-assign-project',
    ),
    # GET  /api/ftth/hld/results/<id>/boq/ — computed BOQ/BOM (JSON)
    path('ftth/hld/results/<str:project_id>/boq/', BoqView.as_view(), name='ftth-boq'),
    # GET  /api/ftth/hld/results/<id>/boq/download/ — BOQ/BOM XLSX
    path(
        'ftth/hld/results/<str:project_id>/boq/download/',
        BoqDownloadView.as_view(),
        name='ftth-boq-download',
    ),
    # POST /api/ftth/hld/results/<id>/boq/regenerate/ — force recompute
    path(
        'ftth/hld/results/<str:project_id>/boq/regenerate/',
        BoqRegenerateView.as_view(),
        name='ftth-boq-regenerate',
    ),
]
