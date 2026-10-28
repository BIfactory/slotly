from __future__ import annotations

from django.contrib import admin

from .models import APIToken, MCPCall


@admin.register(APIToken)
class APITokenAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "name", "prefix", "created_at", "last_used_at", "revoked_at")
    readonly_fields = ("prefix", "created_at", "token_hash")
    fields = ("user", "name", "prefix", "scopes", "created_at", "last_used_at", "revoked_at")


@admin.register(MCPCall)
class MCPCallAdmin(admin.ModelAdmin):
    list_display = ("id", "token", "capability", "status", "duration_ms", "error_code", "created_at")
    list_filter = ("status", "capability")
    search_fields = ("token__prefix", "capability")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
