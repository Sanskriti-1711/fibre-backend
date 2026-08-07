"""
Django REST Framework API views for the FTTH HLD module.

All pipeline operations are **proxied** to the FastAPI engine
(``ftth-engine/``) via HTTP. The Django app acts as an API gateway —
it handles authentication, file upload, and response formatting, while
the engine handles Docker / ``qgis_process`` orchestration.

Endpoints (all under ``/api/ftth/hld/``):
  POST   /api/ftth/hld/run/                — Start a pipeline
  GET    /api/ftth/hld/results/<id>/        — Poll status / results
  GET    /api/ftth/hld/results/<id>/layers/<name>/ — GeoJSON for a layer
  GET    /api/ftth/hld/download/<id>/<file> — Download output file
  GET    /api/ftth/hld/results/<id>/survey-package/ — ZIP of all GPKGs + BOQ + BOM
  GET    /api/ftth/hld/projects/            — List recent pipeline runs

All endpoints require JWT authentication.
"""

import json
import uuid
from pathlib import Path

from django.http import JsonResponse, HttpResponse
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from .config import LAYER_NAME_MAP, STAGES
from .models import FtthProject
from .pipeline import (
    HOST_OUTPUTS_DIR,
    delete_project,
    generate_survey_package,
    get_download_file,
    get_layer_geojson,
    ftth_project_payloads,
    get_status,
    run_pipeline,
)


# ======================================================================
# POST /api/ftth/hld/run/
# ======================================================================

class RunPipelineView(APIView):
    """
    Accept multipart upload (excel + roads), save files to disk,
    submit to the FastAPI engine, and return a project_id
    immediately (HTTP 202).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        excel = request.FILES.get("excel")
        roads = request.FILES.get("roads")
        brownfield = request.FILES.get("brownfield")
        name = request.POST.get("name", "")
        poly_method = int(request.POST.get("poly_method", 3))

        if not excel or not roads:
            return JsonResponse(
                {"detail": "Both 'excel' and 'roads' files are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        excel_ext = excel.name.split(".")[-1].lower() if excel.name else ""
        roads_ext = roads.name.split(".")[-1].lower() if roads.name else ""
        allowed_excel = {"xlsx", "xls"}
        allowed_roads = {"gpkg", "geojson", "json", "shp", "zip"}

        if excel_ext not in allowed_excel:
            return JsonResponse(
                {"detail": f"Excel must be one of: {', '.join(allowed_excel)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if roads_ext not in allowed_roads:
            return JsonResponse(
                {"detail": f"Roads must be one of: {', '.join(allowed_roads)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        project_id = uuid.uuid4().hex

        host_input_dir = HOST_OUTPUTS_DIR / project_id / "inputs"
        host_input_dir.mkdir(parents=True, exist_ok=True)

        excel_path = host_input_dir / (excel.name or "addresses.xlsx")
        roads_path = host_input_dir / (roads.name or "roads.gpkg")

        with open(excel_path, "wb") as f:
            for chunk in excel.chunks():
                f.write(chunk)
        with open(roads_path, "wb") as f:
            for chunk in roads.chunks():
                f.write(chunk)

        # Optional brownfield (existing infrastructure) ZIP / vector file
        brownfield_path = None
        if brownfield:
            brownfield_path = host_input_dir / (brownfield.name or "brownfield.zip")
            with open(brownfield_path, "wb") as f:
                for chunk in brownfield.chunks():
                    f.write(chunk)

        FtthProject.objects.create(
            project_id=project_id,
            name=name,
            created_by=request.user if request.user.is_authenticated else None,
            status=FtthProject.STATUS_QUEUED,
            excel_filename=excel.name,
            roads_filename=roads.name,
        )

        try:
            engine_result = run_pipeline(
                excel_path=str(excel_path),
                roads_path=str(roads_path),
                project_id=project_id,
                name=name,
                poly_method=poly_method,
                brownfield_path=str(brownfield_path) if brownfield_path else None,
            )
        except RuntimeError as exc:
            return JsonResponse(
                {"detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        engine_status = engine_result.get("status", "queued")
        FtthProject.objects.filter(pk=project_id).update(
            status=engine_status,
        )

        return JsonResponse({
            "project_id": project_id,
            "status": engine_status,
            "stage": None,
            "stage_index": 0,
            "stage_count": len(STAGES),
            "progress": 0,
            "layers": [],
            "downloads": [],
            "messages": [],
            "results_url": f"/api/ftth/hld/results/{project_id}/",
            "tile_url_template": (
                f"/tiles/{{layer}}/{{z}}/{{x}}/{{y}}.pbf?project_id={project_id}"
            ),
        }, status=status.HTTP_202_ACCEPTED)


# ======================================================================
# GET /api/ftth/hld/results/<project_id>/
# ======================================================================

class PipelineStatusView(APIView):
    """Return the current status of a pipeline run, proxied from FastAPI."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        try:
            project = FtthProject.objects.get(pk=project_id)
        except FtthProject.DoesNotExist:
            return JsonResponse(
                {"detail": "Project not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        status_data = get_status(project_id)

        if status_data.get("status") == "unknown":
            return JsonResponse({
                "project_id": project_id,
                "status": project.status,
                "stage": project.stage_name,
                "stage_index": project.stage_index,
                "stage_count": project.stage_count,
                "progress": project.progress,
                "messages": [],
                "layers": [],
                "downloads": [],
                "created_at": project.created_at.isoformat(),
                "updated_at": project.updated_at.isoformat(),
                "results_url": f"/api/ftth/hld/results/{project_id}/",
            })

        engine_status = status_data.get("status")
        if engine_status and engine_status != project.status:
            FtthProject.objects.filter(pk=project_id).update(
                status=engine_status,
                progress=status_data.get("progress", 0),
                stage_name=status_data.get("stage_name", ""),
                stage_index=status_data.get("stage_index", 0),
                error_message=status_data.get("error", ""),
            )

        return JsonResponse(status_data)


# ======================================================================
# GET /api/ftth/hld/results/<project_id>/layers/<layer_name>/
# ======================================================================

class LayerGeoJSONView(APIView):
    """Return a pipeline layer as GeoJSON, proxied from FastAPI."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id, layer_name):
        if layer_name.lower() not in LAYER_NAME_MAP:
            return JsonResponse(
                {"detail": f"Unknown layer '{layer_name}'. "
                           f"Valid: {', '.join(LAYER_NAME_MAP.keys())}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        geojson_bytes = get_layer_geojson(project_id, layer_name)
        if geojson_bytes is None:
            return JsonResponse(
                {"detail": f"Layer '{layer_name}' not found for this project."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            data = json.loads(geojson_bytes)
        except json.JSONDecodeError:
            return JsonResponse(
                {"detail": "Invalid GeoJSON received from pipeline."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return JsonResponse(data)


# ======================================================================
# GET /api/ftth/hld/download/<project_id>/<path:file_path>
# ======================================================================

class DownloadFileView(APIView):
    """Download a pipeline output file, proxied from FastAPI."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id, file_path):
        clean_name = Path(file_path).name
        if not clean_name:
            return JsonResponse(
                {"detail": "Invalid file path."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = get_download_file(project_id, clean_name)
        if data is None:
            return JsonResponse(
                {"detail": "File not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return HttpResponse(
            data,
            content_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{clean_name}"',
                "Content-Length": str(len(data)),
            },
        )


# ======================================================================
# GET /api/ftth/hld/results/<project_id>/survey-package/
# ======================================================================

class SurveyPackageView(APIView):
    """
    Generate and download a survey package — a single ZIP containing
    all output GPKG files + BOQ + BOM for field engineers.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        try:
            project = FtthProject.objects.get(pk=project_id)
        except FtthProject.DoesNotExist:
            return JsonResponse(
                {"detail": "Project not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if project.status not in (FtthProject.STATUS_COMPLETED, "completed"):
            return JsonResponse(
                {"detail": "Pipeline has not completed yet. Survey package is only "
                           "available for completed pipelines."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            zip_bytes = generate_survey_package(project_id)
        except FileNotFoundError as exc:
            return JsonResponse(
                {"detail": str(exc)},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as exc:
            return JsonResponse(
                {"detail": f"Failed to generate survey package: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        zip_name = f"{project_id}_survey_package.zip"

        return HttpResponse(
            zip_bytes,
            content_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{zip_name}"',
                "Content-Length": str(len(zip_bytes)),
            },
        )







# ======================================================================
# DELETE /api/ftth/hld/projects/<project_id>/
# ======================================================================

class DeleteProjectView(APIView):
    """
    Delete a pipeline project.

    Proxies the delete to the FastAPI engine (removes disk + PostGIS data)
    AND removes the Django FtthProject record.
    """

    permission_classes = [IsAuthenticated]

    def delete(self, request, project_id):
        # Check it exists in Django first
        try:
            project = FtthProject.objects.get(pk=project_id)
        except FtthProject.DoesNotExist:
            return JsonResponse(
                {"detail": "Project not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # 1. Attempt to delete from the FastAPI engine
        engine_result = delete_project(project_id)

        # 2. Remove the Django record
        project.delete()

        # 3. Remove local cached files
        import shutil
        project_dir = HOST_OUTPUTS_DIR / project_id
        if project_dir.exists():
            shutil.rmtree(str(project_dir), ignore_errors=True)

        return JsonResponse({
            "deleted": True,
            "project_id": project_id,
            "engine_deleted": engine_result.get("deleted", False),
            "engine_detail": engine_result.get("detail"),
        })


# ======================================================================
# GET /api/ftth/hld/projects/
# ======================================================================

class FtthProjectListView(APIView):
    """List recent FTTH pipeline runs.

    Primary source: Django's ``ftth_projects`` table (always available).
    Enriched with download info from the FastAPI engine when reachable.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        limit = int(request.GET.get("limit", 50))
        return JsonResponse(ftth_project_payloads(limit), safe=False)
