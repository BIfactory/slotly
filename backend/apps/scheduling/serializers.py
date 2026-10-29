"""
apps/scheduling/serializers.py — DRF serializers for the scheduling app.

CalendarAccountSerializer — read-side representation of a single linked
OAuth account (Google or Microsoft) for the GET /api/calendar-accounts list.

Composite ID convention
-----------------------
Because Google and Microsoft have independent integer PKs (Google #3 and
Microsoft #3 can both exist for the same user), we use a composite id string
to unambiguously identify an account across all endpoints:

    "g:<pk>"   — a GoogleAccount with that PK
    "m:<pk>"   — a MicrosoftAccount with that PK

This id is returned in the GET response and accepted in URL paths and POST
bodies. Example: DELETE /api/calendar-accounts/g:3
"""

from __future__ import annotations

from rest_framework import serializers


class UserLocationSerializer(serializers.Serializer):
    """Read/write serializer for UserLocation.

    Used by GET (list), POST (create), and PATCH (update) on /api/me/locations.
    ``is_default`` is optional on write — omitting it means "no change" on PATCH,
    and defaults to False on create.
    """

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(max_length=100)
    address = serializers.CharField(max_length=400)
    is_default = serializers.BooleanField(required=False, default=False)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def validate_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("This field may not be blank.")
        if len(value) > 100:
            raise serializers.ValidationError("Ensure this field has no more than 100 characters.")
        return value

    def validate_address(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("This field may not be blank.")
        if len(value) > 400:
            raise serializers.ValidationError("Ensure this field has no more than 400 characters.")
        return value


class CalendarAccountSerializer(serializers.Serializer):
    """Read-only serializer for a single linked OAuth account.

    The ``id`` field uses the composite "g:<pk>" / "m:<pk>" convention so
    callers never need to supply a ``provider`` field alongside a plain integer
    when targeting a specific account.
    """

    id = serializers.CharField(read_only=True)
    provider = serializers.CharField(read_only=True)      # "google" or "microsoft"
    email = serializers.EmailField(read_only=True)
    write_calendar_id = serializers.CharField(read_only=True)
    is_default = serializers.BooleanField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)


def build_account_list(user) -> list[dict]:
    """Build the list of dicts that CalendarAccountSerializer will serialise.

    Combines all of the user's GoogleAccount and MicrosoftAccount rows and
    marks each account as is_default=True iff it is the default for its own
    provider (i.e. Google account whose pk == wd.google_account_id, or MS
    account whose pk == wd.microsoft_account_id).  A user who has set both a
    Google default AND an MS default will see TWO accounts with is_default=True.
    Sorted by created_at descending (most-recently connected first).
    """
    from .models import GoogleAccount, MicrosoftAccount, WriteCalendarDefault

    # Resolve current default (if any).
    default = (
        WriteCalendarDefault.objects
        .filter(user=user)
        .only("google_account_id", "microsoft_account_id")
        .first()
    )
    default_google_id = default.google_account_id if default else None
    default_microsoft_id = default.microsoft_account_id if default else None

    accounts: list[dict] = []

    for g in GoogleAccount.objects.filter(user=user).order_by("-created_at"):
        accounts.append({
            "id": f"g:{g.pk}",
            "provider": "google",
            "email": g.google_email,
            "write_calendar_id": g.write_calendar_id or "primary",
            "is_default": (g.pk == default_google_id),
            "created_at": g.created_at,
        })

    for m in MicrosoftAccount.objects.filter(user=user).order_by("-created_at"):
        accounts.append({
            "id": f"m:{m.pk}",
            "provider": "microsoft",
            "email": m.microsoft_email,
            "write_calendar_id": m.write_calendar_id,
            "is_default": (m.pk == default_microsoft_id),
            "created_at": m.created_at,
        })

    # Sort combined list newest-first.
    accounts.sort(key=lambda a: a["created_at"], reverse=True)
    return accounts


def resolve_composite_id(user, composite_id: str):
    """Parse a composite id string (e.g. ``"g:3"`` or ``"m:5"``) and return
    the corresponding GoogleAccount or MicrosoftAccount instance.

    Returns ``(account, provider_str)`` or raises ``ValueError`` for bad
    format / not found / wrong owner.
    """
    from .models import GoogleAccount, MicrosoftAccount

    if not isinstance(composite_id, str) or ":" not in composite_id:
        raise ValueError(f"Invalid composite id: {composite_id!r}")

    prefix, _, raw_pk = composite_id.partition(":")
    try:
        pk = int(raw_pk)
    except ValueError:
        raise ValueError(f"Invalid composite id: {composite_id!r}")

    if prefix == "g":
        try:
            account = GoogleAccount.objects.get(pk=pk, user=user)
        except GoogleAccount.DoesNotExist:
            raise ValueError("not_found")
        return account, "google"
    elif prefix == "m":
        try:
            account = MicrosoftAccount.objects.get(pk=pk, user=user)
        except MicrosoftAccount.DoesNotExist:
            raise ValueError("not_found")
        return account, "microsoft"
    else:
        raise ValueError(f"Unknown provider prefix: {prefix!r}")
