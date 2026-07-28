"""Admin configuration for the survey app."""

from django.contrib import admin
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


class GPSPointInline(admin.TabularInline):
    model = GPSPoint
    extra = 0
    readonly_fields = ['latitude', 'longitude', 'altitude', 'accuracy', 'timestamp']


@admin.register(GPSTrace)
class GPSTraceAdmin(admin.ModelAdmin):
    list_display = ['id', 'engineer', 'started_at', 'ended_at', 'total_distance_m', 'point_count']
    list_filter = ['started_at']
    search_fields = ['id', 'engineer__email']
    inlines = [GPSPointInline]


@admin.register(TrenchSurvey)
class TrenchSurveyAdmin(admin.ModelAdmin):
    list_display = ['id', 'engineer', 'feature', 'trench_type', 'construction_method', 'created_at']
    list_filter = ['trench_type', 'construction_method', 'surface_type']
    search_fields = ['id', 'engineer__email', 'feature__id']


@admin.register(ExistingAsset)
class ExistingAssetAdmin(admin.ModelAdmin):
    list_display = ['id', 'engineer', 'asset_type', 'condition', 'latitude', 'longitude']
    list_filter = ['asset_type', 'condition']


@admin.register(RiskAssessment)
class RiskAssessmentAdmin(admin.ModelAdmin):
    list_display = ['id', 'engineer', 'category', 'severity', 'probability', 'status']
    list_filter = ['category', 'severity', 'probability', 'status']


@admin.register(Hazard)
class HazardAdmin(admin.ModelAdmin):
    list_display = ['id', 'engineer', 'hazard_type', 'mitigation_template', 'is_active']
    list_filter = ['hazard_type', 'is_active']


@admin.register(FieldEvidence)
class FieldEvidenceAdmin(admin.ModelAdmin):
    list_display = ['id', 'engineer', 'evidence_type', 'captured_at']
    list_filter = ['evidence_type']


@admin.register(SurveyChange)
class SurveyChangeAdmin(admin.ModelAdmin):
    list_display = ['id', 'engineer', 'feature', 'field_name', 'created_at']
    search_fields = ['field_name', 'feature__id']


@admin.register(SurveyStatus)
class SurveyStatusAdmin(admin.ModelAdmin):
    list_display = ['id', 'engineer', 'feature', 'status', 'updated_at']
    list_filter = ['status']


@admin.register(SyncQueueItem)
class SyncQueueItemAdmin(admin.ModelAdmin):
    list_display = ['id', 'engineer', 'item_type', 'status', 'retry_count', 'created_at']
    list_filter = ['item_type', 'status']


@admin.register(SurveyFeature)
class SurveyFeatureAdmin(admin.ModelAdmin):
    list_display = ['id', 'engineer', 'project', 'layer_id', 'survey_status', 'sync_status', 'version_number', 'updated_at']
    list_filter = ['survey_status', 'sync_status', 'layer_id']
    search_fields = ['id', 'layer_id', 'layer_name', 'original_hld_feature__id']
    readonly_fields = ['created_at', 'updated_at', 'original_geometry', 'original_attributes']
    raw_id_fields = ['original_hld_feature', 'project', 'engineer']
