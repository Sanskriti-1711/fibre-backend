from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Q, Count, Case, When, Value, CharField, IntegerField, OuterRef, Subquery
from django.utils import timezone
from datetime import timedelta

from assignments.models import AssignmentJob
from projects.models import Feature, Project
from assignments.api.serializers import AssignmentJobSerializer, AssignmentJobDetailSerializer
from users.models import User


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
        assignee_id = self.request.query_params.get("assignee")

        if project_id:
            qs = qs.filter(project_id=project_id)
        if scope:
            qs = qs.filter(scope=scope)
        if layer_id:
            qs = qs.filter(layer_id=layer_id)
        if assignee_id:
            qs = qs.filter(assignee_id=assignee_id)

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


class EngineerActivityAPIView(APIView):
    """Returns timeline of engineer activity including assignments and status changes."""

    def get(self, request):
        engineer_id = request.query_params.get("engineer")
        days = int(request.query_params.get("days", 30))

        if not engineer_id:
            return Response(
                {"detail": "engineer query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        since = timezone.now() - timedelta(days=days)

        # Get recent assignments for this engineer
        assignments = AssignmentJob.objects.select_related(
            "project", "feature"
        ).filter(
            assignee_id=engineer_id,
            created_at__gte=since
        ).order_by("-created_at")

        # Get features assigned to this engineer with recent updates
        features = Feature.objects.select_related("project").prefetch_related(
            "assignment_jobs"
        ).filter(
            assignment_jobs__assignee_id=engineer_id,
            updated_at__gte=since
        ).order_by("-updated_at")

        # Build activity timeline
        activities = []

        # Assignment events
        for job in assignments[:50]:
            activities.append({
                "type": "assignment",
                "timestamp": job.created_at.isoformat(),
                "project": {
                    "id": str(job.project.id),
                    "name": job.project.name,
                },
                "scope": job.scope,
                "scope_display": job.get_scope_display(),
                "target": self._get_assignment_target(job),
            })

        # Feature update events (status changes)
        seen_features = set()
        for feature in features[:50]:
            if feature.id in seen_features:
                continue
            seen_features.add(feature.id)

            activities.append({
                "type": "feature_update",
                "timestamp": feature.updated_at.isoformat(),
                "feature": {
                    "id": str(feature.id),
                    "layer_name": feature.layer_name,
                    "status": feature.status,
                    "status_display": self._get_status_display(feature.status),
                },
                "project": {
                    "id": str(feature.project.id),
                    "name": feature.project.name,
                },
            })

        # Sort by timestamp desc
        activities.sort(key=lambda x: x["timestamp"], reverse=True)

        return Response({
            "engineer_id": engineer_id,
            "period_days": days,
            "activities": activities[:100],
            "total_count": len(activities),
        })

    def _get_assignment_target(self, job):
        if job.scope == AssignmentJob.SCOPE_PROJECT:
            return {"type": "project", "name": job.project.name}
        elif job.scope == AssignmentJob.SCOPE_LAYER:
            return {"type": "layer", "layer_id": job.layer_id}
        else:
            return {"type": "feature", "id": str(job.feature.id) if job.feature else None}

    def _get_status_display(self, status):
        display_map = {
            Feature.STATUS_PENDING: "Pending",
            Feature.STATUS_ASSIGNED: "Assigned",
            Feature.STATUS_UNDER_REVIEW: "Under Review",
            Feature.STATUS_APPROVED: "Approved",
            Feature.STATUS_REDO: "Redo",
        }
        return display_map.get(status, status)


class EngineerStatsAPIView(APIView):
    """Returns performance statistics for an engineer."""

    def get(self, request):
        engineer_id = request.query_params.get("engineer")
        days = int(request.query_params.get("days", 30))

        if not engineer_id:
            return Response(
                {"detail": "engineer query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        since = timezone.now() - timedelta(days=days)

        # Get features assigned to this engineer
        features = Feature.objects.filter(
            assignment_jobs__assignee_id=engineer_id
        )

        # Overall stats
        overall = features.aggregate(
            total=Count("id"),
            approved=Count(Case(When(status=Feature.STATUS_APPROVED, then=1), output_field=IntegerField())),
            under_review=Count(Case(When(status=Feature.STATUS_UNDER_REVIEW, then=1), output_field=IntegerField())),
            redo=Count(Case(When(status=Feature.STATUS_REDO, then=1), output_field=IntegerField())),
            assigned=Count(Case(When(status=Feature.STATUS_ASSIGNED, then=1), output_field=IntegerField())),
            pending=Count(Case(When(status=Feature.STATUS_PENDING, then=1), output_field=IntegerField())),
        )

        # Recent activity (last N days)
        recent_features = features.filter(updated_at__gte=since)
        recent = recent_features.aggregate(
            total=Count("id"),
            approved=Count(Case(When(status=Feature.STATUS_APPROVED, then=1), output_field=IntegerField())),
        )

        # Daily breakdown for the period
        daily_stats = []
        for day_offset in range(days):
            day_start = since + timedelta(days=day_offset)
            day_end = day_start + timedelta(days=1)

            day_features = recent_features.filter(
                updated_at__gte=day_start,
                updated_at__lt=day_end
            )

            day_stat = day_features.aggregate(
                updated=Count("id"),
                approved=Count(Case(When(status=Feature.STATUS_APPROVED, then=1), output_field=IntegerField())),
            )

            daily_stats.append({
                "date": day_start.date().isoformat(),
                "updated": day_stat["updated"],
                "approved": day_stat["approved"],
            })

        # Project breakdown
        project_stats = features.values(
            "project__id", "project__name"
        ).annotate(
            total=Count("id"),
            approved=Count(Case(When(status=Feature.STATUS_APPROVED, then=1), output_field=IntegerField())),
            under_review=Count(Case(When(status=Feature.STATUS_UNDER_REVIEW, then=1), output_field=IntegerField())),
            redo=Count(Case(When(status=Feature.STATUS_REDO, then=1), output_field=IntegerField())),
        ).order_by("-total")

        return Response({
            "engineer_id": engineer_id,
            "period_days": days,
            "overall": {
                "total": overall["total"],
                "approved": overall["approved"],
                "under_review": overall["under_review"],
                "redo": overall["redo"],
                "assigned": overall["assigned"],
                "pending": overall["pending"],
                "approval_rate": round(
                    (overall["approved"] / overall["total"] * 100), 2
                ) if overall["total"] > 0 else 0,
            },
            "recent_period": {
                "total": recent["total"],
                "approved": recent["approved"],
            },
            "daily_breakdown": daily_stats,
            "project_breakdown": [
                {
                    "project": {
                        "id": str(p["project__id"]),
                        "name": p["project__name"],
                    },
                    "total": p["total"],
                    "approved": p["approved"],
                    "under_review": p["under_review"],
                    "redo": p["redo"],
                }
                for p in project_stats
            ],
        })


class FeatureFieldMeasurementsAPIView(APIView):
    """Update field measurements for a feature."""

    def patch(self, request, pk):
        try:
            feature = Feature.objects.get(pk=pk)
        except Feature.DoesNotExist:
            return Response(
                {"detail": "Feature not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        field_measurements = request.data.get("field_measurements")
        comparison_notes = request.data.get("comparison_notes")

        if field_measurements is not None:
            if not isinstance(field_measurements, dict):
                return Response(
                    {"detail": "field_measurements must be a JSON object"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            feature.field_measurements = field_measurements

        if comparison_notes is not None:
            feature.comparison_notes = comparison_notes

        feature.save()

        return Response({
            "id": str(feature.id),
            "field_measurements": feature.field_measurements,
            "comparison_notes": feature.comparison_notes,
            "updated_at": feature.updated_at.isoformat(),
        })


class FeatureSubmitAPIView(APIView):
    """Submit feature(s) for review."""

    def post(self, request):
        feature_ids = request.data.get("feature_ids", [])
        engineer_id = request.data.get("engineer")

        if not feature_ids:
            return Response(
                {"detail": "feature_ids is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not engineer_id:
            return Response(
                {"detail": "engineer is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Verify engineer has access to these features
        features = Feature.objects.filter(
            id__in=feature_ids,
            assignment_jobs__assignee_id=engineer_id
        )

        if features.count() != len(feature_ids):
            found_ids = set(str(f.id) for f in features)
            missing = set(feature_ids) - found_ids
            return Response(
                {"detail": f"Features not found or not assigned: {missing}"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Only allow submission from certain statuses
        valid_statuses = [Feature.STATUS_ASSIGNED, Feature.STATUS_REDO]
        invalid_features = [
            {"id": str(f.id), "status": f.status}
            for f in features
            if f.status not in valid_statuses
        ]

        if invalid_features:
            return Response(
                {
                    "detail": "Some features cannot be submitted",
                    "invalid_features": invalid_features,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Update status to under_review
        updated_count = features.filter(
            status__in=valid_statuses
        ).update(status=Feature.STATUS_UNDER_REVIEW)

        return Response({
            "submitted_count": updated_count,
            "feature_ids": [str(f.id) for f in features],
            "new_status": Feature.STATUS_UNDER_REVIEW,
            "status_display": "Under Review",
        })
