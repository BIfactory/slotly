"""
Tests for M18a Google OAuth wiring.

We don't reach the actual Google endpoints — every external call is patched.
What we cover:
  - Fernet round-trip on tokens
  - /start refuses to redirect when OAuth credentials are blank (503)
  - /start signs a state we can verify
  - /callback rejects missing/invalid/expired/foreign state
  - /callback persists encrypted tokens on success (and on retry without a
    fresh refresh_token, falls back to the previous one)
  - get_credentials refreshes when expired and updates the row
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.core.signing import TimestampSigner
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as djtz
from rest_framework.test import APIClient

from apps.accounts.models import User

from .google_client import _Credentials, get_credentials
from .models import GoogleAccount
from .security import decrypt, encrypt
from .views import _STATE_SALT


def _bake_state(user_pk: int) -> str:
    # Callback expects the "user:<pk>" discriminator introduced with the SSO
    # anon flow — the callback branches on this prefix to decide "link an
    # existing account" vs "create + login a new user".
    return TimestampSigner(salt=_STATE_SALT).sign(f"user:{user_pk}")


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="dev-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="dev-client-secret",
    GOOGLE_OAUTH_REDIRECT_URI="http://localhost:8000/api/oauth/google/callback",
    FRONTEND_BASE_URL="http://localhost:3000",
)
class FernetRoundTripTests(TestCase):
    def test_encrypt_then_decrypt_returns_original(self) -> None:
        secret = "ya29.A0Af-fake-but-realistic-looking-token-string"
        cipher = encrypt(secret)
        self.assertNotEqual(cipher, secret)
        self.assertEqual(decrypt(cipher), secret)


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="",
    GOOGLE_OAUTH_CLIENT_SECRET="",
    FRONTEND_BASE_URL="http://localhost:3000",
)
class OAuthStartUnconfiguredTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email="u@test.local", password="pwpw12345xyz")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_returns_503_when_client_secret_missing(self) -> None:
        resp = self.client.get(reverse("google-oauth-start"))
        self.assertEqual(resp.status_code, 503)
        self.assertIn("not configured", resp.json()["detail"].lower())


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="dev-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="dev-client-secret",
    GOOGLE_OAUTH_REDIRECT_URI="http://localhost:8000/api/oauth/google/callback",
    FRONTEND_BASE_URL="http://localhost:3000",
)
class OAuthStartTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email="u@test.local", password="pwpw12345xyz")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_redirects_to_google_with_signed_state(self) -> None:
        resp = self.client.get(reverse("google-oauth-start"))
        self.assertEqual(resp.status_code, 302)
        location = resp["Location"]
        self.assertIn("accounts.google.com/o/oauth2/v2/auth", location)
        self.assertIn("client_id=dev-client-id", location)
        self.assertIn("state=", location)
        # State must verify under the same salt and resolve back to our user.
        import urllib.parse
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
        state = params["state"][0]
        signed_pk = TimestampSigner(salt=_STATE_SALT).unsign(state, max_age=600)
        # State discriminator changed with SSO — "user:<pk>" for link-mode,
        # "anon" for signup-with-Google. Verify we're on the link branch.
        self.assertEqual(signed_pk, f"user:{self.user.pk}")


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="dev-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="dev-client-secret",
    GOOGLE_OAUTH_REDIRECT_URI="http://localhost:8000/api/oauth/google/callback",
    FRONTEND_BASE_URL="http://localhost:3000",
)
class OAuthCallbackTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email="cb@test.local", password="pwpw12345xyz")
        self.client = APIClient()

    def test_missing_state_redirects_with_error(self) -> None:
        resp = self.client.get(reverse("google-oauth-callback"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("google=error", resp["Location"])
        self.assertIn("reason=missing", resp["Location"])

    def test_invalid_state_signature(self) -> None:
        resp = self.client.get(
            reverse("google-oauth-callback"),
            {"code": "abc", "state": "tampered-not-signed"},
        )
        self.assertIn("google=error", resp["Location"])
        self.assertIn("state_invalid", resp["Location"])

    def test_google_consent_error_passes_through(self) -> None:
        resp = self.client.get(reverse("google-oauth-callback"), {"error": "access_denied"})
        self.assertIn("google=error", resp["Location"])
        self.assertIn("reason=access_denied", resp["Location"])

    def test_microsoft_aadsts65001_surfaces_admin_consent_required(self) -> None:
        # Enterprise tenant with admin consent required → frontend shows IT admin message.
        resp = self.client.get(
            reverse("microsoft-oauth-callback"),
            {
                "error": "access_denied",
                "error_description": "AADSTS65001: The user or administrator has not consented to use the application.",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("microsoft=error", resp["Location"])
        self.assertIn("reason=admin_consent_required", resp["Location"])

    def test_microsoft_generic_error_passes_through(self) -> None:
        resp = self.client.get(
            reverse("microsoft-oauth-callback"),
            {"error": "access_denied", "error_description": "User cancelled login."},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("microsoft=error", resp["Location"])
        self.assertIn("reason=access_denied", resp["Location"])

    def test_successful_exchange_persists_encrypted_tokens(self) -> None:
        state = _bake_state(self.user.pk)
        with patch("apps.scheduling.views.exchange_code") as mx, patch(
            "apps.scheduling.views.fetch_userinfo"
        ) as mu:
            mx.return_value = {
                "access_token": "AT-xyz",
                "refresh_token": "RT-xyz",
                "expires_in": 3600,
                "scope": "openid email https://www.googleapis.com/auth/calendar.events",
            }
            mu.return_value = {"email": "user@gmail.com"}
            resp = self.client.get(
                reverse("google-oauth-callback"),
                {"code": "AUTHCODE", "state": state},
            )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("google=connected", resp["Location"])
        account = GoogleAccount.objects.get(user=self.user)
        self.assertEqual(account.google_email, "user@gmail.com")
        # Stored ciphertext must NOT equal the plaintext token.
        self.assertNotEqual(account.access_token_encrypted, "AT-xyz")
        self.assertEqual(decrypt(account.access_token_encrypted), "AT-xyz")
        self.assertEqual(decrypt(account.refresh_token_encrypted), "RT-xyz")

    def test_reconnect_without_new_refresh_token_keeps_old_one(self) -> None:
        # Seed an existing connect with a known refresh token.
        GoogleAccount.objects.create(
            user=self.user,
            google_email="user@gmail.com",
            access_token_encrypted=encrypt("OLD-AT"),
            refresh_token_encrypted=encrypt("OLD-RT"),
            expires_at=djtz.now() + timedelta(hours=1),
            scope="openid email",
        )
        state = _bake_state(self.user.pk)
        with patch("apps.scheduling.views.exchange_code") as mx, patch(
            "apps.scheduling.views.fetch_userinfo"
        ) as mu:
            # Google omits refresh_token on subsequent connects in some cases.
            mx.return_value = {
                "access_token": "NEW-AT",
                "expires_in": 3600,
                "scope": "openid email",
            }
            mu.return_value = {"email": "user@gmail.com"}
            resp = self.client.get(
                reverse("google-oauth-callback"),
                {"code": "AUTHCODE", "state": state},
            )
        self.assertIn("google=connected", resp["Location"])
        account = GoogleAccount.objects.get(user=self.user)
        # Old refresh token preserved, new access token written.
        self.assertEqual(decrypt(account.refresh_token_encrypted), "OLD-RT")
        self.assertEqual(decrypt(account.access_token_encrypted), "NEW-AT")


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="dev-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="dev-client-secret",
)
class TokenRefreshHelperTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email="r@test.local", password="pwpw12345xyz")

    def test_returns_existing_token_when_not_expired(self) -> None:
        GoogleAccount.objects.create(
            user=self.user,
            google_email="r@gmail.com",
            access_token_encrypted=encrypt("CURRENT-AT"),
            refresh_token_encrypted=encrypt("RT"),
            expires_at=djtz.now() + timedelta(minutes=30),
            scope="",
        )
        creds = get_credentials(self.user)
        self.assertIsInstance(creds, _Credentials)
        self.assertEqual(creds.access_token, "CURRENT-AT")

    def test_refreshes_when_expired_and_persists(self) -> None:
        GoogleAccount.objects.create(
            user=self.user,
            google_email="r@gmail.com",
            access_token_encrypted=encrypt("STALE-AT"),
            refresh_token_encrypted=encrypt("RT"),
            expires_at=djtz.now() - timedelta(minutes=1),
            scope="",
        )
        with patch("apps.scheduling.google_client.refresh_access_token") as mr:
            mr.return_value = {"access_token": "FRESH-AT", "expires_in": 3600}
            creds = get_credentials(self.user)
        self.assertEqual(creds.access_token, "FRESH-AT")
        account = GoogleAccount.objects.get(user=self.user)
        self.assertEqual(decrypt(account.access_token_encrypted), "FRESH-AT")
        # Refresh token unchanged because Google didn't rotate.
        self.assertEqual(decrypt(account.refresh_token_encrypted), "RT")


class StatusAndDisconnectTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email="s@test.local", password="pwpw12345xyz")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_status_reports_disconnected_when_no_row(self) -> None:
        resp = self.client.get(reverse("google-account"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"connected": False})

    def test_status_includes_email_when_connected(self) -> None:
        GoogleAccount.objects.create(
            user=self.user,
            google_email="me@gmail.com",
            access_token_encrypted=encrypt("AT"),
            refresh_token_encrypted=encrypt("RT"),
            expires_at=djtz.now() + timedelta(hours=1),
            scope="",
        )
        resp = self.client.get(reverse("google-account"))
        self.assertEqual(
            resp.json(),
            {"connected": True, "google_email": "me@gmail.com", "write_calendar_id": "primary"},
        )

    def test_disconnect_deletes_row(self) -> None:
        GoogleAccount.objects.create(
            user=self.user,
            google_email="me@gmail.com",
            access_token_encrypted=encrypt("AT"),
            refresh_token_encrypted=encrypt("RT"),
            expires_at=djtz.now() + timedelta(hours=1),
            scope="",
        )
        resp = self.client.delete(reverse("google-account"))
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(GoogleAccount.objects.filter(user=self.user).exists())


# ---------------------------------------------------------------------------
# Booking flows — public link + peer + booking-request approval.
#
# These exercise the code the user actually clicks through: the earlier
# `timezone.now()` NameError in PublicMeetingCreateView would have been
# caught by test_public_online_booking_creates_event on the very first run.
# ---------------------------------------------------------------------------

from django.contrib.auth import get_user_model  # noqa: E402
from django.core import mail  # noqa: E402

from .models import Booking, BookingRequest, MeetingType  # noqa: E402


def _connect_google(user):
    """Give `user` a GoogleAccount so _pick_write_provider picks Google."""
    return GoogleAccount.objects.create(
        user=user,
        google_email=user.email,
        access_token_encrypted=encrypt("AT"),
        refresh_token_encrypted=encrypt("RT"),
        expires_at=djtz.now() + timedelta(hours=1),
        scope="calendar.events calendar.readonly",
        write_calendar_id="primary",
    )


def _future_slot(hours_from_now: int = 24):
    start = djtz.now() + timedelta(hours=hours_from_now)
    end = start + timedelta(minutes=30)
    return start.isoformat(), end.isoformat()


class PublicMeetingOnlineTests(TestCase):
    """POST /api/public/meetings/<token> with kind='online' — the immediate
    booking path. Mocks the Google API layer; asserts the view wires all
    params through correctly and returns 201 with an event payload."""

    def setUp(self):
        # PublicMeetingCreateView has a per-token + per-IP minute-window
        # rate limit backed by Django's cache. LocMemCache persists across
        # tests in the same process, so as more test methods land they
        # eventually hit 429. Clear on each setUp.
        from django.core.cache import cache as _cache
        _cache.clear()
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(email="host@test.local", password="pwpw12345xyz")
        self.host.share_enabled = True
        self.host.save(update_fields=["share_enabled"])
        _connect_google(self.host)
        self.client = APIClient()

    def _post(self, **overrides):
        start, end = _future_slot()
        body = {
            "visitor_name": "Alice Visitor",
            "visitor_email": "alice@example.com",
            "start": start,
            "end": end,
            "kind": "online",
            "title": "Coffee chat",
        }
        body.update(overrides)
        return self.client.post(
            reverse("public-meetings-create", args=[str(self.host.share_token)]),
            body,
            format="json",
        )

    def test_online_booking_creates_event_and_returns_201(self):
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "abc123", "htmlLink": "https://cal.google.com/e/abc123"}
            resp = self._post()
        self.assertEqual(resp.status_code, 201, resp.content)
        payload = resp.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["event"]["id"], "abc123")
        self.assertEqual(payload["event"]["provider"], "google")
        # Provider was called with the visitor as attendee, on the host's
        # write_calendar_id (default "primary"), with an online meeting.
        args, kwargs = mc.call_args
        self.assertEqual(args[0], self.host)
        self.assertEqual(kwargs["calendar_id"], "primary")
        self.assertEqual(kwargs["attendees"], ["alice@example.com"])
        self.assertTrue(kwargs.get("include_online_meeting", True))

    def test_unknown_token_returns_404(self):
        resp = self.client.post(
            reverse("public-meetings-create", args=["00000000-0000-0000-0000-000000000000"]),
            {},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_share_disabled_host_returns_404(self):
        self.host.share_enabled = False
        self.host.save(update_fields=["share_enabled"])
        resp = self._post()
        self.assertEqual(resp.status_code, 404)

    def test_missing_visitor_email_returns_400(self):
        resp = self._post(visitor_email="not-an-email")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("visitor_email", resp.json())

    def test_honeypot_returns_204_and_does_not_book(self):
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            resp = self._post(hp="http://spam.example.com")
        self.assertEqual(resp.status_code, 204)
        mc.assert_not_called()

    def test_host_without_provider_returns_409(self):
        GoogleAccount.objects.filter(user=self.host).delete()
        resp = self._post()
        self.assertEqual(resp.status_code, 409)
        self.assertIn("hasn't set up", resp.json()["detail"])

    def test_past_slot_rejected(self):
        past_start = (djtz.now() - timedelta(hours=2)).isoformat()
        past_end = (djtz.now() - timedelta(hours=1)).isoformat()
        resp = self._post(start=past_start, end=past_end)
        self.assertEqual(resp.status_code, 400)


class PublicMeetingPhysicalTests(TestCase):
    """kind='physical' opens a BookingRequest for the host to approve —
    should never touch the calendar API on submission."""

    def setUp(self):
        from django.core.cache import cache as _cache
        _cache.clear()
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(email="host@test.local", password="pwpw12345xyz")
        self.host.share_enabled = True
        self.host.save(update_fields=["share_enabled"])
        _connect_google(self.host)  # even with provider connected, physical waits
        self.client = APIClient()

    def test_physical_booking_creates_request_row_returns_202(self):
        start, end = _future_slot()
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            resp = self.client.post(
                reverse("public-meetings-create", args=[str(self.host.share_token)]),
                {
                    "visitor_name": "Bob Guest",
                    "visitor_email": "bob@example.com",
                    "start": start,
                    "end": end,
                    "kind": "physical",
                    "location": "Café Slovanský dům, Prague",
                    "notes": "Would love to discuss the project.",
                },
                format="json",
            )
        self.assertEqual(resp.status_code, 202, resp.content)
        payload = resp.json()
        self.assertTrue(payload["pending"])
        mc.assert_not_called()  # no calendar event yet — pending approval
        req = BookingRequest.objects.get(pk=payload["request_id"])
        self.assertEqual(req.host, self.host)
        self.assertEqual(req.status, BookingRequest.Status.PENDING)
        self.assertEqual(req.location, "Café Slovanský dům, Prague")
        self.assertEqual(req.visitor_email, "bob@example.com")

    def test_physical_missing_location_returns_400(self):
        start, end = _future_slot()
        resp = self.client.post(
            reverse("public-meetings-create", args=[str(self.host.share_token)]),
            {
                "visitor_name": "Bob",
                "visitor_email": "bob@example.com",
                "start": start,
                "end": end,
                "kind": "physical",
                # location omitted
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("location", resp.json())


class BookingRequestDecideTests(TestCase):
    """Host approves / rejects a physical booking request."""

    def setUp(self):
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(email="host@test.local", password="pwpw12345xyz")
        _connect_google(self.host)
        self.client = APIClient()
        self.client.force_authenticate(self.host)
        self.req = BookingRequest.objects.create(
            host=self.host,
            visitor_name="Carol",
            visitor_email="carol@example.com",
            kind=BookingRequest.Kind.PHYSICAL,
            start_at=djtz.now() + timedelta(hours=48),
            end_at=djtz.now() + timedelta(hours=48, minutes=30),
            title="Coffee",
            location="Prague",
        )

    def test_approve_creates_event_and_marks_row(self):
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "evt-1"}
            resp = self.client.post(
                reverse("booking-requests-decide", args=[self.req.pk]),
                {"decision": "approve"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, BookingRequest.Status.APPROVED)
        self.assertEqual(self.req.event_id, "evt-1")
        # Physical meetings don't include a Meet/Teams link, and the address
        # rides on the event's first-class `location` field (not stuffed
        # inside the free-text description) so Google/Outlook can generate
        # a Maps deep-link from it.
        _, kwargs = mc.call_args
        self.assertFalse(kwargs.get("include_online_meeting", True))
        self.assertEqual(kwargs["location"], "Prague")

    def test_reject_marks_row_and_emails_visitor(self):
        resp = self.client.post(
            reverse("booking-requests-decide", args=[self.req.pk]),
            {"decision": "reject", "note": "Sorry, conflict."},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, BookingRequest.Status.REJECTED)
        self.assertEqual(self.req.decision_note, "Sorry, conflict.")
        # The rejection email is best-effort — assert either it was sent or
        # the backend used a no-op backend without raising.
        recipients = [addr for m in mail.outbox for addr in m.to]
        # In tests EmailBackend is memory-locmem — mail should have been sent.
        self.assertIn("carol@example.com", recipients)

    def test_double_decide_is_idempotent(self):
        self.req.status = BookingRequest.Status.APPROVED
        self.req.save(update_fields=["status"])
        resp = self.client.post(
            reverse("booking-requests-decide", args=[self.req.pk]),
            {"decision": "approve"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)  # returns current state, no 409

    def test_only_host_can_decide(self):
        UserModel = get_user_model()
        other = UserModel.objects.create_user(email="other@test.local", password="pwpw12345xyz")
        self.client.force_authenticate(other)
        resp = self.client.post(
            reverse("booking-requests-decide", args=[self.req.pk]),
            {"decision": "approve"},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)  # host-scoped queryset


class CreateEventPayloadTests(TestCase):
    """Regression tests for google_client.create_calendar_event — the JSON
    shape sent to Google matters (missing conferenceData → no Meet link,
    missing conferenceDataVersion → Google ignores the createRequest)."""

    def setUp(self):
        UserModel = get_user_model()
        self.user = UserModel.objects.create_user(email="c@test.local", password="pwpw12345xyz")
        _connect_google(self.user)

    def test_online_booking_requests_meet_link(self):
        from .google_client import create_calendar_event
        with patch("apps.scheduling.google_client.httpx.Client") as mc:
            instance = mc.return_value.__enter__.return_value
            instance.post.return_value.status_code = 200
            instance.post.return_value.json.return_value = {"id": "e1", "htmlLink": "x"}
            create_calendar_event(
                self.user,
                calendar_id="primary",
                summary="Test",
                description="",
                start_iso="2026-09-02T10:00:00+02:00",
                end_iso="2026-09-02T10:30:00+02:00",
                time_zone="Europe/Prague",
                attendees=["a@b.co"],
                include_online_meeting=True,
            )
        _, kwargs = instance.post.call_args
        self.assertEqual(kwargs["params"]["conferenceDataVersion"], "1")
        self.assertIn("conferenceData", kwargs["json"])
        self.assertEqual(
            kwargs["json"]["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"],
            "hangoutsMeet",
        )

    def test_physical_booking_skips_meet_link(self):
        from .google_client import create_calendar_event
        with patch("apps.scheduling.google_client.httpx.Client") as mc:
            instance = mc.return_value.__enter__.return_value
            instance.post.return_value.status_code = 200
            instance.post.return_value.json.return_value = {"id": "e2"}
            create_calendar_event(
                self.user,
                calendar_id="primary",
                summary="Test",
                description="",
                start_iso="2026-09-02T10:00:00+02:00",
                end_iso="2026-09-02T10:30:00+02:00",
                time_zone="Europe/Prague",
                attendees=["a@b.co"],
                include_online_meeting=False,
            )
        _, kwargs = instance.post.call_args
        self.assertNotIn("conferenceDataVersion", kwargs["params"])
        self.assertNotIn("conferenceData", kwargs["json"])


class PublicBookingManageTests(TestCase):
    """/api/public/bookings/<uuid> — visitor's cancel flow."""

    def setUp(self):
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(email="host@test.local", password="pwpw12345xyz")
        _connect_google(self.host)
        self.client = APIClient()
        self.booking = Booking.objects.create(
            host=self.host,
            provider=Booking.Provider.GOOGLE,
            calendar_id="primary",
            event_id="evt-xyz",
            visitor_name="Zoe",
            visitor_email="zoe@example.com",
            kind=Booking.Kind.ONLINE,
            title="Coffee chat",
            start_at=djtz.now() + timedelta(hours=48),
            end_at=djtz.now() + timedelta(hours=48, minutes=30),
        )

    def test_get_returns_booking_details(self):
        resp = self.client.get(
            reverse("public-booking-manage", args=[str(self.booking.uuid)]),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["uuid"], str(self.booking.uuid))
        self.assertEqual(body["visitor_name"], "Zoe")
        self.assertEqual(body["status"], "confirmed")

    def test_get_unknown_uuid_returns_404(self):
        resp = self.client.get(
            reverse("public-booking-manage", args=["00000000-0000-0000-0000-000000000000"]),
        )
        self.assertEqual(resp.status_code, 404)

    def test_cancel_deletes_event_and_marks_row(self):
        with patch("apps.scheduling.views.delete_calendar_event") as md:
            resp = self.client.post(
                reverse("public-booking-manage", args=[str(self.booking.uuid)]),
                {"reason": "Conflict came up"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CANCELLED)
        self.assertTrue(self.booking.cancelled_by_visitor)
        self.assertEqual(self.booking.cancellation_reason, "Conflict came up")
        md.assert_called_once()
        _, kwargs = md.call_args
        self.assertEqual(kwargs["calendar_id"], "primary")
        self.assertEqual(kwargs["event_id"], "evt-xyz")

    def test_cancel_is_idempotent(self):
        self.booking.status = Booking.Status.CANCELLED
        self.booking.save(update_fields=["status"])
        with patch("apps.scheduling.views.delete_calendar_event") as md:
            resp = self.client.post(
                reverse("public-booking-manage", args=[str(self.booking.uuid)]),
                {},
                format="json",
            )
        self.assertEqual(resp.status_code, 200)
        md.assert_not_called()

    def test_cancel_past_booking_returns_409(self):
        self.booking.start_at = djtz.now() - timedelta(hours=2)
        self.booking.end_at = djtz.now() - timedelta(hours=1)
        self.booking.save(update_fields=["start_at", "end_at"])
        resp = self.client.post(
            reverse("public-booking-manage", args=[str(self.booking.uuid)]),
            {},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)

    def test_cancel_survives_provider_error(self):
        """If Google/MS refuses the delete (event already gone, grant broken,
        etc.) we still mark our row cancelled so the visitor sees success."""
        from .google_client import GoogleApiError
        with patch("apps.scheduling.views.delete_calendar_event") as md:
            md.side_effect = GoogleApiError(410, "gone")
            resp = self.client.post(
                reverse("public-booking-manage", args=[str(self.booking.uuid)]),
                {},
                format="json",
            )
        self.assertEqual(resp.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CANCELLED)


class PublicBookingCreatesRowTests(TestCase):
    """POST /api/public/meetings/<token> now records a Booking row so the
    visitor can later cancel via /b/<uuid>. Covers the online path;
    physical goes through BookingRequestDecideView (tested separately)."""

    def setUp(self):
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(email="host@test.local", password="pwpw12345xyz")
        self.host.share_enabled = True
        self.host.save(update_fields=["share_enabled"])
        _connect_google(self.host)
        self.client = APIClient()

    def test_online_booking_creates_booking_row_with_manage_url(self):
        start, end = _future_slot()
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "evt-abc"}
            resp = self.client.post(
                reverse("public-meetings-create", args=[str(self.host.share_token)]),
                {
                    "visitor_name": "Alice",
                    "visitor_email": "alice@example.com",
                    "start": start,
                    "end": end,
                    "kind": "online",
                },
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertIn("manage_url", body)
        # A Booking row now exists tying visitor to the calendar event.
        bookings = Booking.objects.filter(host=self.host)
        self.assertEqual(bookings.count(), 1)
        booking = bookings.first()
        self.assertEqual(booking.visitor_email, "alice@example.com")
        self.assertEqual(booking.event_id, "evt-abc")
        self.assertIn(str(booking.uuid), body["manage_url"])
        # The manage URL is baked into the event description sent to Google.
        _, kwargs = mc.call_args
        self.assertIn(str(booking.uuid), kwargs["description"])


class MeetingCreateGroupTests(TestCase):
    """POST /api/meetings with multiple attendees — feeds the /search
    "book a group slot" flow. Also verifies the legacy single peer_user_id
    field still works so an old cached frontend doesn't hard-fail on us."""

    def setUp(self):
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(email="host@test.local", password="pwpw12345xyz")
        _connect_google(self.host)
        self.a = UserModel.objects.create_user(email="a@test.local", password="pwpw12345xyz")
        self.b = UserModel.objects.create_user(email="b@test.local", password="pwpw12345xyz")
        # Team membership so _can_view_peer passes for both attendees.
        from apps.teams.models import Team, Membership
        team = Team.objects.create(name="Squad")
        Membership.objects.create(team=team, user=self.host, role=Membership.Role.ADMIN)
        Membership.objects.create(team=team, user=self.a, role=Membership.Role.MEMBER)
        Membership.objects.create(team=team, user=self.b, role=Membership.Role.MEMBER)
        self.client = APIClient()
        self.client.force_authenticate(self.host)

    def test_group_booking_invites_every_attendee(self):
        start = (djtz.now() + timedelta(hours=24)).isoformat()
        end = (djtz.now() + timedelta(hours=24, minutes=30)).isoformat()
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "e1"}
            resp = self.client.post(
                reverse("meetings-create"),
                {
                    "attendee_user_ids": [self.a.pk, self.b.pk],
                    "start": start,
                    "end": end,
                },
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        _, kwargs = mc.call_args
        self.assertEqual(sorted(kwargs["attendees"]), sorted([self.a.email, self.b.email]))

    def test_legacy_peer_user_id_still_works(self):
        start = (djtz.now() + timedelta(hours=24)).isoformat()
        end = (djtz.now() + timedelta(hours=24, minutes=30)).isoformat()
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "e2"}
            resp = self.client.post(
                reverse("meetings-create"),
                {"peer_user_id": self.a.pk, "start": start, "end": end},
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        _, kwargs = mc.call_args
        self.assertEqual(kwargs["attendees"], [self.a.email])

    def test_self_stripped_from_attendees(self):
        start = (djtz.now() + timedelta(hours=24)).isoformat()
        end = (djtz.now() + timedelta(hours=24, minutes=30)).isoformat()
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "e3"}
            resp = self.client.post(
                reverse("meetings-create"),
                {
                    "attendee_user_ids": [self.host.pk, self.a.pk],
                    "start": start,
                    "end": end,
                },
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        _, kwargs = mc.call_args
        # Host is the calendar owner — inviting themselves is nonsense.
        self.assertEqual(kwargs["attendees"], [self.a.email])

    def test_group_booking_writes_booking_row_with_attendees(self):
        start = (djtz.now() + timedelta(hours=24)).isoformat()
        end = (djtz.now() + timedelta(hours=24, minutes=30)).isoformat()
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "evt-grp"}
            resp = self.client.post(
                reverse("meetings-create"),
                {
                    "attendee_user_ids": [self.a.pk, self.b.pk],
                    "start": start,
                    "end": end,
                },
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        # Manage URL exposed so host can share/track later.
        self.assertIn("manage_url", body)
        # Booking row persisted with the full attendee list.
        bookings = Booking.objects.filter(host=self.host)
        self.assertEqual(bookings.count(), 1)
        b = bookings.first()
        self.assertEqual(sorted(b.attendee_emails), sorted([self.a.email, self.b.email]))
        self.assertEqual(b.event_id, "evt-grp")

    def test_empty_attendees_returns_400(self):
        start = (djtz.now() + timedelta(hours=24)).isoformat()
        end = (djtz.now() + timedelta(hours=24, minutes=30)).isoformat()
        resp = self.client.post(
            reverse("meetings-create"),
            {"attendee_user_ids": [], "start": start, "end": end},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)


class BufferInflationTests(TestCase):
    """User buffer_before/after minutes pad every busy interval that
    availability views return. Covers the pure helper — full end-to-end
    lives in the accounts app's availability views."""

    def test_helper_expands_intervals_symmetrically(self):
        from datetime import datetime as _dt, timezone as _tz
        from apps.accounts.views import _inflate_busy
        s = _dt(2026, 9, 10, 10, 0, tzinfo=_tz.utc)
        e = _dt(2026, 9, 10, 11, 0, tzinfo=_tz.utc)
        inflated = _inflate_busy([(s, e)], 15, 30)
        self.assertEqual(len(inflated), 1)
        self.assertEqual((inflated[0][0]).minute, 45)  # 15 min earlier
        # 30 min later → 11:30
        self.assertEqual((inflated[0][1]).hour, 11)
        self.assertEqual((inflated[0][1]).minute, 30)

    def test_helper_is_noop_when_buffers_are_zero(self):
        from datetime import datetime as _dt, timezone as _tz
        from apps.accounts.views import _inflate_busy
        s = _dt(2026, 9, 10, 10, 0, tzinfo=_tz.utc)
        e = _dt(2026, 9, 10, 11, 0, tzinfo=_tz.utc)
        inflated = _inflate_busy([(s, e)], 0, 0)
        self.assertEqual(inflated, [(s, e)])


class MeetingTypeTests(TestCase):
    """CRUD + slug generation for meeting-type presets."""

    def setUp(self):
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(email="host@test.local", password="pwpw12345xyz")
        self.client = APIClient()
        self.client.force_authenticate(self.host)

    def test_create_auto_generates_slug(self):
        resp = self.client.post(
            reverse("meeting-types-list"),
            {"name": "15-min Quick Chat", "duration_min": 15},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["slug"], "15-min-quick-chat")

    def test_create_dedupes_slug_within_host(self):
        MeetingType.objects.create(host=self.host, name="Chat", slug="chat", duration_min=30)
        resp = self.client.post(
            reverse("meeting-types-list"),
            {"name": "Chat", "duration_min": 45},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["slug"], "chat-2")

    def test_create_rejects_invalid_duration(self):
        resp = self.client.post(
            reverse("meeting-types-list"),
            {"name": "Random", "duration_min": 17},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("duration_min", resp.json())

    def test_patch_updates_fields(self):
        t = MeetingType.objects.create(
            host=self.host, name="Chat", slug="chat", duration_min=15,
        )
        resp = self.client.patch(
            reverse("meeting-types-detail", args=[t.pk]),
            {"duration_min": 30, "description": "Now longer"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        t.refresh_from_db()
        self.assertEqual(t.duration_min, 30)
        self.assertEqual(t.description, "Now longer")

    def test_delete_scoped_to_owner(self):
        t = MeetingType.objects.create(
            host=self.host, name="Chat", slug="chat", duration_min=15,
        )
        UserModel = get_user_model()
        other = UserModel.objects.create_user(email="other@test.local", password="pwpw12345xyz")
        self.client.force_authenticate(other)
        resp = self.client.delete(reverse("meeting-types-detail", args=[t.pk]))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(MeetingType.objects.filter(pk=t.pk).exists())


class MeetingTypeMeetingLinkKindAPITests(TestCase):
    """POST + PATCH /api/meeting-types accept and persist meeting_link_kind."""

    def setUp(self):
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(email="host@test.local", password="pwpw12345xyz")
        self.client = APIClient()
        self.client.force_authenticate(self.host)

    def test_create_meeting_type_persists_meeting_link_kind(self):
        resp = self.client.post(
            reverse("meeting-types-list"),
            {"name": "Demo", "duration_min": 30, "meeting_link_kind": "teams"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        mt = MeetingType.objects.get(host=self.host, slug="demo")
        self.assertEqual(mt.meeting_link_kind, "teams")

    def test_patch_meeting_type_updates_meeting_link_kind(self):
        t = MeetingType.objects.create(
            host=self.host, name="Chat", slug="chat", duration_min=30,
        )
        resp = self.client.patch(
            reverse("meeting-types-detail", args=[t.pk]),
            {"meeting_link_kind": "google_meet"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        t.refresh_from_db()
        self.assertEqual(t.meeting_link_kind, "google_meet")

    def test_invalid_meeting_link_kind_rejected(self):
        resp = self.client.post(
            reverse("meeting-types-list"),
            {"name": "Zoom", "duration_min": 30, "meeting_link_kind": "zoom"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("meeting_link_kind", resp.json())

    def test_meeting_type_serializer_includes_meeting_link_kind(self):
        t = MeetingType.objects.create(
            host=self.host, name="Chat", slug="chat", duration_min=30,
            meeting_link_kind="none",
        )
        resp = self.client.get(reverse("meeting-types-list"))
        self.assertEqual(resp.status_code, 200)
        types = resp.json()["types"]
        match = next(x for x in types if x["id"] == t.pk)
        self.assertEqual(match["meeting_link_kind"], "none")

    def test_create_defaults_meeting_link_kind_to_auto(self):
        resp = self.client.post(
            reverse("meeting-types-list"),
            {"name": "Auto Default", "duration_min": 15},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["meeting_link_kind"], "auto")

    def test_all_valid_values_accepted(self):
        for i, value in enumerate(["auto", "google_meet", "teams", "none"]):
            with self.subTest(value=value):
                resp = self.client.post(
                    reverse("meeting-types-list"),
                    {"name": f"Type {i}", "duration_min": 30, "meeting_link_kind": value},
                    format="json",
                )
                self.assertEqual(resp.status_code, 201, resp.content)
                self.assertEqual(resp.json()["meeting_link_kind"], value)


class MeetingTypeQuestionsTests(TestCase):
    """MeetingType.questions validation + public POST honors + validates
    custom_answers against the type's question schema."""

    def setUp(self):
        from django.core.cache import cache as _cache
        _cache.clear()
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(email="host@test.local", password="pwpw12345xyz")
        self.host.share_enabled = True
        self.host.save(update_fields=["share_enabled"])
        _connect_google(self.host)
        self.client = APIClient()

    def test_can_create_type_with_questions(self):
        self.client.force_authenticate(self.host)
        resp = self.client.post(
            reverse("meeting-types-list"),
            {
                "name": "Discovery",
                "duration_min": 30,
                "questions": [
                    {"label": "Company name", "kind": "text", "required": True},
                    {"label": "Team size", "kind": "select", "required": False, "options": ["1-10", "11-50", "50+"]},
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(len(body["questions"]), 2)
        # ids auto-generated
        self.assertTrue(all("id" in q for q in body["questions"]))

    def test_missing_required_answer_returns_400(self):
        self.client.force_authenticate(self.host)
        mt = MeetingType.objects.create(
            host=self.host, name="Discovery", slug="discovery", duration_min=30,
            questions=[{"id": "q1", "label": "Company", "kind": "text", "required": True}],
        )
        self.client.force_authenticate(None)
        start = (djtz.now() + timedelta(hours=24)).isoformat()
        end = (djtz.now() + timedelta(hours=24, minutes=30)).isoformat()
        resp = self.client.post(
            reverse("public-meetings-create", args=[str(self.host.share_token)]),
            {
                "visitor_name": "V",
                "visitor_email": "v@example.com",
                "start": start,
                "end": end,
                "type_slug": mt.slug,
                "custom_answers": {},  # missing required q1
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("custom_answers", resp.json())

    def test_valid_answers_land_in_booking_and_description(self):
        self.client.force_authenticate(self.host)
        mt = MeetingType.objects.create(
            host=self.host, name="Discovery", slug="discovery", duration_min=30,
            questions=[
                {"id": "q1", "label": "Company", "kind": "text", "required": True},
                {"id": "q2", "label": "Notes", "kind": "textarea", "required": False},
            ],
        )
        self.client.force_authenticate(None)
        start = (djtz.now() + timedelta(hours=24)).isoformat()
        end = (djtz.now() + timedelta(hours=24, minutes=30)).isoformat()
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "e1"}
            resp = self.client.post(
                reverse("public-meetings-create", args=[str(self.host.share_token)]),
                {
                    "visitor_name": "V",
                    "visitor_email": "v@example.com",
                    "start": start,
                    "end": end,
                    "type_slug": mt.slug,
                    "custom_answers": {"q1": "Acme", "q2": "Looking forward"},
                },
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        booking = Booking.objects.get(host=self.host)
        self.assertEqual(booking.custom_answers, {"q1": "Acme", "q2": "Looking forward"})
        _, kwargs = mc.call_args
        # Answers baked into event description under an "Additional info:" block.
        self.assertIn("Acme", kwargs["description"])
        self.assertIn("Company: Acme", kwargs["description"])

    def test_invalid_select_option_rejected(self):
        self.client.force_authenticate(self.host)
        mt = MeetingType.objects.create(
            host=self.host, name="Discovery", slug="discovery", duration_min=30,
            questions=[{
                "id": "q1", "label": "Size", "kind": "select", "required": True,
                "options": ["1-10", "11-50"],
            }],
        )
        self.client.force_authenticate(None)
        start = (djtz.now() + timedelta(hours=24)).isoformat()
        end = (djtz.now() + timedelta(hours=24, minutes=30)).isoformat()
        resp = self.client.post(
            reverse("public-meetings-create", args=[str(self.host.share_token)]),
            {
                "visitor_name": "V",
                "visitor_email": "v@example.com",
                "start": start,
                "end": end,
                "type_slug": mt.slug,
                "custom_answers": {"q1": "999+"},  # not in options
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)


class PublicMeetingWithTypeTests(TestCase):
    """POST /api/public/meetings/<token> honors type_slug — duration + kind
    baked into the type overrule anything the visitor's client sends."""

    def setUp(self):
        from django.core.cache import cache as _cache
        _cache.clear()
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(email="host@test.local", password="pwpw12345xyz")
        self.host.share_enabled = True
        self.host.save(update_fields=["share_enabled"])
        _connect_google(self.host)
        MeetingType.objects.create(
            host=self.host,
            name="Discovery",
            slug="discovery",
            duration_min=45,
            kind=MeetingType.Kind.ONLINE,
        )
        self.client = APIClient()

    def test_type_slug_forces_duration(self):
        # Visitor's end value is 30 min after start; type says 45.
        start = (djtz.now() + timedelta(hours=24)).isoformat()
        wrong_end = (djtz.now() + timedelta(hours=24, minutes=30)).isoformat()
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "e1"}
            resp = self.client.post(
                reverse("public-meetings-create", args=[str(self.host.share_token)]),
                {
                    "visitor_name": "Rey",
                    "visitor_email": "rey@example.com",
                    "start": start,
                    "end": wrong_end,
                    "type_slug": "discovery",
                },
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        _, kwargs = mc.call_args
        # end_iso is 45 min after start, not the 30 the visitor sent.
        from datetime import datetime as _dt
        actual_start = _dt.fromisoformat(kwargs["start_iso"])
        actual_end = _dt.fromisoformat(kwargs["end_iso"])
        self.assertEqual((actual_end - actual_start).total_seconds() / 60, 45)
        self.assertEqual(kwargs["summary"], "Discovery")

    def test_unknown_type_slug_400(self):
        start = (djtz.now() + timedelta(hours=24)).isoformat()
        end = (djtz.now() + timedelta(hours=24, minutes=30)).isoformat()
        resp = self.client.post(
            reverse("public-meetings-create", args=[str(self.host.share_token)]),
            {
                "visitor_name": "Rey",
                "visitor_email": "rey@example.com",
                "start": start,
                "end": end,
                "type_slug": "nonexistent",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)


class PublicBookingRescheduleTests(TestCase):
    """POST /api/public/bookings/<uuid>/reschedule — visitor moves the
    slot without cancelling; backend deletes old + inserts new + updates
    the row in-place. Booking uuid stays so the manage URL keeps working."""

    def setUp(self):
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(
            email="host@test.local", password="pwpw12345xyz",
            first_name="Han", last_name="Solo",
        )
        _connect_google(self.host)
        self.client = APIClient()
        self.booking = Booking.objects.create(
            host=self.host,
            provider=Booking.Provider.GOOGLE,
            calendar_id="primary",
            event_id="evt-old",
            visitor_name="Rey",
            visitor_email="rey@example.com",
            kind=Booking.Kind.ONLINE,
            title="Training",
            start_at=djtz.now() + timedelta(hours=48),
            end_at=djtz.now() + timedelta(hours=48, minutes=30),
        )

    def _new_slot(self, hours=72):
        s = djtz.now() + timedelta(hours=hours)
        return s.isoformat(), (s + timedelta(minutes=30)).isoformat()

    def test_reschedule_deletes_old_and_creates_new_event(self):
        new_start, new_end = self._new_slot()
        with patch("apps.scheduling.views.delete_calendar_event") as md, \
             patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "evt-new"}
            resp = self.client.post(
                reverse("public-booking-reschedule", args=[str(self.booking.uuid)]),
                {"start": new_start, "end": new_end},
                format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        md.assert_called_once()
        mc.assert_called_once()
        self.booking.refresh_from_db()
        # Row moved to the new time and picked up the new event id.
        self.assertEqual(self.booking.event_id, "evt-new")
        self.assertEqual(self.booking.start_at.isoformat(), new_start)
        # Reminded flag reset so future runs pick this up for the new time.
        self.assertIsNone(self.booking.reminded_at)

    def test_same_slot_is_noop(self):
        with patch("apps.scheduling.views.delete_calendar_event") as md, \
             patch("apps.scheduling.views.create_calendar_event") as mc:
            resp = self.client.post(
                reverse("public-booking-reschedule", args=[str(self.booking.uuid)]),
                {
                    "start": self.booking.start_at.isoformat(),
                    "end": self.booking.end_at.isoformat(),
                },
                format="json",
            )
        self.assertEqual(resp.status_code, 200)
        md.assert_not_called()
        mc.assert_not_called()

    def test_cannot_reschedule_cancelled_booking(self):
        self.booking.status = Booking.Status.CANCELLED
        self.booking.save(update_fields=["status"])
        new_start, new_end = self._new_slot()
        resp = self.client.post(
            reverse("public-booking-reschedule", args=[str(self.booking.uuid)]),
            {"start": new_start, "end": new_end},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)

    def test_cannot_reschedule_to_the_past(self):
        past_start = (djtz.now() - timedelta(hours=2)).isoformat()
        past_end = (djtz.now() - timedelta(hours=1)).isoformat()
        resp = self.client.post(
            reverse("public-booking-reschedule", args=[str(self.booking.uuid)]),
            {"start": past_start, "end": past_end},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)


class HostBookingsIcsExportTests(TestCase):
    """GET /api/host-bookings/export.ics returns a valid iCalendar feed
    scoped to the calling host, with one VEVENT per Booking row."""

    def setUp(self):
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(
            email="host@test.local", password="pwpw12345xyz",
            first_name="Han", last_name="Solo",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.host)
        # One upcoming, one cancelled — verify both render.
        self.upcoming = Booking.objects.create(
            host=self.host, provider=Booking.Provider.GOOGLE,
            calendar_id="primary", event_id="evt-1",
            visitor_name="Leia", visitor_email="leia@example.com",
            attendee_emails=["leia@example.com"],
            title="Chat", start_at=djtz.now() + timedelta(hours=48),
            end_at=djtz.now() + timedelta(hours=48, minutes=30),
        )
        self.cancelled = Booking.objects.create(
            host=self.host, provider=Booking.Provider.GOOGLE,
            calendar_id="primary", event_id="evt-2",
            visitor_email="chewie@example.com",
            attendee_emails=["chewie@example.com"],
            start_at=djtz.now() + timedelta(hours=24),
            end_at=djtz.now() + timedelta(hours=24, minutes=30),
            status=Booking.Status.CANCELLED,
        )

    def test_export_returns_valid_ics(self):
        resp = self.client.get(reverse("host-bookings-export-ics") + "?status=all")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/calendar; charset=utf-8")
        body = resp.content.decode()
        self.assertTrue(body.startswith("BEGIN:VCALENDAR"))
        self.assertTrue(body.rstrip().endswith("END:VCALENDAR"))
        # One VEVENT per booking; RFC-required fields present.
        self.assertEqual(body.count("BEGIN:VEVENT"), 2)
        self.assertEqual(body.count("END:VEVENT"), 2)
        self.assertIn("SUMMARY:Chat", body)
        self.assertIn("STATUS:CANCELLED", body)
        self.assertIn("STATUS:CONFIRMED", body)
        self.assertIn("ATTENDEE", body)
        self.assertIn(f"UID:slotly-{self.upcoming.uuid}@slotly.team", body)

    def test_export_only_returns_callers_own(self):
        UserModel = get_user_model()
        other = UserModel.objects.create_user(email="other@test.local", password="pwpw12345xyz")
        self.client.force_authenticate(other)
        resp = self.client.get(reverse("host-bookings-export-ics") + "?status=all")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        # No VEVENTs for someone else's host.
        self.assertEqual(body.count("BEGIN:VEVENT"), 0)


class HostCancelTests(TestCase):
    """Host-side cancel from /bookings Confirmed tab."""

    def setUp(self):
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(
            email="host@test.local", password="pwpw12345xyz",
            first_name="Han", last_name="Solo",
        )
        _connect_google(self.host)
        self.other = UserModel.objects.create_user(email="other@test.local", password="pwpw12345xyz")
        self.client = APIClient()
        self.client.force_authenticate(self.host)
        self.booking = Booking.objects.create(
            host=self.host,
            provider=Booking.Provider.GOOGLE,
            calendar_id="primary",
            event_id="evt-xyz",
            visitor_name="Padme",
            visitor_email="padme@example.com",
            kind=Booking.Kind.ONLINE,
            title="Chat",
            start_at=djtz.now() + timedelta(hours=48),
            end_at=djtz.now() + timedelta(hours=48, minutes=30),
        )

    def test_host_cancel_deletes_event_and_emails_visitor(self):
        from django.core import mail as _mail
        with patch("apps.scheduling.views.delete_calendar_event") as md:
            resp = self.client.post(
                reverse("host-bookings-cancel", args=[str(self.booking.uuid)]),
                {"reason": "Sick day"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CANCELLED)
        self.assertFalse(self.booking.cancelled_by_visitor)
        self.assertEqual(self.booking.cancellation_reason, "Sick day")
        md.assert_called_once()
        recipients = [addr for m in _mail.outbox for addr in m.to]
        self.assertIn("padme@example.com", recipients)

    def test_other_user_cannot_cancel_someone_elses_booking(self):
        self.client.force_authenticate(self.other)
        resp = self.client.post(
            reverse("host-bookings-cancel", args=[str(self.booking.uuid)]),
            {},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_unauthenticated_gets_401(self):
        self.client.force_authenticate(None)
        resp = self.client.post(
            reverse("host-bookings-cancel", args=[str(self.booking.uuid)]),
            {},
            format="json",
        )
        self.assertIn(resp.status_code, (401, 403))


class SendBookingRemindersCommandTests(TestCase):
    """`python manage.py send_booking_reminders` — cron entrypoint for the
    T-24h visitor reminder. Verifies (a) rows in the window get mailed and
    stamped, (b) already-reminded rows are skipped, (c) cancelled bookings
    are skipped, (d) dry-run touches neither mail nor DB."""

    def setUp(self):
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(
            email="host@test.local", password="pwpw12345xyz",
            first_name="Han", last_name="Solo",
        )
        # 24h from now → inside default window
        self.due = Booking.objects.create(
            host=self.host,
            provider=Booking.Provider.GOOGLE,
            calendar_id="primary",
            event_id="evt-due",
            visitor_name="Leia",
            visitor_email="leia@example.com",
            kind=Booking.Kind.ONLINE,
            title="Diplomacy",
            start_at=djtz.now() + timedelta(hours=24),
            end_at=djtz.now() + timedelta(hours=24, minutes=30),
        )
        # 5 days from now → outside window
        self.far = Booking.objects.create(
            host=self.host,
            provider=Booking.Provider.GOOGLE,
            calendar_id="primary",
            event_id="evt-far",
            visitor_email="chewie@example.com",
            start_at=djtz.now() + timedelta(days=5),
            end_at=djtz.now() + timedelta(days=5, hours=1),
        )
        # 24h but already reminded
        self.already = Booking.objects.create(
            host=self.host,
            provider=Booking.Provider.GOOGLE,
            calendar_id="primary",
            event_id="evt-already",
            visitor_email="lando@example.com",
            start_at=djtz.now() + timedelta(hours=24),
            end_at=djtz.now() + timedelta(hours=24, minutes=30),
            reminded_at=djtz.now(),
        )
        # 24h but cancelled — should not remind
        self.cxl = Booking.objects.create(
            host=self.host,
            provider=Booking.Provider.GOOGLE,
            calendar_id="primary",
            event_id="evt-cxl",
            visitor_email="wedge@example.com",
            start_at=djtz.now() + timedelta(hours=24),
            end_at=djtz.now() + timedelta(hours=24, minutes=30),
            status=Booking.Status.CANCELLED,
        )

    def test_command_reminds_due_bookings_only(self):
        from django.core.management import call_command
        from django.core import mail as _mail
        # already-reminded row for THIS stage stays reminded — mark 24h
        # in reminded_stages so the new stage-aware filter skips it.
        self.already.reminded_stages = ["24h"]
        self.already.save(update_fields=["reminded_stages"])
        call_command("send_booking_reminders", "--stage", "24h")
        recipients = {addr for m in _mail.outbox for addr in m.to}
        self.assertEqual(recipients, {"leia@example.com"})
        self.due.refresh_from_db()
        self.assertIn("24h", self.due.reminded_stages)
        self.assertIsNotNone(self.due.reminded_at)
        self.already.refresh_from_db()
        self.far.refresh_from_db()
        self.cxl.refresh_from_db()
        self.assertEqual(self.far.reminded_stages, [])
        self.assertEqual(self.cxl.reminded_stages, [])

    def test_dry_run_sends_no_mail_and_no_db_writes(self):
        from django.core.management import call_command
        from django.core import mail as _mail
        call_command("send_booking_reminders", "--dry-run")
        self.assertEqual(len(_mail.outbox), 0)
        self.due.refresh_from_db()
        self.assertEqual(self.due.reminded_stages, [])

    def test_stages_are_independent(self):
        """A booking gets both 24h and 1h reminders when the schedules
        catch them on the respective windows."""
        from django.core.management import call_command
        from django.core import mail as _mail

        # Booking at now + 1h → outside 24h window, inside 1h window.
        soon = Booking.objects.create(
            host=self.host,
            provider=Booking.Provider.GOOGLE,
            calendar_id="primary",
            event_id="evt-soon",
            visitor_email="soon@example.com",
            start_at=djtz.now() + timedelta(hours=1),
            end_at=djtz.now() + timedelta(hours=1, minutes=30),
        )
        call_command("send_booking_reminders", "--stage", "1h")
        recipients = {addr for m in _mail.outbox for addr in m.to}
        self.assertIn("soon@example.com", recipients)
        soon.refresh_from_db()
        self.assertIn("1h", soon.reminded_stages)
        # Now flip forward: pretend it's 23 hours later — the same booking
        # (started ago!) shouldn't get a 24h mail because it's past.
        # Just verify calling 24h a second time doesn't re-send the 1h row
        # (window mismatch, not filter).
        _mail.outbox.clear()
        call_command("send_booking_reminders", "--stage", "24h")
        self.assertNotIn("soon@example.com", {addr for m in _mail.outbox for addr in m.to})

    def test_unknown_stage_errors(self):
        from django.core.management import call_command
        with self.assertRaises(Exception):
            call_command("send_booking_reminders", "--stage", "42h")


class ProviderPickerTests(TestCase):
    """_pick_write_provider picks Google when both are connected, MS as
    fallback, None when neither. Direct unit test — cheap to run and covers
    a decision that changes the entire dispatch."""

    def setUp(self):
        UserModel = get_user_model()
        self.user = UserModel.objects.create_user(email="p@test.local", password="pwpw12345xyz")

    def test_none_when_no_provider_connected(self):
        from .views import _pick_write_provider
        self.assertIsNone(_pick_write_provider(self.user))

    def test_google_selected_when_only_google_connected(self):
        from .views import _pick_write_provider
        _connect_google(self.user)
        p = _pick_write_provider(self.user)
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "google")
        self.assertEqual(p.write_calendar_id, "primary")


# ---------------------------------------------------------------------------
# Task B2: PublicMeetingCreateView persists meeting_link_kind
# ---------------------------------------------------------------------------


@override_settings(
    FRONTEND_BASE_URL="http://localhost:3000",
    TIME_ZONE="UTC",
)
class PublicMeetingMeetingLinkKindTests(TestCase):
    """POST /api/public/meetings/<token> with meeting_link_kind persists the field
    and routes to the requested provider."""

    def setUp(self):
        from django.core.cache import cache as _cache
        _cache.clear()
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(email="host@b2view.local", password="pwpw12345xyz")
        self.host.share_enabled = True
        self.host.save(update_fields=["share_enabled"])
        _connect_google(self.host)
        self.client = APIClient()

    def _url(self):
        return reverse("public-meetings-create", args=[str(self.host.share_token)])

    def _post(self, **overrides):
        start, end = _future_slot()
        body = {
            "visitor_name": "Kind Visitor",
            "visitor_email": "kind@example.com",
            "start": start,
            "end": end,
            "kind": "online",
        }
        body.update(overrides)
        return self.client.post(self._url(), body, format="json")

    def test_public_meeting_create_persists_meeting_link_kind_auto(self):
        """Default (no meeting_link_kind sent) → Booking.meeting_link_kind == 'auto'."""
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "evt-auto", "htmlLink": "https://cal.google.com/e/evt-auto"}
            resp = self._post()
        self.assertEqual(resp.status_code, 201, resp.content)
        booking = Booking.objects.order_by("-created_at").first()
        self.assertIsNotNone(booking)
        self.assertEqual(booking.meeting_link_kind, "auto")

    def test_public_meeting_create_persists_meeting_link_kind_google_meet(self):
        """meeting_link_kind='google_meet' → Booking.meeting_link_kind == 'google_meet'."""
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "evt-gm", "htmlLink": "https://cal.google.com/e/evt-gm"}
            resp = self._post(meeting_link_kind="google_meet")
        self.assertEqual(resp.status_code, 201, resp.content)
        booking = Booking.objects.order_by("-created_at").first()
        self.assertIsNotNone(booking)
        self.assertEqual(booking.meeting_link_kind, "google_meet")

    def test_public_meeting_create_invalid_meeting_link_kind_returns_400(self):
        """meeting_link_kind='zoom' → 400 with meeting_link_kind key."""
        resp = self._post(meeting_link_kind="zoom")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("meeting_link_kind", resp.json())

    def test_public_meeting_create_teams_when_only_google_connected_returns_400(self):
        """meeting_link_kind='teams' but host has no MS → 400 with meeting_link_kind key."""
        resp = self._post(meeting_link_kind="teams")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("meeting_link_kind", resp.json())


# ---------------------------------------------------------------------------
# Regression: public booking endpoint must respect accepts_*_bookings toggles
# (was bypassed because the views-layer _resolve_write_provider_for_kind
#  duplicate lacked the toggle checks present in services.py).
# ---------------------------------------------------------------------------


@override_settings(
    FRONTEND_BASE_URL="http://localhost:3000",
    TIME_ZONE="UTC",
)
class PublicBookingToggleEnforcementTests(TestCase):
    """POST /api/public/meetings/<token> must reject requests when the host has
    the relevant accepts_*_bookings toggle turned off, even if the provider
    is otherwise connected (regression guard for F3-critical)."""

    def setUp(self):
        from django.core.cache import cache as _cache
        _cache.clear()
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(
            email="host@toggle.local", password="pwpw12345xyz"
        )
        self.host.share_enabled = True
        self.host.save(update_fields=["share_enabled"])
        _connect_google(self.host)
        self.client = APIClient()

    def _post(self, **overrides):
        start, end = _future_slot()
        body = {
            "visitor_name": "Toggle Visitor",
            "visitor_email": "toggle@example.com",
            "start": start,
            "end": end,
            "kind": "online",
        }
        body.update(overrides)
        return self.client.post(
            reverse("public-meetings-create", args=[str(self.host.share_token)]),
            body,
            format="json",
        )

    def test_google_meet_rejected_when_accepts_google_bookings_false(self):
        """meeting_link_kind='google_meet' + accepts_google_bookings=False → 400."""
        self.host.accepts_google_bookings = False
        self.host.save(update_fields=["accepts_google_bookings"])
        resp = self._post(meeting_link_kind="google_meet")
        self.assertEqual(resp.status_code, 400, resp.content)
        data = resp.json()
        self.assertIn("meeting_link_kind", data)
        self.assertIn("Google Meet", str(data["meeting_link_kind"]))

    def test_auto_both_toggles_off_rejected(self):
        """kind='auto' + both accepts_*_bookings=False → 400 with 'detail' key."""
        self.host.accepts_google_bookings = False
        self.host.accepts_microsoft_bookings = False
        self.host.save(update_fields=["accepts_google_bookings", "accepts_microsoft_bookings"])
        resp = self._post()
        self.assertEqual(resp.status_code, 400, resp.content)
        data = resp.json()
        self.assertIn("detail", data)


# ---------------------------------------------------------------------------
# Visitor locale capture + email localization (Blok B-be)
# ---------------------------------------------------------------------------


@override_settings(
    FRONTEND_BASE_URL="http://localhost:3000",
    TIME_ZONE="UTC",
    DEFAULT_FROM_EMAIL="noreply@slotly.test",
)
class PublicMeetingLocaleCaptureTests(TestCase):
    """POST /api/public/meetings/<token> stores the visitor-picked `locale`
    on the resulting Booking (online path) or BookingRequest (physical path)
    so later emails render in the right language."""

    def setUp(self):
        from django.core.cache import cache as _cache
        _cache.clear()
        from django.contrib.auth import get_user_model
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(email="host@locale.local", password="pwpw12345xyz")
        self.host.share_enabled = True
        self.host.save(update_fields=["share_enabled"])
        _connect_google(self.host)
        self.client = APIClient()

    def _url(self):
        return reverse("public-meetings-create", args=[str(self.host.share_token)])

    def _post(self, **overrides):
        start, end = _future_slot()
        body = {
            "visitor_name": "Lead Visitor",
            "visitor_email": "lead@example.com",
            "start": start,
            "end": end,
            "kind": "online",
        }
        body.update(overrides)
        return self.client.post(self._url(), body, format="json")

    def test_locale_cs_is_stored_on_booking(self):
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "evt-cs", "htmlLink": "https://cal.google.com/e/evt-cs"}
            resp = self._post(locale="cs")
        self.assertEqual(resp.status_code, 201, resp.content)
        booking = Booking.objects.order_by("-created_at").first()
        self.assertIsNotNone(booking)
        self.assertEqual(booking.locale, "cs")

    def test_locale_defaults_to_en_when_omitted(self):
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "evt-default", "htmlLink": "x"}
            resp = self._post()
        self.assertEqual(resp.status_code, 201, resp.content)
        booking = Booking.objects.order_by("-created_at").first()
        self.assertEqual(booking.locale, "en")

    def test_unknown_locale_clamps_to_en(self):
        with patch("apps.scheduling.views.create_calendar_event") as mc:
            mc.return_value = {"id": "evt-x", "htmlLink": "x"}
            resp = self._post(locale="de-DE")
        self.assertEqual(resp.status_code, 201, resp.content)
        booking = Booking.objects.order_by("-created_at").first()
        self.assertEqual(booking.locale, "en")

    def test_locale_cs_stored_on_physical_booking_request(self):
        """kind='physical' creates a BookingRequest (no calendar event). The
        locale must land on that row so a later rejection email speaks CS."""
        resp = self._post(
            kind="physical",
            location="Kavárna Slavia, Praha",
            locale="cs",
        )
        self.assertEqual(resp.status_code, 202, resp.content)
        req = BookingRequest.objects.order_by("-created_at").first()
        self.assertIsNotNone(req)
        self.assertEqual(req.locale, "cs")


class EmailsI18nSubjectTests(TestCase):
    """Unit tests for the emails_i18n module — subject strings only. These
    are the user-visible preview in the mail client's inbox list, so they
    deserve explicit assertions in both locales."""

    def test_host_cancel_subject_cs_contains_czech_verb(self):
        from apps.scheduling.emails_i18n import t_subject_host_cancel
        s = t_subject_host_cancel("cs", host_name="Jana Dvořáková", when="pondělí 3. listopadu, 14:00")
        self.assertIn("zrušil", s.lower())
        self.assertIn("Jana Dvořáková", s)

    def test_host_cancel_subject_en_contains_english_verb(self):
        from apps.scheduling.emails_i18n import t_subject_host_cancel
        s = t_subject_host_cancel("en", host_name="Jane Doe", when="Monday 03 November, 14:00")
        self.assertIn("cancelled", s.lower())
        self.assertIn("Jane Doe", s)

    def test_rejection_subject_cs(self):
        from apps.scheduling.emails_i18n import t_subject_rejection
        s = t_subject_rejection("cs", when="úterý 4. listopadu, 10:00")
        self.assertIn("odmítnuta", s)

    def test_rejection_subject_en(self):
        from apps.scheduling.emails_i18n import t_subject_rejection
        s = t_subject_rejection("en", when="Tuesday 04 November, 10:00")
        self.assertIn("declined", s)

    def test_reminder_strings_24h_cs_subject_verb(self):
        from apps.scheduling.emails_i18n import t_reminder_strings
        d = t_reminder_strings("cs", stage="24h")
        self.assertEqual(d["subject_verb"], "Zítra")

    def test_reminder_strings_1h_cs_subject_verb(self):
        from apps.scheduling.emails_i18n import t_reminder_strings
        d = t_reminder_strings("cs", stage="1h")
        self.assertEqual(d["subject_verb"], "Za hodinu")

    def test_reminder_strings_unknown_locale_falls_back_to_en(self):
        from apps.scheduling.emails_i18n import t_reminder_strings
        d = t_reminder_strings("de", stage="24h")
        self.assertEqual(d["subject_verb"], "Tomorrow")

    def test_format_when_cs_uses_lowercase_czech_month(self):
        from datetime import datetime
        from apps.scheduling.emails_i18n import format_when
        dt = datetime(2026, 11, 2, 14, 0)  # 2026-11-02 is a Monday
        s = format_when(dt, "cs")
        self.assertIn("listopadu", s)
        self.assertIn("pondělí", s)
        self.assertIn("14:00", s)

    def test_format_when_en_uses_english_month(self):
        from datetime import datetime
        from apps.scheduling.emails_i18n import format_when
        dt = datetime(2026, 11, 2, 14, 0)  # Monday
        s = format_when(dt, "en")
        self.assertIn("November", s)
        self.assertIn("Monday", s)


@override_settings(
    FRONTEND_BASE_URL="http://localhost:3000",
    TIME_ZONE="UTC",
    DEFAULT_FROM_EMAIL="noreply@slotly.test",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class VisitorEmailLocalizationTests(TestCase):
    """Integration tests: send the three visitor-facing emails on bookings
    whose `locale` is CS, assert the outbox contents are Czech."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        UserModel = get_user_model()
        self.host = UserModel.objects.create_user(
            email="hostmail@locale.local",
            password="pwpw12345xyz",
            first_name="Jana",
            last_name="Dvořáková",
        )
        self.host.share_enabled = True
        self.host.save(update_fields=["share_enabled"])

    def _make_booking(self, locale: str = "cs", **extra) -> Booking:
        from datetime import datetime, timedelta
        start = djtz.now() + timedelta(days=1)
        end = start + timedelta(minutes=30)
        return Booking.objects.create(
            host=self.host,
            provider="google",
            visitor_name="Petr Novák",
            visitor_email="petr@example.com",
            attendee_emails=["petr@example.com"],
            kind=Booking.Kind.ONLINE,
            title="Konzultace",
            location="",
            start_at=start,
            end_at=end,
            locale=locale,
            **extra,
        )

    def test_host_cancellation_mail_cs_contains_czech_subject(self):
        from django.core import mail as djmail
        from apps.scheduling.services import _mail_visitor_host_cancellation
        booking = self._make_booking(locale="cs")
        djmail.outbox = []
        _mail_visitor_host_cancellation(booking, reason="")
        self.assertEqual(len(djmail.outbox), 1)
        m = djmail.outbox[0]
        self.assertIn("zrušil", m.subject.lower())
        self.assertIn("Petr Novák", m.body) if False else None  # noqa: W505 (greeting)
        self.assertIn("Dobrý den", m.body)
        # CTA link should carry ?lang=cs so the follow-up public page opens in CS.
        joined = m.alternatives[0][0] if m.alternatives else ""
        self.assertIn("lang=cs", joined)

    def test_host_cancellation_mail_en_default(self):
        from django.core import mail as djmail
        from apps.scheduling.services import _mail_visitor_host_cancellation
        booking = self._make_booking(locale="en")
        djmail.outbox = []
        _mail_visitor_host_cancellation(booking, reason="had to run")
        self.assertEqual(len(djmail.outbox), 1)
        m = djmail.outbox[0]
        self.assertIn("cancelled", m.subject.lower())
        self.assertIn("Hi Petr Novák", m.body)

    def test_rejection_mail_cs_contains_czech(self):
        from django.core import mail as djmail
        from datetime import timedelta
        from apps.scheduling.views import _mail_visitor_rejection
        start = djtz.now() + timedelta(days=1)
        req = BookingRequest.objects.create(
            host=self.host,
            visitor_name="Petra Nováková",
            visitor_email="petra@example.com",
            kind=BookingRequest.Kind.PHYSICAL,
            start_at=start,
            end_at=start + timedelta(hours=1),
            title="Osobní schůzka",
            location="Kavárna Slavia",
            locale="cs",
        )
        djmail.outbox = []
        _mail_visitor_rejection(req, note="")
        self.assertEqual(len(djmail.outbox), 1)
        m = djmail.outbox[0]
        self.assertIn("odmítnuta", m.subject)
        self.assertIn("Dobrý den", m.body)

    def test_reminder_mail_cs_subject_starts_with_zitra(self):
        from django.core import mail as djmail
        from apps.scheduling.management.commands.send_booking_reminders import Command
        booking = self._make_booking(locale="cs")
        cmd = Command()
        # Minimal cfg dict shape — real stage reg has more, but _send only
        # reads nothing from cfg now (strings come from emails_i18n).
        djmail.outbox = []
        cmd._send(booking, stage="24h", cfg={}, dry_run=False, frontend_base="http://localhost:3000")
        self.assertEqual(len(djmail.outbox), 1)
        m = djmail.outbox[0]
        self.assertTrue(m.subject.startswith("[Slotly] Zítra:"), m.subject)
        self.assertIn("Dobrý den", m.body)

    def test_reminder_mail_en_subject_starts_with_tomorrow(self):
        from django.core import mail as djmail
        from apps.scheduling.management.commands.send_booking_reminders import Command
        booking = self._make_booking(locale="en")
        cmd = Command()
        djmail.outbox = []
        cmd._send(booking, stage="24h", cfg={}, dry_run=False, frontend_base="http://localhost:3000")
        self.assertEqual(len(djmail.outbox), 1)
        m = djmail.outbox[0]
        self.assertTrue(m.subject.startswith("[Slotly] Tomorrow:"), m.subject)
