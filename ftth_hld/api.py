"""
Django REST Framework API views for the FTTH HLD module.

All pipeline operations are **proxied** to the FastAPI engine
(``HLD_Planning_01/web/backend``) via HTTP. The Django app acts as an API gateway —
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
import logging
from pathlib import Path

from django.http import JsonResponse, HttpResponse
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from .assign import accept_survey_project, assign_hld_project
from .boq import (
    compute_quantities,
    generate_snapshot,
    render_boq_xlsx,
    totals_for,
)
from .config import LAYER_NAME_MAP, STAGES
from .models import FtthProject, FtthLayer
logger = logging.getLogger(__name__)

from .pipeline import (
    HOST_OUTPUTS_DIR,
    EngineError,
    delete_project,
    get_area_fetch,
    get_input_layer,
    list_countries,
    osm_status,
    resolve_area,
    run_from_area,
    suggest_places,
    generate_design_package,
    generate_survey_package,
    get_download_file,
    get_layer_geojson,
    ftth_project_payloads,
    get_status,
    persist_layer,
    run_pipeline,
    sync_project_layers,
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

        # Optional OSM reference layers (stored with the project; the design
        # algorithm does not consume them yet — they will feed routing
        # constraints / permits in a later phase).
        osm_layers = {
            key: request.FILES.get(key)
            for key in ("railways", "waterways", "water", "landuse", "natural")
        }

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

        # Optional OSM reference layers — write to inputs/ (stored, not used
        # by the current algorithm).
        osm_paths = {}
        for key, upload in osm_layers.items():
            if upload:
                path = host_input_dir / (upload.name or f"{key}.zip")
                with open(path, "wb") as f:
                    for chunk in upload.chunks():
                        f.write(chunk)
                osm_paths[key] = str(path)

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
                osm_layer_paths=osm_paths or None,
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
# POST /api/ftth/hld/resolve-area/  — area name -> boundary (+ counts)
# ======================================================================

def _area_inputs(data) -> dict:
    """Structured area fields from a request body, empties dropped.

    Accepts the structured form (country / city / postcode / area_name) and the
    older single ``area`` label.  Nothing is joined here: the engine owns the
    composition, so there is one implementation of how the parts become a
    search string.
    """
    fields = {}
    for key in ("country", "city", "postcode", "area_name", "area"):
        value = str(data.get(key) or "").strip()
        if value:
            fields[key] = value
    return fields


def _has_locator(fields: dict) -> bool:
    """A country on its own is not an area, and never was."""
    return any(fields.get(k) for k in ("area", "city", "postcode", "area_name"))


# A short display label for the project row, before the engine returns the
# canonical composed label (which the row is updated with below).
_DISPLAY_LABEL_KEYS = ("area_name", "postcode", "city")


def _display_label(fields: dict) -> str:
    parts = [fields[k] for k in _DISPLAY_LABEL_KEYS if fields.get(k)]
    if fields.get("country"):
        parts.append(str(fields["country"]).upper())
    return ", ".join(parts) or fields.get("area", "")


class ResolveAreaView(APIView):
    """Resolve an area to its boundary, premises and household mix.

    Read-only and pipeline-free.  ``boundary_only=true`` returns as soon as
    Nominatim resolves the area so the map can draw the boundary immediately;
    the premise counts need the area's OSM data and are a separate, slower
    call (cached from then on).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        fields = _area_inputs(request.data)
        if not _has_locator(fields):
            return JsonResponse(
                {"detail": "Give a postcode and/or a place, street or city name."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            payload = resolve_area(
                fields.get("area", ""),
                boundary_only=bool(request.data.get("boundary_only")),
                max_premises=request.data.get("max_premises"),
                country=fields.get("country", ""),
                city=fields.get("city", ""),
                postcode=fields.get("postcode", ""),
                area_name=fields.get("area_name", ""),
            )
        except EngineError as exc:
            return JsonResponse({"detail": exc.detail}, status=exc.status_code)
        return JsonResponse(payload)


# ======================================================================
# POST /api/ftth/hld/input-layers/  — pre-run OSM/HLD input layer
# ======================================================================

class InputLayerView(APIView):
    """Return buildings, premises, roads and OSM reference layers before HLD."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        fields = _area_inputs(request.data)
        layer = str(request.data.get("layer") or "").strip()
        if not _has_locator(fields) or not layer:
            return JsonResponse(
                {"detail": "Both 'layer' and an area (postcode and/or place) are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            return JsonResponse(get_input_layer(
                fields.get("area", ""),
                layer,
                country=fields.get("country", ""),
                city=fields.get("city", ""),
                postcode=fields.get("postcode", ""),
                area_name=fields.get("area_name", ""),
            ))
        except EngineError as exc:
            return JsonResponse({"detail": exc.detail}, status=exc.status_code)


# ======================================================================
# GET /api/ftth/hld/area-fetch/  — progress of the area's OSM download
# ======================================================================

class AreaFetchView(APIView):
    """What the engine's OSM download for an area is doing right now.

    A cold area can take 10-16 minutes to download.  The page polls this while
    it waits, so the wait says what it is doing ("downloading roads, 3 of 4
    groups, 9 minutes in") instead of looking like a failure.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        area = str(request.query_params.get("area") or "").strip()
        bbox = str(request.query_params.get("bbox") or "").strip()
        if not area and not bbox:
            return JsonResponse(
                {"detail": "Give an 'area' or a 'bbox' (lon_w,lon_e,lat_s,lat_n)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return JsonResponse(get_area_fetch(area=area, bbox=bbox))


# ======================================================================
# POST /api/ftth/hld/run-from-area/  — area name -> a full HLD run
# ======================================================================

class RunFromAreaView(APIView):
    """Start a full HLD run from an area name — no files to prepare.

    Mirrors RunPipelineView (same project row, same status flow, same
    response shape) so the existing status/results pages work untouched.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        fields = _area_inputs(request.data)
        if not _has_locator(fields):
            return JsonResponse(
                {"detail": "Give a postcode and/or a place, street or city name."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        name = str(request.data.get("name") or "")
        try:
            poly_method = int(request.data.get("poly_method", 3))
        except (TypeError, ValueError):
            poly_method = 3

        project_id = uuid.uuid4().hex
        FtthProject.objects.create(
            project_id=project_id,
            name=name or _display_label(fields),
            created_by=request.user if request.user.is_authenticated else None,
            status=FtthProject.STATUS_QUEUED,
        )

        try:
            engine_result = run_from_area(
                fields.get("area", ""),
                project_id=project_id,
                name=name,
                poly_method=poly_method,
                country=fields.get("country", ""),
                city=fields.get("city", ""),
                postcode=fields.get("postcode", ""),
                area_name=fields.get("area_name", ""),
            )
        except EngineError as exc:
            # The row was already created, so mark it failed rather than leaving
            # it queued forever behind a 502.
            FtthProject.objects.filter(pk=project_id).update(
                status=FtthProject.STATUS_FAILED, error=str(exc.detail)[:2000]
            )
            return JsonResponse(
                {"detail": exc.detail, "project_id": project_id},
                status=exc.status_code,
            )

        # The engine composes the canonical label from the parts; when the
        # planner typed no project name, that label is what the project is called.
        area_label = str(engine_result.get("area") or "") or _display_label(fields)
        engine_status = engine_result.get("status", "queued")
        updates = {"status": engine_status}
        if not name:
            updates["name"] = area_label
        FtthProject.objects.filter(pk=project_id).update(**updates)

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
            "area": area_label,
            "results_url": f"/api/ftth/hld/results/{project_id}/",
            "tile_url_template": (
                f"/tiles/{{layer}}/{{z}}/{{x}}/{{y}}.pbf?project_id={project_id}"
            ),
        }, status=status.HTTP_202_ACCEPTED)


# ======================================================================
# GET /api/ftth/hld/osm-status/  — what the local OSM store holds
# ======================================================================

class OsmStatusView(APIView):
    """Report the engine's local OSM store (empty is normal, not an error)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return JsonResponse(osm_status())


# ======================================================================
# GET /api/ftth/hld/countries/  — country options for the area input
# ======================================================================

class CountriesView(APIView):
    """Country options for the area input's country dropdown.

    A static ISO 3166-1 list served by the engine.  This is what removed the
    guesswork from a bare postcode: the country is chosen, not inferred, so a
    five-digit code is no longer read as German by default.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return JsonResponse({"countries": list_countries()})


# ======================================================================
# GET /api/ftth/hld/places/  — city suggestions for the area input
# ======================================================================

class PlacesView(APIView):
    """City/town suggestions for the city combobox, filtered by country.

    Best-effort by design: an empty list is a normal answer while someone is
    typing, and an engine outage is reported in ``reason`` rather than as a 5xx
    on a form that is still being filled in.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        q = str(request.query_params.get("q") or "").strip()
        country = str(request.query_params.get("country") or "").strip()
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 8), 20))
        except (TypeError, ValueError):
            limit = 8
        return JsonResponse(suggest_places(q, country=country, limit=limit))


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
        engine_status_raw = status_data.get("status")

        # When the engine restarts it loses its in-memory task registry,
        # so it may report "queued" or "unknown" even though the run
        # completed long ago.  Fall back to the persisted DB status in
        # that case so the frontend still sees the correct layer list.
        # Also handle the case where the DB status was never updated to
        # "completed" (engine died mid-poll) but layers exist in the DB.
        db_has_layers = FtthLayer.objects.filter(
            ftth_project__project_id=project_id
        ).exists()
        if engine_status_raw in ("unknown", "queued") and (
            project.status == "completed" or db_has_layers
        ):
            # Mark as completed if layers exist (even if DB status was stale)
            if db_has_layers and project.status != "completed":
                FtthProject.objects.filter(pk=project_id).update(
                    status="completed", progress=100
                )

            status_data = {
                "project_id": project_id,
                "status": "completed",
                "progress": 100,
                "stage_name": "Complete",
                "messages": [],
                "layers": [
                    {"name": l.name, "count": l.feature_count}
                    for l in FtthLayer.objects.filter(
                        ftth_project__project_id=project_id
                    )
                ],
                "downloads": [],
                "created_at": project.created_at.isoformat() if project.created_at else "",
                "updated_at": project.updated_at.isoformat() if project.updated_at else "",
            }

        if status_data.get("status") == "unknown":
            # A non-completed run must never advertise a finished progress
            # bar, even when the engine is unreachable and we fall back to
            # the last persisted Django row.
            fallback_status = project.status
            fallback_progress = int(project.progress or 0)
            if fallback_status != "completed":
                fallback_progress = min(fallback_progress, 99)
            return JsonResponse({
                "project_id": project_id,
                "status": fallback_status,
                "stage": project.stage_name,
                "stage_index": project.stage_index,
                "stage_count": project.stage_count,
                "progress": fallback_progress,
                "messages": [],
                "layers": [],
                "downloads": [],
                "created_at": project.created_at.isoformat(),
                "updated_at": project.updated_at.isoformat(),
                "results_url": f"/api/ftth/hld/results/{project_id}/",
            })

        engine_status = status_data.get("status")
        is_completed = engine_status == "completed"
        if engine_status and (engine_status != project.status or is_completed):
            # Persist the engine progress, but never record 100% for a run
            # that has not actually completed (guards stale DB rows).
            persisted_progress = int(status_data.get("progress", 0) or 0)
            if not is_completed:
                persisted_progress = min(persisted_progress, 99)
            else:
                persisted_progress = 100
            FtthProject.objects.filter(pk=project_id).update(
                status=engine_status,
                progress=persisted_progress,
                stage_name=status_data.get("stage_name") or "",
                stage_index=int(status_data.get("stage_index") or 0),
                completed_at=(
                    timezone.now() if is_completed else project.completed_at
                ),
                error_message=status_data.get("error") or "",
            )

        # Attach survey-assignment info so the results/status pages can show
        # who this HLD run is assigned to for the field survey, plus the
        # survey copy status (assigned / active / submitted / ...).
        try:
            from projects.models import Project as SurveyProject
            from assignments.models import AssignmentJob
            copy = SurveyProject.objects.filter(
                source_ftth_project_id=project_id
            ).first()
            assigned_engineers = []
            if copy is not None:
                for job in AssignmentJob.objects.filter(
                    project=copy, scope=AssignmentJob.SCOPE_PROJECT
                ).select_related("assignee"):
                    assigned_engineers.append({
                        "id": str(job.assignee.id),
                        "email": job.assignee.email,
                        "full_name": job.assignee.full_name,
                    })
            status_data["survey"] = {
                "copy_project_id": str(copy.id) if copy else None,
                "copy_name": copy.name if copy else None,
                "status": copy.status if copy else None,
                "assigned_engineers": assigned_engineers,
            }
        except Exception:
            status_data["survey"] = None

        # Persist completed layers into the GIS table (idempotent, best-effort)
        # so the results map is backed by the database, not just the engine.
        if status_data.get("status") == "completed":
            try:
                layer_names = [
                    (l.get("name") or "").lower()
                    for l in status_data.get("layers", [])
                    if l.get("name")
                ]
                sync_project_layers(project_id, layer_names)
            except Exception:
                pass

            # ── The post-HLD chain runs OFF this request ──────────────
            # Road class -> street-attributed sections -> permit matrix ->
            # preliminary package used to run right here, guarded by data the
            # chain itself produces: "any gis.trench_layer row with a NULL
            # fclass" (the engine's trench payload carries no fclass, so every
            # fresh run re-arms it) and trenches.updated_at vs
            # trench_sections.updated_at (any re-publish bumps the former).
            # Measured cost of one re-armed poll: 234.7 s for the road
            # attribution alone, against a 405,599-road extract.
            #
            # `schedule_post_hld` claims a durable HldPostProcess row keyed on
            # the trench CONTENT revision and runs the steps in a background
            # thread, so this request only reads back the state it left.
            try:
                from .posthld import post_hld_state, schedule_post_hld

                schedule_post_hld(project_id, project.name or project_id)
                status_data["post_process"] = post_hld_state(project_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "HLD post-process scheduling failed for %s: %s",
                    project_id, exc,
                )

        # Enrich layer counts from the persisted GIS table (fast, read-only).
        try:
            persisted = {
                row.name: row.feature_count
                for row in FtthLayer.objects.filter(
                    ftth_project__project_id=project_id
                )
            }
            for layer in status_data.get("layers", []):
                persisted_name = (layer.get("name") or "").lower()
                if persisted_name in persisted:
                    layer["count"] = persisted[persisted_name]
        except Exception:
            pass

        # Final guard: cap the progress we serve at 99% unless the run is
        # actually completed, so a buggy/stale backend can never drive the
        # UI progress bar to 100% mid-run.
        if status_data.get("status") != "completed":
            try:
                status_data["progress"] = min(
                    int(status_data.get("progress") or 0), 99
                )
            except (TypeError, ValueError):
                status_data["progress"] = 0

        return JsonResponse(status_data)


# ======================================================================
# GET /api/ftth/hld/results/<project_id>/layers/<layer_name>/
# ======================================================================

# The five trench sub-layers (feeder/distribution/garden/drill/final) merge
# into gis.trench_layer with colliding fids — the unique key is the gis row
# ``id``. This helper injects that id (+ fclass from the roads attribution)
# into the served layer so the frontend can match permit-matrix rows.

def _enrich_trench_layer(project_id: str, geojson: dict) -> dict:
    """Rebuild the trenches layer from the live gis.trench_layer table.

    The five trench sub-layers (feeder/distribution/garden/drill/final) merge
    into gis.trench_layer with colliding fids, so the permit matrix keys on
    the unique gis row ``id`` (bigserial). The persisted FtthLayer snapshot
    can be stale or built from the disk-merge fallback (no fclass), so this
    rebuilds the FeatureCollection straight from the gis table and stamps
    ``properties.feature_id`` = gis id — the exact key the frontend matches
    against the permit matrix for segment colouring.
    """
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute(
            "SELECT id, ST_AsGeoJSON(geom), properties "
            "FROM gis.trench_layer WHERE project_id = %s AND geom IS NOT NULL "
            "ORDER BY id",
            [project_id],
        )
        rows = cur.fetchall()
    if not rows:
        return geojson

    features = []
    for gid, geom_json, props in rows:
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except (TypeError, ValueError):
                props = {}
        props = dict(props or {})
        props["feature_id"] = str(gid)
        features.append({
            "type": "Feature",
            "id": gid,
            "geometry": json.loads(geom_json) if geom_json else None,
            "properties": props,
        })
    return {"type": "FeatureCollection", "features": features}


class LayerGeoJSONView(APIView):
    """Return a pipeline layer as GeoJSON, served from the DB when available."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id, layer_name):
        name = layer_name.lower()
        if name not in LAYER_NAME_MAP:
            return JsonResponse(
                {"detail": f"Unknown layer '{layer_name}'. "
                           f"Valid: {', '.join(LAYER_NAME_MAP.keys())}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # 1. Serve from the persisted GIS table if we already have it.
        #    A row persisted mid-run (before ingestion) can hold an empty
        #    FeatureCollection — treat that as stale and refresh from the
        #    engine instead of serving an empty layer forever.
        row = FtthLayer.objects.filter(
            ftth_project__project_id=project_id, name=name
        ).first()
        if row is not None and row.geojson and row.geojson.get("features"):
            geojson = row.geojson
            # The trench layer is the union of five sub-layers whose fids
            # collide; the permit matrix keys on the unique gis row ``id``.
            # Enrich the served features with that id (+ fclass) so the map
            # can colour segments by permit status.
            if name in ("trenches", "trench_layer"):
                try:
                    geojson = _enrich_trench_layer(project_id, geojson)
                except Exception:
                    pass
            return JsonResponse(geojson)

        # 2. Otherwise fetch from the engine and persist for next time.
        geojson_bytes = get_layer_geojson(project_id, name)
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

        try:
            persist_layer(project_id, name, data)
        except Exception:
            pass  # persistence is best-effort; still return the layer data

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
# GET /api/ftth/hld/results/<project_id>/design-package/
# ======================================================================

class DesignPackageView(APIView):
    """
    Generate and download the HLD design package — a single ZIP with
    every output layer (GPKG + WGS84 GeoJSON) plus the BOQ / BOM and
    any other generated documents.
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
                {"detail": "Pipeline has not completed yet. Design package is only "
                           "available for completed pipelines."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            zip_bytes = generate_design_package(project_id)
        except FileNotFoundError as exc:
            return JsonResponse(
                {"detail": str(exc)},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as exc:
            return JsonResponse(
                {"detail": f"Failed to generate design package: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        zip_name = f"{project_id}_design_package.zip"

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

        # 1. Remove the Django record FIRST.
        #
        # The engine's project table is referenced by Django's permit matrix
        # (PermitMatrix.project → FtthProject), so the engine's own
        # `DELETE FROM ftth_projects` fails with a foreign-key violation if
        # any permit row is still present.  Dropping our rows first (they
        # cascade: layers, LLD runs, permits, submissions, documents) releases
        # the reference, and only then is the engine free to clean up disk
        # + PostGIS.
        project.delete()

        # 2. Delete from the FastAPI engine (disk + PostGIS).  Non-fatal: the
        # Django side is already gone, so a failure here leaves orphaned
        # engine files at worst, and that is reported to the caller.
        engine_result = delete_project(project_id)

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
            # PostGIS cleanup is best-effort on the engine side; surface a
            # failure so an orphaned project row is not discovered later.
            "postgis_cleaned": engine_result.get("postgis_cleaned", True),
            "postgis_error": engine_result.get("postgis_error"),
        })


# ======================================================================
# GET /api/ftth/hld/results/<project_id>/boq/
# ======================================================================

class BoqView(APIView):
    """
    Return the computed BOQ/BOM for a completed HLD run.

    GET /api/ftth/hld/results/<id>/boq/ → JSON with boq_rows, bom_rows,
    boq_totals, bom_totals, generated_at.
    GET /api/ftth/hld/results/<id>/boq/download/ → XLSX (BoQ + BoM sheets)
    GET /api/ftth/hld/results/<id>/boq/regenerate/ → force recompute
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
                {"detail": "BOQ is only available for completed pipelines."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            snapshot = generate_snapshot(project_id)
        except ValueError as exc:
            return JsonResponse({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except Exception as exc:
            return JsonResponse(
                {"detail": f"Failed to generate BOQ: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Tier-1 A4: anomaly screening — flags quantities that look wrong
        # (zero length items, dominant line items, outliers vs siblings).
        from .boq_anomalies import detect_boq_anomalies
        try:
            anomalies = detect_boq_anomalies(project_id)
        except Exception:
            anomalies = {"anomalies": [], "checked": 0, "basis": {}}

        return JsonResponse({
            "project_id": project_id,
            "boq_rows": snapshot.boq_json,
            "bom_rows": snapshot.bom_json,
            "boq_totals": snapshot.boq_totals,
            "bom_totals": snapshot.bom_totals,
            "anomalies": anomalies,
            "generated_at": snapshot.regenerated_at or snapshot.created_at,
        })


class BoqRegenerateView(APIView):
    """Force-recompute the BOQ/BOM snapshot for a project."""

    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        try:
            project = FtthProject.objects.get(pk=project_id)
        except FtthProject.DoesNotExist:
            return JsonResponse(
                {"detail": "Project not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if project.status not in (FtthProject.STATUS_COMPLETED, "completed"):
            return JsonResponse(
                {"detail": "BOQ is only available for completed pipelines."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            snapshot = generate_snapshot(project_id, force=True)
        except Exception as exc:
            return JsonResponse(
                {"detail": f"Failed to regenerate BOQ: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return JsonResponse({
            "project_id": project_id,
            "boq_rows": snapshot.boq_json,
            "bom_rows": snapshot.bom_json,
            "boq_totals": snapshot.boq_totals,
            "bom_totals": snapshot.bom_totals,
            "generated_at": snapshot.regenerated_at or snapshot.created_at,
        })


class BoqDownloadView(APIView):
    """Download the BOQ/BOM workbook as XLSX."""

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
                {"detail": "BOQ is only available for completed pipelines."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            data = render_boq_xlsx(project_id)
        except Exception as exc:
            return JsonResponse(
                {"detail": f"Failed to generate BOQ workbook: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return HttpResponse(
            data,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="{project_id}_BOQ.xlsx"',
                "Content-Length": str(len(data)),
            },
        )


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


# ======================================================================
# POST /api/ftth/hld/projects/<project_id>/assign/
# ======================================================================

class FtthProjectAssignView(APIView):
    """Assign a completed HLD run to a field engineer.

    Creates (or reuses) the Survey copy of the HLD run, auto-imports the
    generated survey package into it, creates the project-scope AssignmentJob
    and marks the engineer on the HLD project. SUBADMIN only.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        if getattr(request.user, "role", None) != "SUBADMIN":
            return JsonResponse(
                {"detail": "Only SUBADMIN can assign projects."},
                status=status.HTTP_403_FORBIDDEN,
            )

        engineer_ids = (
            request.data.get("engineer_ids")
            or request.POST.getlist("engineer_ids")
            or request.data.get("engineer_id")
            or request.POST.get("engineer_id")
        )
        if not engineer_ids:
            return JsonResponse(
                {"detail": "engineer_ids (list) or engineer_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = assign_hld_project(project_id, engineer_ids)
        except ValueError as exc:
            return JsonResponse({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except FileNotFoundError as exc:
            return JsonResponse(
                {"detail": f"Survey package not available: {exc}"},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as exc:
            return JsonResponse(
                {"detail": f"Assignment failed: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return JsonResponse(result, status=status.HTTP_201_CREATED)
