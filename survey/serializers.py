"""Serializers for the survey app."""

from django.core.exceptions import ValidationError
from rest_framework import serializers

from projects.models import Feature

from .models import (
    ExistingAsset,
    FieldEvidence,
    GPSPoint,
    GPSTrace,
    Hazard,
    RiskAssessment,
    SurveyEditLog,
    SurveyFeature,
    SurveyStatus,
    SyncQueueItem,
    TrenchSurvey,
)


def route_feature_ref(raw):
    """Map a posted ``feature`` id onto the column that owns it.

    Returns ``{'feature': raw}`` for a project feature, ``{'survey_feature':
    raw}`` for a survey feature, ``{}`` for nothing, and ``{'feature': raw}``
    for an id in neither table so normal field validation reports it.
    """
    if not raw:
        return {}
    for model, field_name in ((Feature, 'feature'), (SurveyFeature, 'survey_feature')):
        try:
            if model.objects.filter(pk=raw).exists():
                return {field_name: raw}
        except (ValidationError, ValueError, TypeError):
            continue
    return {'feature': raw}


class FeatureRefSerializerMixin:
    """Accept a ``feature`` id from either side of the HLD/survey split.

    The app posts whichever id it is holding. An imported package feature has a
    ``projects.Feature`` uuid, but a feature the engineer creates on site exists
    only as a ``survey.SurveyFeature`` row. Posting that id to a
    ``projects.Feature`` foreign key was rejected with ``Invalid pk ... - object
    does not exist`` (HTTP 400), so every hazard, risk, trench classification,
    existing-asset check and evidence record written against a newly added
    feature was refused -- the app could only show a generic failure toast.

    On write the id is routed to the column that owns it. On read ``feature``
    echoes back whichever id was stored, so the app's
    ``record.feature === featureId`` look-ups keep working on both sides.
    """

    @staticmethod
    def _pk_exists(model, raw):
        try:
            return model.objects.filter(pk=raw).exists()
        except (ValidationError, ValueError, TypeError):
            return False

    @classmethod
    def route_feature_id(cls, raw):
        """See :func:`route_feature_ref`; kept on the class for callers that
        already hold a serializer (views doing their own create-or-update)."""
        return route_feature_ref(raw)

    def to_internal_value(self, data):
        if hasattr(data, 'get') and data.get('feature'):
            routed = route_feature_ref(data['feature'])
            if 'survey_feature' in routed:
                data = data.copy() if hasattr(data, 'copy') else dict(data)
                data['feature'] = None
                data['survey_feature'] = routed['survey_feature']
        # Mixin super() resolves through the ModelSerializer MRO at runtime;
        # mypy cannot see past the plain-object mixin base.
        return super().to_internal_value(data)  # type: ignore[misc]

    def to_representation(self, instance):
        # Same MRO note as above: serializers.Serializer provides this.
        data = super().to_representation(instance)  # type: ignore[misc]
        if instance.survey_feature_id and not instance.feature_id:
            data['feature'] = str(instance.survey_feature_id)
        return data


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
            'id',
            'engineer',
            'project',
            'started_at',
            'ended_at',
            'total_distance_m',
            'point_count',
            'points',
        ]
        read_only_fields = ['id', 'engineer']


# ── Trench ─────────────────────────────────────────────────────────────────


class TrenchSurveySerializer(FeatureRefSerializerMixin, serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = TrenchSurvey
        fields = [
            'id',
            'engineer',
            'engineer_name',
            'feature',
            'survey_feature',
            'trench_type',
            'construction_method',
            'depth_mm',
            'width_mm',
            'surface_type',
            'road_crossing',
            'footpath_crossing',
            'rail_crossing',
            'river_crossing',
            'private_property',
            'traffic_sensitive',
            'permit_required',
            'notes',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'engineer']


class ExistingAssetSerializer(FeatureRefSerializerMixin, serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = ExistingAsset
        fields = [
            'id',
            'engineer',
            'engineer_name',
            'feature',
            'survey_feature',
            'asset_type',
            'condition',
            'latitude',
            'longitude',
            'description',
            'created_at',
        ]
        read_only_fields = ['id', 'engineer']


# ── CRM / Risk ─────────────────────────────────────────────────────────────


class RiskAssessmentSerializer(FeatureRefSerializerMixin, serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = RiskAssessment
        fields = [
            'id',
            'engineer',
            'engineer_name',
            'feature',
            'survey_feature',
            'trench_survey',
            'category',
            'severity',
            'probability',
            'mitigation',
            'notes',
            'status',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'engineer']


class HazardSerializer(FeatureRefSerializerMixin, serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = Hazard
        fields = [
            'id',
            'engineer',
            'engineer_name',
            'feature',
            'survey_feature',
            'hazard_type',
            'mitigation_template',
            'notes',
            'is_active',
            'created_at',
        ]
        read_only_fields = ['id', 'engineer']


# ── Evidence ───────────────────────────────────────────────────────────────


class FieldEvidenceSerializer(FeatureRefSerializerMixin, serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = FieldEvidence
        fields = [
            'id',
            'engineer',
            'engineer_name',
            'feature',
            'survey_feature',
            'evidence_type',
            'file',
            'description',
            'latitude',
            'longitude',
            'weather',
            'captured_at',
            'created_at',
        ]
        read_only_fields = ['id', 'engineer']


# ── Survey Changes ─────────────────────────────────────────────────────────


class SurveyEditLogSerializer(serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = SurveyEditLog
        fields = [
            'id',
            'engineer',
            'engineer_name',
            'feature',
            'field_name',
            'old_value',
            'new_value',
            'reason',
            'latitude',
            'longitude',
            'created_at',
        ]
        read_only_fields = ['id', 'engineer']


class SurveyStatusSerializer(FeatureRefSerializerMixin, serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = SurveyStatus
        fields = [
            'id',
            'engineer',
            'engineer_name',
            'feature',
            'survey_feature',
            'status',
            'notes',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'engineer']


# ── Sync Queue ─────────────────────────────────────────────────────────────


class SyncQueueItemSerializer(serializers.ModelSerializer):
    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)

    class Meta:
        model = SyncQueueItem
        fields = [
            'id',
            'engineer',
            'engineer_name',
            'item_type',
            'entity_id',
            'payload',
            'status',
            'retry_count',
            'error_message',
            'created_at',
            'synced_at',
        ]
        read_only_fields = ['id', 'engineer', 'synced_at']


# ── Survey Feature (HLD/Survey Separation) ────────────────────────────────


class SurveyFeatureSerializer(serializers.ModelSerializer):
    """Serializer for the SurveyFeature model."""

    engineer_name = serializers.CharField(source='engineer.full_name', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    hld_feature_id = serializers.UUIDField(source='original_hld_feature_id', read_only=True)
    photo_url = serializers.SerializerMethodField()

    def get_photo_url(self, obj):
        if obj.photo:
            request = self.context.get('request')
            if request is not None:
                return request.build_absolute_uri(obj.photo.url)
            return obj.photo.url
        return None

    class Meta:
        model = SurveyFeature
        fields = [
            'id',
            'original_hld_feature',
            'hld_feature_id',
            'project',
            'project_name',
            'engineer',
            'engineer_name',
            'layer_id',
            'layer_name',
            'original_geometry',
            'original_attributes',
            'survey_geometry',
            'survey_attributes',
            'photo_url',
            'photo_tags',
            'gps_accuracy_m',
            'gps_quality',
            'survey_status',
            'version_number',
            'sync_status',
            'change_reason',
            'review_notes',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'engineer', 'created_at', 'updated_at']
