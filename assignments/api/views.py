from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Q, Count, Case, When, Value, CharField, IntegerField, OuterRef, Subquery

from assignments.models import AssignmentJob
from projects.models import Feature
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


class JobAssignmentsListAPIView(APIView):
    def get(self, request):
        # Get query parameters
        search = request.query_params.get("search", "").strip().lower()
        project_id = request.query_params.get("project")
        status_filter = request.query_params.get("status")
        engineer_id = request.query_params.get("engineer")
        scope_filter = request.query_params.get("scope")
        layer_id = request.query_params.get("layer")

        # Base queryset: features that have assignments
        queryset = Feature.objects.select_related("project").prefetch_related(
            "assignment_jobs", "assignment_jobs__assignee"
        )

        # Apply filters
        if project_id:
            queryset = queryset.filter(project_id=project_id)

        if layer_id:
            queryset = queryset.filter(layer_id=layer_id)

        if status_filter:
            queryset = queryset.filter(status=status_filter)

        if engineer_id:
            queryset = queryset.filter(assignment_jobs__assignee_id=engineer_id)

        # Search filter (job ID, project name, or engineer email)
        if search:
            queryset = queryset.filter(
                Q(assignment_jobs__id__icontains=search) |
                Q(project__name__icontains=search) |
                Q(assignment_jobs__assignee__email__icontains=search)
            )

        # Get all features with their assignments
        features = list(queryset.distinct())

        # Build job assignments list
        jobs = []
        seen_assignments = set()

        for feature in features:
            for job in feature.assignment_jobs.all():
                # For project/layer scope, group by assignment job
                if job.scope in [AssignmentJob.SCOPE_PROJECT, AssignmentJob.SCOPE_LAYER]:
                    if job.id in seen_assignments:
                        continue
                    seen_assignments.add(job.id)

                    # For aggregate scopes, get all related features
                    if job.scope == AssignmentJob.SCOPE_PROJECT:
                        related_features = Feature.objects.filter(
                            project=job.project
                        )
                    else:  # SCOPE_LAYER
                        related_features = Feature.objects.filter(
                            project=job.project,
                            layer_id=job.layer_id
                        )

                    # Calculate aggregate status
                    status_counts = related_features.values("status").annotate(
                        count=Count("id")
                    )
                    total_features = sum(s["count"] for s in status_counts)

                    # Determine aggregate status (priority: redo > under_review > approved > pending > assigned)
                    statuses = {s["status"]: s["count"] for s in status_counts}
                    agg_status = self._get_aggregate_status(statuses)

                    # Apply status filter to aggregate
                    if status_filter and agg_status != status_filter:
                        continue

                    jobs.append({
                        "id": str(job.id),
                        "project": {
                            "id": str(job.project.id),
                            "name": job.project.name,
                        },
                        "scope": job.scope,
                        "scope_display": job.get_scope_display(),
                        "assignee": {
                            "id": str(job.assignee.id),
                            "email": job.assignee.email,
                            "initials": job.assignee.email[0].upper() if job.assignee.email else "?",
                        },
                        "feature_count": total_features,
                        "status": agg_status,
                        "status_display": self._get_status_display(agg_status),
                        "created_at": job.created_at.isoformat(),
                    })
                else:
                    # Feature scope - each feature is its own job
                    jobs.append({
                        "id": str(job.id),
                        "project": {
                            "id": str(job.project.id),
                            "name": job.project.name,
                        },
                        "scope": job.scope,
                        "scope_display": job.get_scope_display(),
                        "assignee": {
                            "id": str(job.assignee.id),
                            "email": job.assignee.email,
                            "initials": job.assignee.email[0].upper() if job.assignee.email else "?",
                        },
                        "feature": {
                            "id": str(feature.id),
                            "status": feature.status,
                            "layer_id": feature.layer_id,
                            "layer_name": feature.layer_name,
                        },
                        "feature_count": 1,
                        "status": feature.status,
                        "status_display": self._get_status_display(feature.status),
                        "created_at": job.created_at.isoformat(),
                    })

        # Apply scope filter
        if scope_filter:
            jobs = [j for j in jobs if j["scope"] == scope_filter]

        # Calculate stats from all features (not filtered by status)
        all_features = Feature.objects.all()
        if project_id:
            all_features = all_features.filter(project_id=project_id)
        if engineer_id:
            all_features = all_features.filter(assignment_jobs__assignee_id=engineer_id)

        stats = all_features.aggregate(
            total=Count("id"),
            under_review=Count(Case(When(status=Feature.STATUS_UNDER_REVIEW, then=1), output_field=IntegerField())),
            approved=Count(Case(When(status=Feature.STATUS_APPROVED, then=1), output_field=IntegerField())),
            redo=Count(Case(When(status=Feature.STATUS_REDO, then=1), output_field=IntegerField())),
            pending=Count(Case(When(status=Feature.STATUS_PENDING, then=1), output_field=IntegerField())),
            assigned=Count(Case(When(status=Feature.STATUS_ASSIGNED, then=1), output_field=IntegerField())),
        )

        # Sort by created_at desc
        jobs.sort(key=lambda x: x["created_at"], reverse=True)

        # Pagination
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", 20))
        start = (page - 1) * page_size
        end = start + page_size
        paginated_jobs = jobs[start:end]

        return Response({
            "count": len(jobs),
            "page": page,
            "page_size": page_size,
            "results": paginated_jobs,
            "stats": {
                "total": stats["total"],
                "under_review": stats["under_review"],
                "approved": stats["approved"],
                "redo": stats["redo"],
                "pending": stats["pending"],
                "assigned": stats["assigned"],
            },
        })

    def _get_aggregate_status(self, statuses):
        """Determine aggregate status with priority: redo > under_review > approved > pending > assigned"""
        if statuses.get(Feature.STATUS_REDO, 0) > 0:
            return Feature.STATUS_REDO
        if statuses.get(Feature.STATUS_UNDER_REVIEW, 0) > 0:
            return Feature.STATUS_UNDER_REVIEW
        if statuses.get(Feature.STATUS_APPROVED, 0) > 0:
            return Feature.STATUS_APPROVED
        if statuses.get(Feature.STATUS_PENDING, 0) > 0:
            return Feature.STATUS_PENDING
        return Feature.STATUS_ASSIGNED

    def _get_status_display(self, status):
        """Get human-readable status label"""
        display_map = {
            Feature.STATUS_PENDING: "Pending",
            Feature.STATUS_ASSIGNED: "Assigned",
            Feature.STATUS_UNDER_REVIEW: "Under Review",
            Feature.STATUS_APPROVED: "Approved",
            Feature.STATUS_REDO: "Redo",
        }
        return display_map.get(status, status)
