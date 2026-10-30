from django.http import HttpResponse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.models import Project
from supervision.services.export_service import build_project_workbook, export_filename

XLSX_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class ProjectExportAPIView(APIView):
    """GET /api/projects/<project_id>/export/

    Whole-project Excel export: a Summary sheet plus one sheet per layer.
    Supervision's as-built handover pack (plan G6 / Stage 8).
    """

    def get(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {'detail': 'Project not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        user = request.user
        generated_by = None
        if user and user.is_authenticated:
            generated_by = getattr(user, 'full_name', None) or user.email

        stream = build_project_workbook(project, generated_by=generated_by)
        filename = export_filename(project)

        response = HttpResponse(stream.getvalue(), content_type=XLSX_CONTENT_TYPE)
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        # A browser on another origin can only read the filename if the header
        # is explicitly exposed.
        response['Access-Control-Expose-Headers'] = 'Content-Disposition'
        return response
