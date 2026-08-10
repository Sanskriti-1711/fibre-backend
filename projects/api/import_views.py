import os
import requests
import json
import zipfile
from uuid import UUID

from django.conf import settings
from django.core.files.storage import default_storage
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser

from projects.models import Project, ImportSession, Feature


MICROSERVICE_BASE_URL = "https://fiber-import.zeabur.app"


def _is_zip_file(filename):
    """Check if a filename has a .zip extension."""
    return filename.lower().endswith(".zip")


def _get_extension(filename):
    """Get the file extension from a filename."""
    _, ext = os.path.splitext(filename.lower())
    return ext


def discover_zip_layers(file_path):
    """List .geojson layers inside a zip, with feature counts.

    Extracted from GpkgDiscoverView so the survey-package auto-import can
    reuse the same discovery logic without an HTTP round-trip.
    """
    layers = []
    with zipfile.ZipFile(file_path, "r") as zf:
        for info in zf.infolist():
            if not info.filename.lower().endswith(".geojson"):
                continue
            layer_name = os.path.splitext(os.path.basename(info.filename))[0]
            feature_count = 0
            try:
                content = zf.read(info.filename).decode("utf-8")
                data = json.loads(content)
                features = data.get("features", [])
                if isinstance(features, list):
                    feature_count = len(features)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            layers.append({
                "name": layer_name,
                "feature_count": feature_count,
                "filename": info.filename,
            })
    return layers


def import_zip_layers(project, file_path, selected_layers):
    """Create Feature records from .geojson layers inside a zip.

    Extracted from GpkgImportView so the survey-package auto-import can
    reuse the same import logic without an HTTP round-trip. Returns the
    number of features created.
    """
    features_created = 0
    selected_lower = [s.lower() for s in (selected_layers or [])]
    with zipfile.ZipFile(file_path, "r") as zf:
        for info in zf.infolist():
            if not info.filename.lower().endswith(".geojson"):
                continue
            layer_name = os.path.splitext(os.path.basename(info.filename))[0]
            if selected_lower and layer_name.lower() not in selected_lower:
                continue
            try:
                content = zf.read(info.filename).decode("utf-8")
                data = json.loads(content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            features = data.get("features", [])
            if not isinstance(features, list):
                continue

            field_schema = None
            if features:
                first_props = features[0].get("properties", {})
                if isinstance(first_props, dict):
                    field_schema = [
                        {"key": k, "label": k.replace("_", " ").title(), "type": "text"}
                        for k in first_props.keys()
                    ]

            for feature_payload in features:
                geom = feature_payload.get("geometry")
                props = feature_payload.get("properties", {})
                if not isinstance(props, dict):
                    props = {}
                if not geom or not isinstance(geom, dict):
                    continue
                Feature.objects.create(
                    project=project,
                    layer_name=layer_name,
                    layer_id=layer_name,
                    properties=props,
                    geometry=geom,
                    field_schema=field_schema,
                    status=Feature.STATUS_PENDING,
                )
                features_created += 1
    return features_created


class GpkgUploadView(APIView):
    """
    POST /api/projects/{project_id}/import/upload/
    Upload .gpkg or .zip file, create ImportSession.
    """
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        file_obj = request.FILES.get("file")
        if not file_obj:
            return Response(
                {"detail": "No file provided"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Accept both .gpkg and .zip files
        name_lower = file_obj.name.lower()
        if not (name_lower.endswith(".gpkg") or name_lower.endswith(".zip")):
            return Response(
                {"detail": "File must be a .gpkg or .zip file"},
                status=status.HTTP_400_BAD_REQUEST
            )

        ext = _get_extension(file_obj.name)
        filename = f"{project_id}{ext}"
        file_path = os.path.join("imports", filename)
        
        # Ensure directory exists
        full_path = os.path.join(settings.MEDIA_ROOT, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        
        # Save file
        saved_path = default_storage.save(file_path, file_obj)
        absolute_path = os.path.join(settings.MEDIA_ROOT, saved_path)

        # Create or update ImportSession
        import_session, _ = ImportSession.objects.update_or_create(
            project=project,
            defaults={
                "original_filename": file_obj.name,
                "stored_file_path": absolute_path,
                "status": "uploaded",
                "validation_summary": None,
            }
        )

        return Response({
            "session_id": str(import_session.id),
            "project_id": str(project_id),
            "filename": saved_path,
            "status": "uploaded"
        }, status=status.HTTP_201_CREATED)


class GpkgDiscoverView(APIView):
    """
    POST /api/projects/{project_id}/import/discover/
    For .gpkg: call microservice.
    For .zip: extract and list .geojson layers.
    """
    def post(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        # Find the import session for this project
        try:
            import_session = ImportSession.objects.get(project=project)
        except ImportSession.DoesNotExist:
            return Response(
                {"detail": "No upload found for this project. Please upload first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        file_path = import_session.stored_file_path
        if not os.path.exists(file_path):
            return Response(
                {"detail": "Uploaded file not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        original_filename = import_session.original_filename or ""

        if _is_zip_file(original_filename):
            # ── ZIP discover: extract & list .geojson layers ────────────────
            try:
                layers = discover_zip_layers(file_path)
            except zipfile.BadZipFile as e:
                return Response(
                    {"detail": f"Invalid zip file: {str(e)}"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            import_session.status = "validated"
            import_session.validation_summary = {"layers": layers}
            import_session.save()

            return Response({
                "project_id": str(project_id),
                "layers": layers,
            })
        else:
            # ── GPKG discover: call microservice ────────────────────────────
            try:
                with open(file_path, "rb") as f:
                    response = requests.post(
                        f"{MICROSERVICE_BASE_URL}/import/gpkg/discover",
                        files={"file": (f"{project_id}.gpkg", f)},
                        timeout=60
                    )
                    response.raise_for_status()
                    layers_data = response.json()
            except requests.RequestException as e:
                return Response(
                    {"detail": f"Error discovering layers: {str(e)}"},
                    status=status.HTTP_502_BAD_GATEWAY
                )

            import_session.status = "validated"
            import_session.validation_summary = {"layers": layers_data}
            import_session.save()

            return Response({
                "project_id": str(project_id),
                "layers": layers_data,
            })


class GpkgImportView(APIView):
    """
    POST /api/projects/{project_id}/import/import/
    For .gpkg: call microservice.
    For .zip: extract .geojson and create Features directly.
    """
    def post(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        selected_layers = request.data.get("selected_layers", [])
        if not selected_layers:
            return Response(
                {"detail": "No layers selected for import"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Find the import session
        try:
            import_session = ImportSession.objects.get(project=project)
        except ImportSession.DoesNotExist:
            return Response(
                {"detail": "No upload found for this project. Please upload first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        file_path = import_session.stored_file_path
        if not os.path.exists(file_path):
            return Response(
                {"detail": "Uploaded file not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        original_filename = import_session.original_filename or ""

        if _is_zip_file(original_filename):
            # ── ZIP import: extract .geojson, create Feature records directly ─
            try:
                features_created = import_zip_layers(project, file_path, selected_layers)
            except zipfile.BadZipFile as e:
                return Response(
                    {"detail": f"Invalid zip file: {str(e)}"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if features_created == 0:
                import_session.status = "failed"
                import_session.save()
                return Response(
                    {"detail": "No features could be created from the selected layers. Check that the zip contains valid GeoJSON files."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            import_session.status = "imported"
            import_session.save()

            if project.status != "active":
                project.status = "active"
                project.save(update_fields=["status", "updated_at", "last_activity_at"])

            return Response({
                "project_id": str(project_id),
                "status": "imported",
                "imported_layers": selected_layers,
                "features_created": features_created,
            })

        else:
            # ── GPKG import: call microservice ──────────────────────────────
            try:
                with open(file_path, "rb") as f:
                    response = requests.post(
                        f"{MICROSERVICE_BASE_URL}/import/gpkg",
                        files={"file": (f"{project_id}.gpkg", f)},
                        data={
                            "project_id": str(project_id),
                            "layers": json.dumps(selected_layers)
                        },
                        timeout=120
                    )
                    response.raise_for_status()
                    import_result = response.json()
            except requests.RequestException as e:
                error_detail = f"Error importing layers: {str(e)}"
                if e.response is not None:
                    error_detail += f" | Status: {e.response.status_code}"
                    try:
                        error_detail += f" | Response: {e.response.text}"
                    except:
                        pass
                return Response(
                    {"detail": error_detail},
                    status=status.HTTP_502_BAD_GATEWAY
                )

            import_session.status = "imported"
            import_session.save()

            # Persist feature data from microservice response
            features_created = False
            layers_result = import_result.get("layers", [])
            if not isinstance(layers_result, list):
                layers_result = []

            for layer_payload in layers_result:
                layer_name = layer_payload.get("layer_name") or layer_payload.get("name")
                layer_id = layer_payload.get("table_name") or layer_payload.get("layer_id")

                if not layer_name or not layer_id:
                    continue

                field_schema = layer_payload.get("field_schema")

                features_payload = layer_payload.get("features", [])
                if not isinstance(features_payload, list):
                    continue

                for feature_payload in features_payload:
                    feature_id = feature_payload.get("id")
                    if not feature_id:
                        continue

                    properties = feature_payload.get("properties", {})

                    Feature.objects.update_or_create(
                        id=feature_id,
                        defaults={
                            "project": project,
                            "layer_name": layer_name,
                            "layer_id": layer_id,
                            "properties": properties,
                            "field_schema": field_schema,
                            "status": Feature.STATUS_PENDING,
                        }
                    )
                    features_created = True

            if features_created and project.status != "active":
                project.status = "active"
                project.save(update_fields=["status", "updated_at", "last_activity_at"])

            return Response({
                "project_id": str(project_id),
                "status": "queued",
                "imported_layers": selected_layers,
                "microservice_response": import_result,
            })


class ImportStatusView(APIView):
    """
    GET /api/projects/{project_id}/import/status/
    Get current import status
    """
    def get(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        try:
            import_session = ImportSession.objects.get(project=project)
        except ImportSession.DoesNotExist:
            return Response({
                "project_id": str(project_id),
                "status": "no_upload"
            })

        return Response({
            "project_id": str(project_id),
            "session_id": str(import_session.id),
            "status": import_session.status,
            "original_filename": import_session.original_filename,
            "validation_summary": import_session.validation_summary,
            "created_at": import_session.created_at,
        })
