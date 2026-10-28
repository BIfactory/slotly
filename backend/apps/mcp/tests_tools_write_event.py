"""
Tests for T4: slotly.create_event (bare calendar event, no Booking row).

Covers:
  - Happy path (valid params, mocked provider)
  - No Booking row created (regression guard)
  - Attendees passed through to provider
  - Custom calendar_id honoured
  - Scope enforcement: write_event scope required; create_booking scope denied
  - Wildcard scope allows
  - Input validation (missing title, empty title, naive datetime, too many attendees, invalid email)
  - Duration bounds: < 5 min → 400; > 24 h → 400
  - Idempotency key deduplicated
  - Write-tier rate limit (20/min) applies
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone as djtz
from rest_framework.test import APITestCase

from apps.accounts.models import User

from .models import APIToken, MCPCall


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _future_slot(hours_ahead: int = 24, duration_min: int = 30):
    """Return (start_iso, end_iso) tuple well into the future with UTC tz."""
    start = djtz.now() + timedelta(hours=hours_ahead)
    end = start + timedelta(minutes=duration_min)
    return start.isoformat(), end.isoformat()


def _mock_provider(event_id: str = "evt-t4") -> MagicMock:
    """Return a mock _WriteProvider simulating a successful Google insert."""
    provider = MagicMock()
    provider.name = "google"
    provider.write_calendar_id = "primary"
    provider.create_event.return_value = {
        "id": event_id,
        "htmlLink": f"https://calendar.google.com/event/{event_id}",
        "hangoutLink": None,
    }
    return provider


# ---------------------------------------------------------------------------
# Happy-path and core behaviour
# ---------------------------------------------------------------------------

@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    FRONTEND_BASE_URL="http://localhost:3000",
    TIME_ZONE="UTC",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class CreateEventTests(APITestCase):

    def setUp(self):
        cache.clear()
        self.host = User.objects.create_user(
            email="alice@t4.local", password="pass", first_name="Alice", last_name="T4"
        )
        self.token, self.raw = APIToken.create_for_user(
            self.host, "T4-write-event", ["write_event"]
        )

    def _call(self, params, *, token_raw=None, idem_key=None):
        raw = token_raw or self.raw
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        body = {"name": "slotly.create_event", "params": params}
        if idem_key is not None:
            body["idempotency_key"] = idem_key
        return self.client.post("/mcp/v1/tools/call", body, format="json")

    # 1
    def test_create_event_happy_path(self):
        """Valid params + mocked provider → 200, status=ok, event.id present."""
        start_iso, end_iso = _future_slot()
        provider = _mock_provider("happy-t4")

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            resp = self._call({"start": start_iso, "end": end_iso, "title": "Team Sync"})

        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["event"]["id"], "happy-t4")
        self.assertEqual(result["event"]["calendar_id"], "primary")
        self.assertEqual(result["invited_emails"], [])

        # No Booking row created (regression guard — F15 bare event has no Booking).
        from apps.scheduling.models import Booking
        self.assertEqual(Booking.objects.count(), 0)

        # Audit row written with status=ok.
        call_row = MCPCall.objects.filter(
            capability="slotly.create_event", status="ok"
        ).first()
        self.assertIsNotNone(call_row)

    # 2
    def test_create_event_with_attendees(self):
        """attendee_emails → 200; emails echoed in invited_emails; provider called with them."""
        start_iso, end_iso = _future_slot()
        provider = _mock_provider("att-t4")

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            resp = self._call({
                "start": start_iso,
                "end": end_iso,
                "title": "Invite meeting",
                "attendee_emails": ["a@example.com", "b@example.com"],
            })

        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertIn("a@example.com", result["invited_emails"])
        self.assertIn("b@example.com", result["invited_emails"])

        # Provider.create_event was called once with attendees list.
        provider.create_event.assert_called_once()
        call_kwargs = provider.create_event.call_args
        # The service passes attendees= as a keyword arg
        attendees_passed = call_kwargs.kwargs.get("attendees") or call_kwargs[1].get("attendees")
        self.assertIn("a@example.com", attendees_passed)
        self.assertIn("b@example.com", attendees_passed)

    # 3
    def test_create_event_custom_calendar_id(self):
        """calendar_id param honoured: provider called with it; response echoes it."""
        start_iso, end_iso = _future_slot()
        provider = _mock_provider("cal-t4")

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            resp = self._call({
                "start": start_iso,
                "end": end_iso,
                "title": "Custom Cal",
                "calendar_id": "cal_xyz",
            })

        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertEqual(result["event"]["calendar_id"], "cal_xyz")

        # Provider.create_event called with calendar_id=cal_xyz
        provider.create_event.assert_called_once()
        call_kwargs = provider.create_event.call_args
        cal_id_passed = call_kwargs.kwargs.get("calendar_id") or call_kwargs[1].get("calendar_id")
        self.assertEqual(cal_id_passed, "cal_xyz")

    # 4
    def test_create_event_scope_denied(self):
        """Token with create_booking scope only → 403 scope_denied."""
        _, raw2 = APIToken.create_for_user(self.host, "T4-wrong-scope", ["create_booking"])
        start_iso, end_iso = _future_slot()
        resp = self._call(
            {"start": start_iso, "end": end_iso, "title": "No scope"},
            token_raw=raw2,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"], "scope_denied")

    # 5
    def test_create_event_scope_write_event_allows(self):
        """Token with write_event scope → 200 (alias resolved correctly)."""
        _, raw_we = APIToken.create_for_user(self.host, "T4-write-event-ok", ["write_event"])
        start_iso, end_iso = _future_slot()
        provider = _mock_provider("scope-t4")

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            resp = self._call(
                {"start": start_iso, "end": end_iso, "title": "Scope OK"},
                token_raw=raw_we,
            )

        self.assertEqual(resp.status_code, 200)

    # 6
    def test_create_event_scope_wildcard_allows(self):
        """Token with * scope → 200."""
        _, raw_star = APIToken.create_for_user(self.host, "T4-star", ["*"])
        start_iso, end_iso = _future_slot()
        provider = _mock_provider("wild-t4")

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            resp = self._call(
                {"start": start_iso, "end": end_iso, "title": "Wildcard"},
                token_raw=raw_star,
            )

        self.assertEqual(resp.status_code, 200)

    # 7
    def test_create_event_missing_title(self):
        """No title field → 400 validation_error."""
        start_iso, end_iso = _future_slot()
        resp = self._call({"start": start_iso, "end": end_iso})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # 8
    def test_create_event_empty_title(self):
        """title="" (whitespace only) → 400 validation_error."""
        start_iso, end_iso = _future_slot()
        resp = self._call({"start": start_iso, "end": end_iso, "title": "   "})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # 9
    def test_create_event_duration_too_short(self):
        """Duration < 5 min → 400 (service enforces 5 min minimum)."""
        start = djtz.now() + timedelta(hours=24)
        end = start + timedelta(minutes=4)  # only 4 minutes
        resp = self._call({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "title": "Too short",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # 10
    def test_create_event_duration_too_long(self):
        """Duration > 24 h → 400 (service enforces 24 h maximum)."""
        start = djtz.now() + timedelta(hours=24)
        end = start + timedelta(hours=25)  # 25 hours
        provider = _mock_provider()
        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            resp = self._call({
                "start": start.isoformat(),
                "end": end.isoformat(),
                "title": "Too long",
            })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # 11
    def test_create_event_naive_start(self):
        """start without timezone offset → 400 (must include timezone)."""
        resp = self._call({
            "start": "2027-06-01T10:00:00",  # no tz offset
            "end": "2027-06-01T10:30:00+00:00",
            "title": "Naive start",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # 12
    def test_create_event_too_many_attendees(self):
        """51 attendee_emails → 400 validation_error."""
        start_iso, end_iso = _future_slot()
        many_emails = [f"user{i}@example.com" for i in range(51)]
        resp = self._call({
            "start": start_iso,
            "end": end_iso,
            "title": "Too many",
            "attendee_emails": many_emails,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # 13
    def test_create_event_invalid_email(self):
        """attendee_emails with various invalid values → 400 validation_error."""
        start_iso, end_iso = _future_slot()
        bad_emails = [
            ["not-an-email"],   # original case: no @ at all
            ["@"],              # both local and domain empty
            ["x@"],             # domain missing
            ["@y"],             # local missing
            ["a@b@c"],          # multiple @
        ]
        for emails in bad_emails:
            with self.subTest(emails=emails):
                resp = self._call({
                    "start": start_iso,
                    "end": end_iso,
                    "title": "Bad email",
                    "attendee_emails": emails,
                })
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.json()["error"], "validation_error")

    # 13b
    def test_create_event_title_too_long(self):
        """title longer than 200 chars → 400 validation_error."""
        start_iso, end_iso = _future_slot()
        with patch("apps.scheduling.services._pick_write_provider", return_value=_mock_provider()):
            resp = self._call({
                "start": start_iso,
                "end": end_iso,
                "title": "x" * 201,
            })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # 14
    def test_create_event_idempotency_hit(self):
        """Same idempotency_key twice → handler called once; second is a cache hit."""
        start_iso, end_iso = _future_slot()
        provider = _mock_provider("idem-t4")

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            r1 = self._call(
                {"start": start_iso, "end": end_iso, "title": "Idem"},
                idem_key="t4-idem-14",
            )
            r2 = self._call(
                {"start": start_iso, "end": end_iso, "title": "Idem"},
                idem_key="t4-idem-14",
            )

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        # Same event id in both responses
        self.assertEqual(
            r1.json()["result"]["event"]["id"],
            r2.json()["result"]["event"]["id"],
        )
        # Provider.create_event called exactly once
        self.assertEqual(provider.create_event.call_count, 1)
        # Second audit row marked as idempotency_hit
        hit = MCPCall.objects.filter(
            capability="slotly.create_event", error_code="idempotency_hit"
        ).first()
        self.assertIsNotNone(hit)
        self.assertEqual(hit.status, "ok")

    # 15
    def test_create_event_rate_limit_write_tier(self):
        """21 calls with same token → 21st returns 429 (write tier = 20/min)."""
        fake_result = {
            "status": "ok",
            "event": {
                "id": "rl-t4",
                "html_link": None,
                "start": "",
                "end": "",
                "provider": "google",
                "calendar_id": "primary",
            },
            "invited_emails": [],
        }
        with patch(
            "apps.scheduling.services.create_event",
            return_value=fake_result,
        ):
            statuses = []
            for _ in range(21):
                start_iso, end_iso = _future_slot()
                resp = self._call({"start": start_iso, "end": end_iso, "title": "RL"})
                statuses.append(resp.status_code)

        self.assertEqual(statuses[:20], [200] * 20, f"Expected 20 x 200, got: {statuses[:20]}")
        self.assertEqual(statuses[20], 429, f"Expected 429 on 21st call, got: {statuses[20]}")


# ---------------------------------------------------------------------------
# auth.has_scope unit test for write_event alias
# ---------------------------------------------------------------------------

class WriteEventScopeTest(TestCase):
    """Verify the _SCOPE_ALIASES entry for create_event → write_event."""

    def setUp(self):
        self.user = User.objects.create_user(email="scope.t4@local", password="pass")

    def test_write_event_scope_grants_create_event(self):
        """Token with write_event scope passes has_scope for slotly.create_event."""
        from .auth import has_scope
        token, _ = APIToken.create_for_user(self.user, "scope-we", ["write_event"])
        self.assertTrue(has_scope(token, "slotly.create_event"))

    def test_create_booking_scope_does_not_grant_create_event(self):
        """Token with create_booking scope is denied slotly.create_event (F15-R4)."""
        from .auth import has_scope
        token, _ = APIToken.create_for_user(self.user, "scope-cb", ["create_booking"])
        self.assertFalse(has_scope(token, "slotly.create_event"))

    def test_write_event_scope_does_not_grant_create_booking(self):
        """Token with write_event scope is denied slotly.create_booking."""
        from .auth import has_scope
        token, _ = APIToken.create_for_user(self.user, "scope-we2", ["write_event"])
        self.assertFalse(has_scope(token, "slotly.create_booking"))

    def test_wildcard_grants_create_event(self):
        """* scope grants slotly.create_event."""
        from .auth import has_scope
        token, _ = APIToken.create_for_user(self.user, "scope-star-t4", ["*"])
        self.assertTrue(has_scope(token, "slotly.create_event"))
