from django.contrib import admin

from .models import ApprovedSurveyVersion, LldLayer, LldRun


@admin.register(ApprovedSurveyVersion)
class ApprovedSurveyVersionAdmin(admin.ModelAdmin):
    list_display = ('version', 'ftth_project', 'hld_version', 'created_by', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('version', 'ftth_project_id')


@admin.register(LldRun)
class LldRunAdmin(admin.ModelAdmin):
    list_display = (
        'lld_version',
        'ftth_project',
        'approved_survey_version',
        'status',
        'progress',
        'run_by',
        'run_date',
    )
    list_filter = ('status', 'run_date')
    search_fields = ('lld_version', 'ftth_project_id')


@admin.register(LldLayer)
class LldLayerAdmin(admin.ModelAdmin):
    list_display = ('name', 'lld_run', 'feature_count', 'updated_at')
    search_fields = ('name', 'lld_run__lld_version')
