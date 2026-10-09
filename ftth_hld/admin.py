"""Django admin registrations for the FTTH HLD module."""

from django.contrib import admin

from .models import BoqRate, BoqSnapshot, FtthLayer, FtthProject


@admin.register(FtthProject)
class FtthProjectAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'project_id',
        'status',
        'progress',
        'stage_name',
        'created_at',
        'completed_at',
    )
    list_filter = ('status',)
    search_fields = ('name', 'project_id')
    readonly_fields = ('project_id', 'created_at', 'updated_at')


@admin.register(FtthLayer)
class FtthLayerAdmin(admin.ModelAdmin):
    list_display = ('name', 'ftth_project', 'feature_count', 'updated_at')
    search_fields = ('name', 'ftth_project__project_id')
    list_filter = ('name',)


@admin.register(BoqRate)
class BoqRateAdmin(admin.ModelAdmin):
    """Rate card — edit material/labour/rent prices per BOQ item."""

    list_display = (
        'item_code',
        'item_name',
        'unit',
        'section',
        'material_rate',
        'labour_rate',
        'rent_rate',
        'active',
    )
    list_filter = ('section', 'active')
    search_fields = ('item_code', 'item_name')
    list_editable = ('material_rate', 'labour_rate', 'rent_rate', 'active')
    ordering = ('item_code',)


@admin.register(BoqSnapshot)
class BoqSnapshotAdmin(admin.ModelAdmin):
    list_display = ('ftth_project', 'created_at', 'regenerated_at')
    readonly_fields = (
        'ftth_project',
        'boq_json',
        'bom_json',
        'boq_totals',
        'bom_totals',
        'created_at',
        'regenerated_at',
    )
