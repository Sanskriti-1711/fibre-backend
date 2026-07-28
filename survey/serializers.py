"""Serializers for the survey app."""

from rest_framework import serializers

from .models import (
    GPSTrace,
    GPSPoint,
    TrenchSurvey,
    ExistingAsset,
    RiskAssessment,
    Hazard,
    FieldEvidence,
    SurveyChange,
    SurveyStatus,
    SyncQueueItem,
    SurveyFeature,
)


# ── GPS ────────────────────────────────────────────────────────────────────

class GPSPointSerializer(serializers.ModelSerializer):
    class Meta:
        model = GPSPoint
        fields = ['id', 'latitude', 'longitude', 'altitude', 'accuracy', 'timestamp', 'order']


class GPSTraceSerializer(serializers.ModelSerializer):
    points = GPSPointSerializer(many=True, read_only=True)

    class Meta:
        model = GPSTrace
        fields = [
            'id', 'engineer', 'project', 'started_at', 'ended_at',
            'total_distance_m', 'point_count', 'points',
        ]
        read_only_fields = ['id', 'engineer']


# ── Trench ─────────────────────────────────────────────────────────────────

class TrenchSurveySerializer(serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = TrenchSurvey
        fields = [
            'id', 'engineer', 'engineer_name', 'feature',
            'trench_type', 'construction_method', 'depth_mm', 'width_mm',
            'surface_type', 'road_crossing', 'footpath_crossing', 'rail_crossing',
            'river_crossing', 'private_property', 'traffic_sensitive',
            'permit_required', 'notes', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'engineer']


class ExistingAssetSerializer(serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = ExistingAsset
        fields = [
            'id', 'engineer', 'engineer_name', 'feature',
            'asset_type', 'condition', 'latitude', 'longitude',
            'description', 'created_at',
        ]
        read_only_fields = ['id', 'engineer']


# ── CRM / Risk ─────────────────────────────────────────────────────────────

class RiskAssessmentSerializer(serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = RiskAssessment
        fields = [
            'id', 'engineer', 'engineer_name', 'feature', 'trench_survey',
            'category', 'severity', 'probability', 'mitigation',
            'notes', 'status', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'engineer']


class HazardSerializer(serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = Hazard
        fields = [
            'id', 'engineer', 'engineer_name', 'feature',
            'hazard_type', 'mitigation_template', 'notes',
            'is_active', 'created_at',
        ]
        read_only_fields = ['id', 'engineer']


# ── Evidence ───────────────────────────────────────────────────────────────

class FieldEvidenceSerializer(serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = FieldEvidence
        fields = [
            'id', 'engineer', 'engineer_name', 'feature',
            'evidence_type', 'file', 'description',
            'latitude', 'longitude', 'weather', 'captured_at', 'created_at',
        ]
        read_only_fields = ['id', 'engineer']


# ── Survey Changes ─────────────────────────────────────────────────────────

class SurveyChangeSerializer(serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = SurveyChange
        fields = [
            'id', 'engineer', 'engineer_name', 'feature',
            'field_name', 'old_value', 'new_value',
            'reason', 'latitude', 'longitude', 'created_at',
        ]
        read_only_fields = ['id', 'engineer']


class SurveyStatusSerializer(serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = SurveyStatus
        fields = [
            'id', 'engineer', 'engineer_name', 'feature',
            'status', 'notes', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'engineer']


# ── Sync Queue ─────────────────────────────────────────────────────────────

class SyncQueueItemSerializer(serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = SyncQueueItem
        fields = [
            'id', 'engineer', 'engineer_name',
            'item_type', 'entity_id', 'payload',
            'status', 'retry_count', 'error_message',
            'created_at', 'synced_at',
        ]
        read_only_fields = ['id', 'engineer', 'synced_at']


# ── Survey Feature (HLD/Survey Separation) ────────────────────────────────

class SurveyFeatureSerializer(serializers.ModelSerializer):
    """Serializer for the SurveyFeature model."""
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    hld_feature_id = serializers.UUIDField(source='original_hld_feature_id', read_only=True)

    class Meta:
        model = SurveyFeature
        fields = [
            'id',
            'original_hld_feature', 'hld_feature_id',
            'project', 'project_name',
            'engineer', 'engineer_name',
            'layer_id', 'layer_name',
            'original_geometry', 'original_attributes',
            'survey_geometry', 'survey_attributes',
            'survey_status', 'version_number',
            'sync_status', 'change_reason',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'engineer', 'created_at', 'updated_at']
