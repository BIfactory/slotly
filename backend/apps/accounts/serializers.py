from __future__ import annotations

import re
from datetime import time

from rest_framework import serializers

from .models import WEEKDAYS, User

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _parse_hhmm(value: str) -> time:
    if not isinstance(value, str) or not _TIME_PATTERN.match(value):
        raise serializers.ValidationError(f"Expected HH:MM time, got {value!r}")
    h, m = value.split(":")
    return time(int(h), int(m))


def _validate_day(day_name: str, payload: object) -> dict[str, str | bool]:
    if not isinstance(payload, dict):
        raise serializers.ValidationError({day_name: "Must be an object"})
    extra = set(payload.keys()) - {"start", "end", "available"}
    if extra:
        raise serializers.ValidationError({day_name: f"Unknown keys: {sorted(extra)}"})
    start = _parse_hhmm(payload.get("start", ""))
    end = _parse_hhmm(payload.get("end", ""))
    if end <= start:
        raise serializers.ValidationError({day_name: "`end` must be after `start`"})
    available = payload.get("available", True)
    if not isinstance(available, bool):
        raise serializers.ValidationError({day_name: "`available` must be a boolean"})
    return {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"), "available": available}


class WorkingHoursField(serializers.JSONField):
    """Validates the shape of the per-weekday working-hours object."""

    def to_internal_value(self, data: object) -> dict[str, dict[str, str | bool]]:
        if not isinstance(data, dict):
            raise serializers.ValidationError("Must be an object keyed by weekday")
        missing = set(WEEKDAYS) - set(data.keys())
        if missing:
            raise serializers.ValidationError(f"Missing weekdays: {sorted(missing)}")
        extra = set(data.keys()) - set(WEEKDAYS)
        if extra:
            raise serializers.ValidationError(f"Unknown weekdays: {sorted(extra)}")
        return {day: _validate_day(day, data[day]) for day in WEEKDAYS}


SUPPORTED_COUNTRIES = (
    "CZ", "SK", "AT", "DE", "PL", "HU", "FR", "IT", "ES", "GB",
    "IE", "NL", "BE", "US", "CA", "AU", "NZ", "NO", "SE", "DK", "FI",
)


class MeSerializer(serializers.ModelSerializer):
    working_hours = WorkingHoursField()
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "email",
            "first_name",
            "last_name",
            "phone",
            "working_hours",
            "country",
            "share_enabled",
            "share_token",
            "avatar_url",
            "buffer_before_min",
            "buffer_after_min",
            "accepts_google_bookings",
            "accepts_microsoft_bookings",
        )
        read_only_fields = ("email", "share_token", "avatar_url")

    def get_avatar_url(self, obj: User) -> str | None:
        if not obj.avatar:
            return None
        request = self.context.get("request")
        url = obj.avatar.url
        return request.build_absolute_uri(url) if request else url

    def validate_country(self, value: str) -> str:
        v = (value or "").strip().upper()
        if v not in SUPPORTED_COUNTRIES:
            raise serializers.ValidationError(
                f"Unsupported country code. Pick one of: {', '.join(SUPPORTED_COUNTRIES)}",
            )
        return v


class PublicProfileSerializer(serializers.ModelSerializer):
    """Read-only payload for /api/public/profile/<token>. Strips PII."""

    display_name = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("display_name", "avatar_url", "country", "working_hours")
        read_only_fields = fields

    def get_display_name(self, obj: User) -> str:
        full = f"{obj.first_name} {obj.last_name}".strip()
        return full or obj.email.split("@")[0]

    def get_avatar_url(self, obj: User) -> str | None:
        if not obj.avatar:
            return None
        request = self.context.get("request")
        url = obj.avatar.url
        return request.build_absolute_uri(url) if request else url


class TeammateSerializer(serializers.ModelSerializer):
    """Public profile shape, returned only when the caller shares a team."""

    shared_team_ids = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "phone",
            "working_hours",
            "country",
            "shared_team_ids",
        )
        read_only_fields = fields

    def get_shared_team_ids(self, obj: User) -> list[int]:
        # Populated by the view via context to avoid an extra query per request.
        return self.context.get("shared_team_ids", [])
