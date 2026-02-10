import os
import requests
import json
from uuid import UUID

from django.conf import settings
from django.core.files.storage import default_storage
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser

from projects.models import Project, ImportSession, Feature


MICROSERVICE_BASE_URL = "https://fiber-import.zeabur.app"


class GpkgUploadView(APIView):
    """
    POST /api/projects/{project_id}/import/upload/
    Upload GPKG file, save as {project_id}.gpkg, create ImportSession
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

        # Validate file extension
        if not file_obj.name.endswith(".gpkg"):
            return Response(
                {"detail": "File must be a .gpkg file"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Save file as {project_id}.gpkg in media/imports/
        filename = f"{project_id}.gpkg"
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
    Call microservice /gpkg/discover to get layer list
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

        # Call microservice discover endpoint
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

        # Update session status
        import_session.status = "validated"
        import_session.validation_summary = {"layers": layers_data}
        import_session.save()

        return Response({
            "project_id": str(project_id),
            "layers": layers_data
        })


class GpkgImportView(APIView):
    """
    POST /api/projects/{project_id}/import/import/
    Import selected layers via microservice
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

        # Call microservice import endpoint
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

        # Update session status
        import_session.status = "imported"
        import_session.save()

        # Persist feature data from microservice response
        layers_result = import_result.get("layers", [])
        if not isinstance(layers_result, list):
            layers_result = []

        for layer_payload in layers_result:
            layer_name = layer_payload.get("layer_name") or layer_payload.get("name")
            layer_id = layer_payload.get("table_name") or layer_payload.get("layer_id")

            if not layer_name or not layer_id:
                continue

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
                        "status": Feature.STATUS_PENDING,
                    }
                )

        return Response({
            "project_id": str(project_id),
            "status": "queued",
            "imported_layers": selected_layers,
            "microservice_response": import_result
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
