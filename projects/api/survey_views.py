from django.utils import timezone
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.models.project import Project
from projects.models.feature import Feature
from projects.models.survey_change import SurveyChange
from projects.models.approved_survey_version import ApprovedSurveyVersion
from .survey_serializers import (
    SurveyChangeSerializer,
    SurveyChangeCreateSerializer,
    SurveyChangeReviewSerializer,
    ApprovedSurveyVersionSerializer,
    ApprovedSurveyVersionDetailSerializer,
    CreateApprovedSurveyVersionSerializer,
)


class SurveyChangeListCreateView(generics.ListCreateAPIView):
    """
    List all survey changes for a project or create a new one.
    
    GET: List all survey changes with optional filters
    POST: Create a new survey change
    """
    permission_classes = [IsAuthenticated]
    
    def get_serializer_class(self):
        if self.request.method == 'POST':
            return SurveyChangeCreateSerializer
        return SurveyChangeSerializer
    
    def get_queryset(self):
        queryset = SurveyChange.objects.all()
        
        # Filter by project
        project_id = self.kwargs.get('project_id')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        
        # Filter by status
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        # Filter by change type
        change_type = self.request.query_params.get('change_type')
        if change_type:
            queryset = queryset.filter(change_type=change_type)
        
        # Filter by layer
        layer_name = self.request.query_params.get('layer_name')
        if layer_name:
            queryset = queryset.filter(layer_name=layer_name)
        
        return queryset
    
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class SurveyChangeDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    Retrieve, update, or delete a survey change.
    
    GET: Retrieve a survey change
    PUT/PATCH: Update a survey change
    DELETE: Delete a survey change
    """
    serializer_class = SurveyChangeSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = 'pk'
    
    def get_queryset(self):
        project_id = self.kwargs.get('project_id')
        if project_id:
            return SurveyChange.objects.filter(project_id=project_id)
        return SurveyChange.objects.all()


class SurveyChangeReviewView(APIView):
    """
    Review a survey change (approve, reject, or request correction).
    
    POST: Review a survey change
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request, project_id, change_id):
        try:
            change = SurveyChange.objects.get(id=change_id, project_id=project_id)
        except SurveyChange.DoesNotExist:
            return Response(
                {"detail": "Survey change not found"},
                status=status.HTTP_404_NOT_FOUND
            )
        
        serializer = SurveyChangeReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        action = serializer.validated_data['action']
        review_comments = serializer.validated_data.get('review_comments', '')
        
        # Update the change
        change.status = action
        change.reviewer = request.user
        change.review_comments = review_comments
        change.reviewed_at = timezone.now()
        change.save()
        
        # Return the updated change
        response_serializer = SurveyChangeSerializer(change)
        return Response(response_serializer.data, status=status.HTTP_200_OK)


class SurveyChangeBulkReviewView(APIView):
    """
    Bulk review multiple survey changes.
    
    POST: Review multiple survey changes at once
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request, project_id):
        change_ids = request.data.get('change_ids', [])
        action = request.data.get('action')
        review_comments = request.data.get('review_comments', '')
        
        if not change_ids:
            return Response(
                {"detail": "change_ids is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if action not in ['approved', 'rejected', 'needs_correction']:
            return Response(
                {"detail": "Invalid action"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get the changes
        changes = SurveyChange.objects.filter(
            id__in=change_ids,
            project_id=project_id
        )
        
        # Update each change
        updated_count = 0
        for change in changes:
            change.status = action
            change.reviewer = request.user
            change.review_comments = review_comments
            change.reviewed_at = timezone.now()
            change.save()
            updated_count += 1
        
        return Response({
            "detail": f"Updated {updated_count} survey changes",
            "updated_count": updated_count
        }, status=status.HTTP_200_OK)


class SurveyChangeStatsView(APIView):
    """
    Get statistics for survey changes in a project.
    
    GET: Get survey change statistics
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND
            )
        
        changes = SurveyChange.objects.filter(project=project)
        
        stats = {
            "total": changes.count(),
            "pending_review": changes.filter(status=SurveyChange.STATUS_PENDING).count(),
            "approved": changes.filter(status=SurveyChange.STATUS_APPROVED).count(),
            "rejected": changes.filter(status=SurveyChange.STATUS_REJECTED).count(),
            "needs_correction": changes.filter(status=SurveyChange.STATUS_NEEDS_CORRECTION).count(),
            "by_type": {
                "geometry": changes.filter(change_type=SurveyChange.CHANGE_TYPE_GEOMETRY).count(),
                "attribute": changes.filter(change_type=SurveyChange.CHANGE_TYPE_ATTRIBUTE).count(),
                "new_feature": changes.filter(change_type=SurveyChange.CHANGE_TYPE_NEW_FEATURE).count(),
                "removed_feature": changes.filter(change_type=SurveyChange.CHANGE_TYPE_REMOVED_FEATURE).count(),
            },
            "by_layer": {},
        }
        
        # Get counts by layer
        layer_names = changes.values_list('layer_name', flat=True).distinct()
        for layer_name in layer_names:
            stats["by_layer"][layer_name] = changes.filter(layer_name=layer_name).count()
        
        return Response(stats, status=status.HTTP_200_OK)


class SurveyChangeReadinessView(APIView):
    """
    Check if a project is ready for LLD generation.
    
    GET: Check LLD readiness
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND
            )
        
        changes = SurveyChange.objects.filter(project=project)
        
        pending = changes.filter(status=SurveyChange.STATUS_PENDING).count()
        needs_correction = changes.filter(status=SurveyChange.STATUS_NEEDS_CORRECTION).count()
        
        is_ready = (pending == 0 and needs_correction == 0)
        
        return Response({
            "is_ready": is_ready,
            "pending_review": pending,
            "needs_correction": needs_correction,
            "total_changes": changes.count(),
            "approved": changes.filter(status=SurveyChange.STATUS_APPROVED).count(),
            "rejected": changes.filter(status=SurveyChange.STATUS_REJECTED).count(),
        }, status=status.HTTP_200_OK)


class ApprovedSurveyVersionListCreateView(generics.ListCreateAPIView):
    """
    List all approved survey versions for a project or create a new one.
    
    GET: List all approved survey versions
    POST: Create a new approved survey version from approved changes
    """
    permission_classes = [IsAuthenticated]
    
    def get_serializer_class(self):
        if self.request.method == 'POST':
            return CreateApprovedSurveyVersionSerializer
        return ApprovedSurveyVersionSerializer
    
    def get_queryset(self):
        queryset = ApprovedSurveyVersion.objects.all()
        
        project_id = self.kwargs.get('project_id')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        
        return queryset
    
    def perform_create(self, serializer):
        project_id = self.kwargs.get('project_id')
        project = Project.objects.get(id=project_id)
        
        # Get the latest version number
        latest_version = ApprovedSurveyVersion.objects.filter(
            project=project
        ).order_by('-version_number').first()
        
        next_version = (latest_version.version_number + 1) if latest_version else 1
        
        # Get all approved changes
        approved_changes = SurveyChange.objects.filter(
            project=project,
            status=SurveyChange.STATUS_APPROVED
        )
        
        # Build the approved data snapshot
        approved_data = self._build_approved_data(project, approved_changes)
        
        # Create the version
        version = ApprovedSurveyVersion.objects.create(
            project=project,
            version_number=next_version,
            hld_project_id=str(project.source_ftth_project_id) or str(project.id),
            total_changes=SurveyChange.objects.filter(project=project).count(),
            approved_changes=approved_changes.count(),
            rejected_changes=SurveyChange.objects.filter(
                project=project,
                status=SurveyChange.STATUS_REJECTED
            ).count(),
            approved_data=approved_data,
            status=ApprovedSurveyVersion.STATUS_READY,
            created_by=self.request.user,
        )
        
        return version
    
    def _build_approved_data(self, project, approved_changes):
        """
        Build the approved dataset by applying approved changes to HLD baseline.
        
        Approved Survey = HLD + Approved Changes - Approved Removals
        """
        # Start with all features from the project
        features = Feature.objects.filter(project=project)
        
        # Build the feature map
        feature_map = {}
        for feature in features:
            feature_map[str(feature.id)] = {
                "id": str(feature.id),
                "layer_name": feature.layer_name,
                "layer_id": feature.layer_id,
                "properties": feature.properties,
                "geometry": feature.geometry,
                "status": feature.status,
            }
        
        # Apply approved changes
        removal_ids = set()
        for change in approved_changes:
            feature_id = str(change.original_feature_id) if change.original_feature_id else None
            
            if change.change_type == SurveyChange.CHANGE_TYPE_REMOVED_FEATURE:
                # Mark for removal
                if feature_id:
                    removal_ids.add(feature_id)
            
            elif change.change_type == SurveyChange.CHANGE_TYPE_GEOMETRY:
                # Update geometry
                if feature_id and feature_id in feature_map:
                    feature_map[feature_id]["geometry"] = change.proposed_geometry
            
            elif change.change_type == SurveyChange.CHANGE_TYPE_ATTRIBUTE:
                # Update attribute
                if feature_id and feature_id in feature_map:
                    if change.changed_field:
                        feature_map[feature_id]["properties"][change.changed_field] = change.proposed_value
            
            elif change.change_type == SurveyChange.CHANGE_TYPE_NEW_FEATURE:
                # Add new feature
                if change.proposed_feature_id:
                    new_feature = Feature.objects.filter(id=change.proposed_feature_id).first()
                    if new_feature:
                        feature_map[str(new_feature.id)] = {
                            "id": str(new_feature.id),
                            "layer_name": new_feature.layer_name,
                            "layer_id": new_feature.layer_id,
                            "properties": new_feature.properties,
                            "geometry": new_feature.geometry,
                            "status": new_feature.status,
                            "is_new": True,
                        }
        
        # Remove deleted features
        for feature_id in removal_ids:
            if feature_id in feature_map:
                del feature_map[feature_id]
        
        return {
            "features": list(feature_map.values()),
            "feature_count": len(feature_map),
            "applied_changes": approved_changes.count(),
            "removed_features": len(removal_ids),
        }


class ApprovedSurveyVersionDetailView(generics.RetrieveAPIView):
    """
    Retrieve an approved survey version with full details.
    
    GET: Retrieve an approved survey version
    """
    serializer_class = ApprovedSurveyVersionDetailSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = 'pk'
    
    def get_queryset(self):
        project_id = self.kwargs.get('project_id')
        if project_id:
            return ApprovedSurveyVersion.objects.filter(project_id=project_id)
        return ApprovedSurveyVersion.objects.all()


class ApprovedSurveyVersionLatestView(APIView):
    """
    Get the latest approved survey version for a project.
    
    GET: Get the latest approved survey version
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request, project_id):
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"detail": "Project not found"},
                status=status.HTTP_404_NOT_FOUND
            )
        
        version = ApprovedSurveyVersion.objects.filter(
            project=project
        ).order_by('-version_number').first()
        
        if not version:
            return Response(
                {"detail": "No approved survey versions found"},
                status=status.HTTP_404_NOT_FOUND
            )
        
        serializer = ApprovedSurveyVersionDetailSerializer(version)
        return Response(serializer.data, status=status.HTTP_200_OK)
