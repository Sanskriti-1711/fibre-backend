from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from projects.models.project import Project
from .serializers import ProjectSerializer


class ProjectListCreateAPIView(APIView):
    """
    POST  /api/projects/        -> create project
    GET   /api/projects/        -> list projects (with filters)
    """

    def get(self, request):
        qs = Project.objects.all().order_by("-created_at")

        # Filters
        status_filter = request.GET.get("status")
        region_filter = request.GET.get("region")

        if status_filter:
            qs = qs.filter(status=status_filter)

        if region_filter:
            qs = qs.filter(region__icontains=region_filter)

        serializer = ProjectSerializer(qs, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = ProjectSerializer(data=request.data)
        if serializer.is_valid():
            project = serializer.save()
            return Response(
                ProjectSerializer(project).data,
                status=status.HTTP_201_CREATED
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class ProjectDetailAPIView(APIView):
    """
    GET /api/projects/<uuid>/
    """

    def get(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = ProjectSerializer(project)
        return Response(serializer.data)

from django.utils.timezone import now
from datetime import timedelta


class LatestProjectUpdatesAPIView(APIView):
    """
    GET /api/projects/latest/
    """

    def get(self, request):
        since = now() - timedelta(days=7)

        qs = Project.objects.filter(
            last_activity_at__isnull=False,
            last_activity_at__gte=since
        ).order_by("-last_activity_at")[:10]

        serializer = ProjectSerializer(qs, many=True)
        return Response(serializer.data)
