"""Tests for OAuth-driven busy intervals (OAuthBusyWindow model + sync tasks + search integration)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.accounts.models import User
from apps.scheduling.models import GoogleAccount, MicrosoftAccount, OAuthBusyWindow


class OAuthBusyWindowSchemaTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(email="a@x.com", password="x" * 12)
        self.g = GoogleAccount.objects.create(
            user=self.alice,
            google_email="a@gmail.com",
            access_token_encrypted="x",
            refresh_token_encrypted="x",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    def test_create_and_query_by_user(self):
        now = datetime.now(timezone.utc)
        OAuthBusyWindow.objects.create(
            user=self.alice,
            provider="google",
            google_account=self.g,
            calendar_id="primary",
            starts_at=now,
            ends_at=now + timedelta(hours=1),
        )
        self.assertEqual(OAuthBusyWindow.objects.filter(user=self.alice).count(), 1)

    def test_cascade_delete_with_account(self):
        now = datetime.now(timezone.utc)
        OAuthBusyWindow.objects.create(
            user=self.alice,
            provider="google",
            google_account=self.g,
            calendar_id="primary",
            starts_at=now,
            ends_at=now + timedelta(hours=1),
        )
        self.g.delete()
        self.assertEqual(OAuthBusyWindow.objects.count(), 0)

    def test_no_title_field(self):
        """Privacy baseline — model MUST NOT have a title or description field."""
        field_names = {f.name for f in OAuthBusyWindow._meta.get_fields()}
        for forbidden in ("title", "summary", "description", "location", "attendees"):
            self.assertNotIn(
                forbidden,
                field_names,
                f"{forbidden} field would leak privacy — see PRD §5.2 and F15-R9",
            )

    def test_microsoft_cascade_delete(self):
        """OAuthBusyWindow rows for a MicrosoftAccount are deleted when the account is deleted."""
        ms = MicrosoftAccount.objects.create(
            user=self.alice,
            microsoft_email="a@outlook.com",
            access_token_encrypted="x",
            refresh_token_encrypted="x",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        now = datetime.now(timezone.utc)
        OAuthBusyWindow.objects.create(
            user=self.alice,
            provider="microsoft",
            microsoft_account=ms,
            calendar_id="primary",
            starts_at=now,
            ends_at=now + timedelta(hours=1),
        )
        ms.delete()
        self.assertEqual(OAuthBusyWindow.objects.count(), 0)
