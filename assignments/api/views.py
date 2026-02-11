from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from assignments.models import AssignmentJob
from assignments.api.serializers import AssignmentJobSerializer, AssignmentJobDetailSerializer


class AssignmentJobListCreateAPIView(generics.ListCreateAPIView):
    queryset = AssignmentJob.objects.select_related("project", "feature", "assignee")

    def get_serializer_class(self):
        if self.request.method == "GET":
            return AssignmentJobDetailSerializer
        return AssignmentJobSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        project_id = self.request.query_params.get("project")
        scope = self.request.query_params.get("scope")
        layer_id = self.request.query_params.get("layer_id")

        if project_id:
            qs = qs.filter(project_id=project_id)
        if scope:
            qs = qs.filter(scope=scope)
        if layer_id:
            qs = qs.filter(layer_id=layer_id)

        return qs.order_by("scope", "layer_id", "created_at")


class AssignmentJobDetailAPIView(generics.RetrieveUpdateDestroyAPIView):
    queryset = AssignmentJob.objects.select_related("project", "feature", "assignee")

    def get_serializer_class(self):
        if self.request.method == "GET":
            return AssignmentJobDetailSerializer
        return AssignmentJobSerializer


class AssignmentJobSummaryAPIView(APIView):
    def get(self, request):
        project_id = request.query_params.get("project")
        if not project_id:
            return Response(
                {"detail": "project query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        assignments = (
            AssignmentJob.objects.select_related("project", "feature", "assignee")
            .filter(project_id=project_id)
            .order_by("scope", "layer_id", "created_at")
        )

        serializer = AssignmentJobDetailSerializer(assignments, many=True)
        grouped = {"project": [], "layer": [], "feature": []}
        for record in serializer.data:
            grouped[record["scope"]].append(record)

        return Response(
            {
                "project_id": project_id,
                "counts": {k: len(v) for k, v in grouped.items()},
                "assignments": grouped,
            }
        )
