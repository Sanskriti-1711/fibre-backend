import os
import requests

from rest_framework.views import APIView
from django.db.models import Q
from rest_framework.response import Response
from rest_framework import status

from django.db import connection

from projects.models.project import Project
from projects.models.import_session import ImportSession
from ftth_hld.assign import accept_survey_project
from ftth_hld.pipeline import ftth_project_payloads
from .serializers import ProjectSerializer


MICROSERVICE_BASE_URL = "https://fiber-import.zeabur.app"


class ProjectListCreateAPIView(APIView):
    """
    POST  /api/projects/        -> create project
    GET   /api/projects/        -> list projects (with filters)
    """

    def get(self, request):
        qs = Project.objects.all().order_by("-created_at")

        # Engineers only see projects assigned to them (project-scope jobs)
        # plus projects they created — "My Projects". Admins see everything.
        role = getattr(request.user, "role", None)
        if role == "ENGINEER":
            from assignments.models import AssignmentJob
            # Project-, layer- and feature-scope jobs all mean "this engineer
            # works on that project" — include every scope so My Projects
            # never hides a project the engineer is actually assigned to.
            assigned_ids = AssignmentJob.objects.filter(
                assignee=request.user,
            ).exclude(project__isnull=True).values_list("project_id", flat=True)
            qs = qs.filter(id__in=list(assigned_ids))

        # Filters
        status_filter = request.GET.get("status")
        region_filter = request.GET.get("region")

        if status_filter:
            qs = qs.filter(status=status_filter)

        if region_filter:
            qs = qs.filter(region__icontains=region_filter)

        # kind: 'survey' (default) | 'hld' | 'all' — which project kinds to
        # return. Survey-only by default so downstream consumers (job dropdowns,
        # approval queues) never see HLD run ids; the projects page opts into
        # 'all' for the unified view.
        kind = request.GET.get("kind", "survey").lower()

        survey = ProjectSerializer(qs, many=True).data
        for item in survey:
            item["type"] = "survey"
            item["kind"] = "survey"

        hld = []
        if kind in ("hld", "all"):
            # HLD pipeline runs (FtthProject). Runs have no region, so a region
            # filter excludes them; a status filter matches their own statuses
            # (queued / running / completed / failed).
            hld = ftth_project_payloads(limit=100)
            if status_filter:
                hld = [h for h in hld if h.get("status") == status_filter]
            if region_filter:
                hld = []
            for item in hld:
                item["type"] = "hld"
                item["kind"] = "hld"

        if kind == "hld":
            return Response(hld)

        merged = survey + hld
        merged.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return Response(merged)

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

    def delete(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        # Delete microservice layers first
        microservice_url = f"{MICROSERVICE_BASE_URL}/geo/projects/{project_id}/layers"
        try:
            response = requests.delete(microservice_url, timeout=60)
            if response.status_code not in (200, 202, 204, 404):
                response.raise_for_status()
        except requests.RequestException as exc:
            return Response(
                {"detail": f"Failed to delete project layers in microservice: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Clean up uploaded files from import sessions before cascade delete
        import_sessions = ImportSession.objects.filter(project=project)
        for session in import_sessions:
            if session.stored_file_path:
                try:
                    file_path = session.stored_file_path
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except (OSError, IOError):
                    pass  # Continue even if file deletion fails

        # All related models (Features, AssignmentJobs, ImportSessions) have
        # on_delete=CASCADE, so deleting the project will cascade delete everything
        project.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

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


class ProjectAcceptAPIView(APIView):
    """POST /api/projects/<uuid:project_id>/accept/

    Engineer accepts their assigned Survey copy → status flips
    ``assigned`` → ``active``. Only the assigned engineer (or a SUBADMIN)
    may accept.
    """

    def post(self, request, project_id):
        try:
            result = accept_survey_project(str(project_id), request.user)
        except ValueError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        except PermissionError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN
            )
        except Exception as exc:
            return Response(
                {"detail": f"Accept failed: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(result)



class ProjectSubmitAPIView(APIView):
    """POST /api/projects/<uuid:project_id>/submit/

    The engineer submits their finished survey → status flips
    ``active`` → ``submitted``. Only the assigned engineer (or a SUBADMIN)
    may submit.
    """

    def post(self, request, project_id):
        from ftth_hld.assign import submit_survey_project

        try:
            result = submit_survey_project(str(project_id), request.user)
        except ValueError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        except PermissionError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN
            )
        except Exception as exc:
            return Response(
                {"detail": f"Submit failed: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(result)


class ProjectReviewAPIView(APIView):
    """POST /api/projects/<uuid:project_id>/review/

    Admin review workflow on a submitted Survey copy. Body:
        {"action": "start_review" | "reviewed" | "accept" | "redo" | "complete"}

    Transitions:
      start_review: submitted -> under_review
      reviewed:     under_review -> reviewed
      accept:       reviewed (or under_review) -> accepted
      redo:         submitted | under_review | reviewed -> redo
      complete:     accepted -> completed
    Only SUBADMIN users may review.
    """

    def post(self, request, project_id):
        from ftth_hld.assign import review_survey_project

        action = (request.data or {}).get("action", "")
        try:
            result = review_survey_project(str(project_id), request.user, action)
        except ValueError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        except PermissionError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN
            )
        except Exception as exc:
            return Response(
                {"detail": f"Review failed: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(result)
