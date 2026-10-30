"""
Per-user Google account linkage for booking-into-Google flows.

Stores the OAuth tokens needed to call `events.insert` on the user's primary
Google Calendar. Both access and refresh tokens are Fernet-encrypted at rest
— anyone reading the raw row gets ciphertext, not credentials. The encryption
key is the same `CALENDAR_URL_ENCRYPTION_KEY` used by apps.calendars (a
single key keeps the deploy surface small; can be split later if the threat
model demands it).
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import CheckConstraint, Index, Q, UniqueConstraint


class UserLocation(models.Model):
    """Named location predefined by a user, e.g. "Kavárna U Fialek, Praha 2".

    Users build a list of locations in /settings/locations and then pick one
    when configuring a MeetingType (physical kind) or overriding the default on
    the public booking page. The list is personal — two users may have
    identically-named locations without conflict.

    ``is_default`` flags the location Slotly should pre-select in pickers;
    the "at most one default per user" invariant is enforced at the endpoint
    layer (G2), not here.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="locations",
    )
    name = models.CharField(max_length=100)        # "Kavárna U Fialek"
    address = models.CharField(max_length=400)      # "Vinohradská 42, Praha 2"
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["user", "name"],
                name="unique_location_name_per_user",
            ),
        ]
        indexes = [Index(fields=["user", "is_default"], name="sched_uloc_user_is_default_idx")]

    def __str__(self) -> str:
        return f"{self.name} ({self.user_id})"


# Maximum number of saved locations per user. Shared constant used by the
# CRUD view (scheduling) and the public profile view (accounts) so both caps
# stay in sync without a second hard-coded literal.
USER_LOCATION_CAP = 20


class GoogleAccount(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="google_accounts",
    )
    # Email of the linked Google identity. May differ from the user's Slotly
    # email — we display this so the user can see *which* Google account is
    # currently authorised.
    google_email = models.EmailField()

    access_token_encrypted = models.TextField()
    refresh_token_encrypted = models.TextField()
    # UTC instant after which the access token is considered stale. Refreshed
    # opportunistically via `google_client.get_credentials`.
    expires_at = models.DateTimeField()
    # OAuth scope actually granted (Google may grant less than requested).
    scope = models.CharField(max_length=500, blank=True)

    # ID of the Google Calendar new events go into (both /people booking and
    # public /u/<token> booking write here). Defaults to "primary" — the
    # user's main calendar — so meetings work the moment they connect Google.
    # Configurable in /settings/integrations from the list of calendars the
    # user has writer/owner access to.
    write_calendar_id = models.CharField(max_length=1024, default="primary")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("user",))]
        constraints = [
            UniqueConstraint(
                fields=("user", "google_email"),
                name="unique_google_account_per_user_email",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.google_email}"


class MeetingLinkKind(models.TextChoices):
    """Controls which video-link (if any) Slotly requests when creating a
    calendar event.

    ``AUTO``         — use whatever the provider attaches by default (e.g.
                       Google Meet for Google, Teams for Microsoft).
    ``GOOGLE_MEET``  — explicitly request a Google Meet link.
    ``TEAMS``        — explicitly request a Microsoft Teams meeting link.
    ``NONE``         — strip any auto-generated video link from the event.

    Both ``Booking`` and ``MeetingType`` share this class so that the host
    preset (``MeetingType.meeting_link_kind``) and the per-booking override
    (``Booking.meeting_link_kind``) express the same vocabulary.
    """

    AUTO = "auto", "Auto (use host's default)"
    GOOGLE_MEET = "google_meet", "Google Meet"
    TEAMS = "teams", "Microsoft Teams"
    NONE = "none", "No video link"


class MeetingType(models.Model):
    """Host-defined preset a visitor picks *before* they see any slots.

    Calendly's core UX primitive: instead of "pick any free time for as
    long as you want", the visitor sees a list of options ("15-min chat",
    "30-min demo", "60-min deep dive") and each one locks the booking to
    a specific duration, kind (online/physical), and — for physical — a
    default location. Optional: hosts who don't define any types keep
    the generic flow (visitor picks duration in the dialog).
    """

    class Kind(models.TextChoices):
        ONLINE = "online", "Online"
        PHYSICAL = "physical", "In person"

    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="meeting_types",
    )
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=60)
    description = models.TextField(blank=True)
    duration_min = models.PositiveSmallIntegerField(default=30)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.ONLINE)
    # Default location for physical meetings — visitor can still override.
    location = models.CharField(max_length=300, blank=True)
    # Hex colour used to tint the type card on the public booking page.
    color = models.CharField(max_length=7, default="#4f46e5")
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=0)

    # Ordered list of {id, label, kind, required, options?} — questions
    # the visitor must answer before they can pick a slot with this type.
    # kind is one of "text" | "textarea" | "select"; select uses options.
    # id is a stable UUID string so Booking.custom_answers can key by it
    # and survive question renames or reorders.
    questions = models.JSONField(default=list, blank=True)

    # Optional URL to send the visitor to after a successful booking —
    # typically a "thank you" page on the host's own site (Calendly-style
    # conversion tracking). Empty = show the inline success block instead.
    redirect_url = models.URLField(max_length=2000, blank=True)

    # Optional FK to a UserLocation the host has saved — the canonical way to
    # pre-select a location when this meeting type is physical. If the
    # referenced UserLocation is later deleted, the FK goes NULL (SET_NULL)
    # but the free-form `location` CharField above is left intact as a
    # fallback. The two fields are independent: `default_location` is the
    # "structured" reference; `location` is the "free text" fallback.
    default_location = models.ForeignKey(
        "UserLocation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # Host's preferred video-link mode for bookings on this meeting type.
    # Defaults to "auto" — the provider's built-in video link is used as-is.
    # Visitors landing on a booking page can override this at the Booking level
    # (B2/B3 will expose a picker on the public page).
    meeting_link_kind = models.CharField(
        max_length=16,
        choices=MeetingLinkKind.choices,
        default=MeetingLinkKind.AUTO,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("display_order", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("host", "slug"),
                name="uniq_meeting_type_host_slug",
            ),
        ]
        indexes = [models.Index(fields=("host", "is_active"))]

    def __str__(self) -> str:
        return f"{self.name} ({self.duration_min}min)"


class Booking(models.Model):
    """Record of a calendar event that Slotly created on behalf of the host.

    Populated only for **public** bookings (visitor-facing flow) — peer /
    group bookings from /people and /search don't need this row because the
    invitees already control the event in their own calendars. Purpose here
    is to give the visitor a stable, unguessable manage URL (``/b/<uuid>``)
    they can cancel or reschedule from without needing a Slotly account.

    Kept separate from ``BookingRequest`` because that model tracks the
    *pending-approval* lifecycle; this one tracks the *confirmed* event.
    A physical BookingRequest that gets approved spawns a Booking row.
    """

    class Provider(models.TextChoices):
        GOOGLE = "google", "Google"
        MICROSOFT = "microsoft", "Microsoft"

    class Kind(models.TextChoices):
        ONLINE = "online", "Online"
        PHYSICAL = "physical", "In person"

    class Status(models.TextChoices):
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="hosted_bookings",
    )
    provider = models.CharField(max_length=16, choices=Provider.choices)
    calendar_id = models.CharField(max_length=1024, blank=True)
    event_id = models.CharField(max_length=256, blank=True, db_index=True)

    visitor_name = models.CharField(max_length=120, blank=True)
    visitor_email = models.EmailField(blank=True)
    # For group bookings from /search or /people, every invitee's email
    # goes here. Public single-visitor bookings put a one-element list
    # here too so downstream code can read one field.
    attendee_emails = models.JSONField(default=list, blank=True)

    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.ONLINE)
    title = models.CharField(max_length=200, blank=True)
    location = models.CharField(max_length=300, blank=True)
    notes = models.TextField(blank=True)

    start_at = models.DateTimeField()
    end_at = models.DateTimeField()

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CONFIRMED)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True)
    cancelled_by_visitor = models.BooleanField(default=False)

    # Timestamp of the most-recent reminder mail sent (observability only).
    # Which stages have been sent is authoritative in ``reminded_stages``.
    reminded_at = models.DateTimeField(null=True, blank=True)
    # Reminder stages already mailed for this booking (e.g. ["24h"] or
    # ["24h", "1h"]). The management command runs once per stage on its
    # own cron cadence and skips rows whose stage is already in the list.
    reminded_stages = models.JSONField(default=list, blank=True)

    # Answers the visitor gave to the MeetingType's custom questions,
    # keyed by question id → answer string. Empty for bookings without
    # a meeting type or without questions on the picked type.
    custom_answers = models.JSONField(default=dict, blank=True)

    # Visitor-level override for the video-link mode. Defaults to "auto" so
    # that existing bookings continue to behave exactly as before. B2/B3 will
    # expose a picker on the public booking page that writes a specific value
    # here; the booking-creation service will read this when building the
    # calendar event payload.
    meeting_link_kind = models.CharField(
        max_length=16,
        choices=MeetingLinkKind.choices,
        default=MeetingLinkKind.AUTO,
    )

    # Visitor's picked language at booking time. Drives the language of the
    # visitor-facing emails Slotly sends later (host-cancel, reminders, etc.).
    # Default "en" preserves pre-i18n behavior for existing rows.
    class Locale(models.TextChoices):
        ENGLISH = "en", "English"
        CZECH = "cs", "Czech"

    locale = models.CharField(
        max_length=2,
        choices=Locale.choices,
        default=Locale.ENGLISH,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("host", "-start_at")),
            models.Index(fields=("visitor_email", "-start_at")),
        ]

    def __str__(self) -> str:
        return f"Booking(host={self.host_id}, {self.status}, {self.start_at})"


class BookingRequest(models.Model):
    """A visitor-submitted "please meet me" that needs host approval before
    it becomes a calendar event. Only used for the `physical` kind — online
    bookings go straight into the calendar (host has already committed to
    being bookable via their public link).

    ``requester_user`` is populated when the requester is a signed-in Slotly
    user (peer booking from /people/<id>); otherwise ``visitor_email`` and
    ``visitor_name`` identify them. ``event_id`` is filled once the host
    approves and we successfully create the calendar event, so we can link
    from the request row to the underlying event."""

    class Kind(models.TextChoices):
        PHYSICAL = "physical", "In person"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="booking_requests",
    )
    requester_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    visitor_name = models.CharField(max_length=120, blank=True)
    visitor_email = models.EmailField(blank=True)

    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.PHYSICAL)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()

    title = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    location = models.CharField(max_length=300, blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    decision_note = models.CharField(max_length=500, blank=True)
    event_id = models.CharField(max_length=256, blank=True)

    # Visitor's picked language at request time. Copied onto the Booking
    # when the host approves this request, so the approval/rejection email
    # and later reminders go out in the right language.
    locale = models.CharField(
        max_length=2,
        choices=Booking.Locale.choices,
        default=Booking.Locale.ENGLISH,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("host", "status", "-created_at")),
        ]

    def __str__(self) -> str:
        return f"BookingRequest(host={self.host_id}, {self.status}, {self.start_at})"


class MicrosoftAccount(models.Model):
    """Per-user Microsoft (Graph) account linkage — mirror of GoogleAccount
    for Outlook/Microsoft 365 users. Same encryption scheme (same key), same
    field shape, so the surrounding code (public flag, booking dispatch,
    settings UI) can treat both providers with one abstraction.

    ``write_calendar_id`` empty string means "primary calendar" (Graph's
    ``/me/calendar/events`` endpoint); a non-empty value picks a specific
    calendar via ``/me/calendars/<id>/events``.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="microsoft_accounts",
    )
    microsoft_email = models.EmailField()

    access_token_encrypted = models.TextField()
    refresh_token_encrypted = models.TextField()
    expires_at = models.DateTimeField()
    scope = models.CharField(max_length=500, blank=True)

    write_calendar_id = models.CharField(max_length=1024, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("user",))]
        constraints = [
            UniqueConstraint(
                fields=("user", "microsoft_email"),
                name="unique_microsoft_account_per_user_email",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.microsoft_email}"


class WriteCalendarDefault(models.Model):
    """Records which of the user's linked OAuth accounts is the
    *write-side provider* — the one that receives new calendar events.

    Both ``google_account`` and ``microsoft_account`` can be non-null
    simultaneously: a user who has connected both providers can independently
    pick which Google account AND which Microsoft account should receive new
    events. At least one must be set; both-null is rejected by the DB
    constraint ``write_default_has_at_least_one_provider``.

    The old XOR constraint (``write_default_exactly_one_provider``) has been
    removed as of migration 0015.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="write_calendar_default",
    )
    google_account = models.ForeignKey(
        "GoogleAccount",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    microsoft_account = models.ForeignKey(
        "MicrosoftAccount",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        constraints = [
            CheckConstraint(
                condition=Q(google_account__isnull=False) | Q(microsoft_account__isnull=False),
                name="write_default_has_at_least_one_provider",
            ),
        ]

    def default_write_target(self, provider: str):
        """Return the FK for *provider* (``"google"`` or ``"microsoft"``), or None."""
        if provider == "google":
            return self.google_account
        if provider == "microsoft":
            return self.microsoft_account
        return None

    def has_default(self, provider: str) -> bool:
        """Return True if the given provider has a non-null FK on this row."""
        return self.default_write_target(provider) is not None

    def primary_write_target(self):
        """Back-compat accessor: returns Google account if set, else Microsoft.

        Callers that do not yet distinguish per-provider defaults use this to
        get *a* write target without caring which provider it is. Google takes
        precedence because it was the original default provider.
        """
        return self.google_account or self.microsoft_account

    @property
    def selected_account(self):
        """Back-compat: Return the primary non-null account (Google > Microsoft)."""
        return self.primary_write_target()

    @property
    def provider(self) -> str:
        """Back-compat: reports the *primary* active provider (Google > Microsoft).

        Returns ``"google"`` when ``google_account_id`` is non-null, otherwise
        ``"microsoft"``.

        **Warning — ambiguous when both providers are active.**  When both
        ``google_account_id`` and ``microsoft_account_id`` are non-null this
        property silently returns ``"google"`` with no indication that a
        Microsoft default also exists.  Code that needs to route a new event to
        a *specific* provider MUST call
        ``apps.scheduling.services._pick_write_provider(user, provider=...)``
        instead (delivered in Task A2).  This property exists only for
        back-compat with single-provider callers.
        """
        return "google" if self.google_account_id is not None else "microsoft"

    @property
    def write_calendar_id(self) -> str:
        """Proxy the write_calendar_id from the primary account."""
        return self.selected_account.write_calendar_id

    def __str__(self) -> str:
        providers = []
        if self.google_account_id is not None:
            providers.append("google")
        if self.microsoft_account_id is not None:
            providers.append("microsoft")
        return f"WriteCalendarDefault(user={self.user_id}, providers={'+'.join(providers) or 'none'})"


class OAuthBusyWindow(models.Model):
    """Busy interval harvested from a connected OAuth account's calendars
    via Google freebusy / MS Graph getSchedule.

    Privacy: starts_at + ends_at only. Never title, description, location,
    attendees. Any addition must trigger a privacy review (PRD §5.2, F15-R9).

    Transparent events (Google: transparency=transparent) are NOT included —
    the Google freebusy API only returns opaque (blocking) events. All-day
    events that are transparent are therefore also excluded; this differs
    from the ICS pipeline which treats all-day as always busy. This semantic
    drift is intentional — see docs/oauth-busy.md.

    Nullable FKs because a user has either a GoogleAccount OR a MicrosoftAccount
    rooting a given row — but never both. The `provider` field is the discriminator.
    """

    PROVIDER_CHOICES = [("google", "Google"), ("microsoft", "Microsoft")]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="oauth_busy_windows",
    )
    provider = models.CharField(max_length=16, choices=PROVIDER_CHOICES)
    google_account = models.ForeignKey(
        "GoogleAccount",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="busy_windows",
    )
    microsoft_account = models.ForeignKey(
        "MicrosoftAccount",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="busy_windows",
    )
    calendar_id = models.CharField(max_length=1024)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=("user", "starts_at", "ends_at")),
            models.Index(fields=("google_account", "fetched_at")),
            models.Index(fields=("microsoft_account", "fetched_at")),
        ]
        constraints = [
            CheckConstraint(
                condition=(
                    Q(google_account__isnull=False)
                    & Q(microsoft_account__isnull=True)
                    & Q(provider="google")
                )
                | (
                    Q(google_account__isnull=True)
                    & Q(microsoft_account__isnull=False)
                    & Q(provider="microsoft")
                ),
                name="oauth_busy_window_exactly_one_provider",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"OAuthBusyWindow(user={self.user_id}, provider={self.provider},"
            f" {self.starts_at}–{self.ends_at})"
        )
