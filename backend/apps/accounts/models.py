from __future__ import annotations

import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models

# Per PRD §5.1: default working hours are Mon-Fri 8:00-17:00, weekends unavailable.
# Single window per day in MVP; lunch breaks etc. are out of scope.
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def default_working_hours() -> dict[str, dict[str, str | bool]]:
    weekday_default = {"start": "08:00", "end": "17:00", "available": True}
    weekend_default = {"start": "09:00", "end": "13:00", "available": False}
    return {
        **{day: dict(weekday_default) for day in WEEKDAYS[:5]},
        **{day: dict(weekend_default) for day in WEEKDAYS[5:]},
    }


class UserManager(BaseUserManager):
    """Email-as-identity user manager. Username field is unused."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    """Custom user with email as the unique identifier; no username."""

    username = None
    email = models.EmailField("email address", unique=True)
    first_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    avatar = models.ImageField(upload_to="avatars/", blank=True, null=True)
    working_hours = models.JSONField(default=default_working_hours)
    # ISO 3166-1 alpha-2 country code; drives public-holiday markers in the
    # calendar UI. Default CZ (current target market). User-changeable.
    country = models.CharField(max_length=2, default="CZ")
    # Per-event × per-channel matrix (PRD §5.6). Initialized at signup via
    # apps.notifications.signals. Empty dict for legacy users; the dispatcher
    # treats a missing event key as "all on", matching the default.
    notification_prefs = models.JSONField(default=dict, blank=True)

    # Public profile sharing (M17). When share_enabled is True, anyone with
    # /u/<share_token> can view a read-only busy/free + name + avatar page.
    # Regenerating the token (apps.accounts.views.RegenerateShareTokenView)
    # invalidates the previous URL immediately.
    share_enabled = models.BooleanField(default=False)
    share_token = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)

    # Buffer minutes the availability engine adds around every busy block
    # so back-to-back meetings can't happen. "Before" pads the tail of a
    # slot ending too close to the next busy block; "After" pads the head
    # of a slot starting too close to the previous busy block. Zero on
    # both means no buffer (default) — matches Calendly's default.
    buffer_before_min = models.PositiveSmallIntegerField(default=0)
    buffer_after_min = models.PositiveSmallIntegerField(default=0)

    # Per-provider booking toggles (PRD §booking-controls). Let the user
    # disable new bookings from a specific OAuth provider independently of
    # whether that OAuth account is still connected. Defaults to True so
    # existing users keep their current behaviour unchanged.
    accepts_google_bookings = models.BooleanField(default=True)
    accepts_microsoft_bookings = models.BooleanField(default=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    def __str__(self) -> str:
        return self.email
