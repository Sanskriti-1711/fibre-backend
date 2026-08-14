from django.contrib import admin

from .models import (
    PermitAuthority,
    PermitDocument,
    PermitEvent,
    PermitMatrix,
    PermitRule,
)


@admin.register(PermitAuthority)
class PermitAuthorityAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "authority_type", "country", "region", "active")
    list_filter = ("authority_type", "country", "active")
    search_fields = ("code", "name")


@admin.register(PermitRule)
class PermitRuleAdmin(admin.ModelAdmin):
    list_display = ("rule_id", "name", "layer_a", "layer_b", "required_level", "active")
    list_filter = ("active", "required_level")
    search_fields = ("rule_id", "name")


@admin.register(PermitMatrix)
class PermitMatrixAdmin(admin.ModelAdmin):
    list_display = (
        "permit_type", "project", "route_section", "authority",
        "status", "readiness_pct", "required", "updated_at",
    )
    list_filter = ("status", "permit_type", "required")
    search_fields = ("project_id", "route_section", "permit_type")


@admin.register(PermitDocument)
class PermitDocumentAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "permit", "version", "created_at")
    list_filter = ("kind",)


@admin.register(PermitEvent)
class PermitEventAdmin(admin.ModelAdmin):
    list_display = ("event", "permit", "created_at")
    list_filter = ("event",)
