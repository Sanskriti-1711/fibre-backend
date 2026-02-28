from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.models import Project
from projects.services import CompletionService


class ProjectCompletionAPIView(APIView):
    """GET /api/projects/<project_id>/completion/"""

    def get(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        result = CompletionService.calculate_project_completion(project)

        return Response(result)
