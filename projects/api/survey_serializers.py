from rest_framework import serializers

from projects.models.survey_change import SurveyChange
from projects.models.approved_survey_version import ApprovedSurveyVersion


class SurveyChangeSerializer(serializers.ModelSerializer):
    """Serializer for SurveyChange model."""
    
    created_by_email = serializers.CharField(source='created_by.email', read_only=True)
    reviewer_email = serializers.CharField(source='reviewer.email', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    change_type_display = serializers.CharField(source='get_change_type_display', read_only=True)
    
    class Meta:
        model = SurveyChange
        fields = [
            "id",
            "project",
            "original_feature",
            "proposed_feature",
            "change_type",
            "change_type_display",
            "layer_name",
            "feature_id_display",
            "original_geometry",
            "original_attributes",
            "proposed_geometry",
            "proposed_attributes",
            "changed_field",
            "original_value",
            "proposed_value",
            "reason",
            "engineer_notes",
            "status",
            "status_display",
            "reviewer",
            "reviewer_email",
            "review_comments",
            "reviewed_at",
            "created_by",
            "created_by_email",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
        ]


class SurveyChangeCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating new SurveyChange."""
    
    class Meta:
        model = SurveyChange
        fields = [
            "project",
            "original_feature",
            "change_type",
            "layer_name",
            "feature_id_display",
            "original_geometry",
            "original_attributes",
            "proposed_geometry",
            "proposed_attributes",
            "changed_field",
            "original_value",
            "proposed_value",
            "reason",
            "engineer_notes",
        ]
    
    def validate(self, data):
        change_type = data.get('change_type')
        
        if change_type == SurveyChange.CHANGE_TYPE_GEOMETRY:
            if not data.get('proposed_geometry'):
                raise serializers.ValidationError(
                    "Proposed geometry is required for geometry changes"
                )
        
        elif change_type == SurveyChange.CHANGE_TYPE_ATTRIBUTE:
            if not data.get('changed_field'):
                raise serializers.ValidationError(
                    "Changed field is required for attribute changes"
                )
        
        elif change_type == SurveyChange.CHANGE_TYPE_NEW_FEATURE:
            if not data.get('proposed_geometry'):
                raise serializers.ValidationError(
                    "Proposed geometry is required for new features"
                )
        
        elif change_type == SurveyChange.CHANGE_TYPE_REMOVED_FEATURE:
            if not data.get('original_feature'):
                raise serializers.ValidationError(
                    "Original feature is required for removal changes"
                )
        
        return data


class SurveyChangeReviewSerializer(serializers.Serializer):
    """Serializer for reviewing SurveyChange (approve/reject/correct)."""
    
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_NEEDS_CORRECTION = "needs_correction"
    
    action = serializers.ChoiceField(
        choices=[
            (STATUS_APPROVED, "Approve"),
            (STATUS_REJECTED, "Reject"),
            (STATUS_NEEDS_CORRECTION, "Request Correction"),
        ]
    )
    review_comments = serializers.CharField(required=False, allow_blank=True)
    
    def validate_action(self, value):
        valid_actions = [self.STATUS_APPROVED, self.STATUS_REJECTED, self.STATUS_NEEDS_CORRECTION]
        if value not in valid_actions:
            raise serializers.ValidationError(f"Invalid action: {value}")
        return value


class ApprovedSurveyVersionSerializer(serializers.ModelSerializer):
    """Serializer for ApprovedSurveyVersion model."""
    
    created_by_email = serializers.CharField(source='created_by.email', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = ApprovedSurveyVersion
        fields = [
            "id",
            "project",
            "version_number",
            "hld_project_id",
            "total_changes",
            "approved_changes",
            "rejected_changes",
            "status",
            "status_display",
            "created_by",
            "created_by_email",
            "created_at",
            "lld_run_id",
            "lld_run_at",
        ]
        read_only_fields = [
            "id",
            "version_number",
            "created_at",
        ]


class ApprovedSurveyVersionDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer including the approved data snapshot."""
    
    created_by_email = serializers.CharField(source='created_by.email', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = ApprovedSurveyVersion
        fields = [
            "id",
            "project",
            "version_number",
            "hld_project_id",
            "total_changes",
            "approved_changes",
            "rejected_changes",
            "approved_data",
            "status",
            "status_display",
            "created_by",
            "created_by_email",
            "created_at",
            "lld_run_id",
            "lld_run_at",
        ]
        read_only_fields = [
            "id",
            "version_number",
            "approved_data",
            "created_at",
        ]


class CreateApprovedSurveyVersionSerializer(serializers.Serializer):
    """Serializer for creating a new ApprovedSurveyVersion from approved changes."""
    
    def validate(self, data):
        # This will be validated in the view
        return data
