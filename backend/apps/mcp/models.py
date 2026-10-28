from __future__ import annotations

import hashlib
import secrets

from django.conf import settings
from django.db import models


class APIToken(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="api_tokens",
    )
    name = models.CharField(max_length=100)
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    prefix = models.CharField(max_length=12, unique=True, db_index=True)
    scopes = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("user", "revoked_at")),
        ]

    def __str__(self) -> str:
        return f"{self.prefix} ({self.name})"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    @classmethod
    def create_for_user(cls, user, name: str, scopes: list[str]) -> tuple[APIToken, str]:
        raw = f"sltly_{secrets.token_urlsafe(24)}"
        token = cls.objects.create(
            user=user,
            name=name,
            scopes=list(scopes),
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            prefix=raw[:12],
        )
        return token, raw


class MCPCall(models.Model):
    token = models.ForeignKey(
        APIToken,
        on_delete=models.CASCADE,
        related_name="calls",
    )
    capability = models.CharField(max_length=100)
    status = models.CharField(max_length=20)
    duration_ms = models.IntegerField()
    error_code = models.CharField(max_length=50, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("token", "-created_at")),
        ]

    def __str__(self) -> str:
        return f"MCPCall({self.capability}, {self.status})"
