from __future__ import annotations

from django.apps import AppConfig


class McpConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mcp"
    label = "mcp"

    def ready(self):
        # Import tools module so all @register_tool decorators run at startup.
        from . import tools  # noqa: F401
