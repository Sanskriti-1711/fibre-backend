from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from projects.models.project import Project
from projects.services.import_service import import_geopackage

class ImportGeoPackageAPIView(APIView):
    """
    POST /api/projects/<project_id>/import/
    """

    def post(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        gpkg = request.FILES.get("file")
        if not gpkg:
            return Response(
                {"detail": "GeoPackage file is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            session = import_geopackage(project, gpkg)
        except Exception as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response(
            {
                "import_session_id": session.id,
                "status": session.status,
                "summary": session.validation_summary,
            },
            status=status.HTTP_201_CREATED
        )
