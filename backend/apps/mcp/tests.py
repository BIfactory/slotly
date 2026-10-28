from __future__ import annotations

import hashlib

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User

from .models import APIToken, MCPCall


class APITokenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="test@example.com", password="testpass123")

    def test_create_token_returns_raw_once(self):
        token, raw = APIToken.create_for_user(self.user, "Test Token", ["list_teams"])
        self.assertTrue(raw.startswith("sltly_"))
        self.assertEqual(len(raw), 38)
        expected_hash = hashlib.sha256(raw.encode()).hexdigest()
        self.assertEqual(token.token_hash, expected_hash)
        self.assertEqual(token.prefix, raw[:12])

    def test_token_prefix_length_exactly_12(self):
        for i in range(20):
            token, _raw = APIToken.create_for_user(self.user, f"Token {i}", ["list_teams"])
            self.assertEqual(len(token.prefix), 12, f"Token {i} prefix length is not 12: {token.prefix!r}")

    def test_token_hash_uniqueness_enforced(self):
        token, raw = APIToken.create_for_user(self.user, "First Token", ["list_teams"])
        with self.assertRaises(IntegrityError):
            APIToken.objects.create(
                user=self.user,
                name="Duplicate Hash",
                token_hash=token.token_hash,
                prefix="sltly_dup12",
                scopes=["list_teams"],
            )

    def test_is_active_toggles_on_revoked_at(self):
        token, _raw = APIToken.create_for_user(self.user, "Active Token", ["list_teams"])
        self.assertTrue(token.is_active)
        token.revoked_at = timezone.now()
        self.assertFalse(token.is_active)

    def test_mcpcall_row_persists_fields(self):
        token, _raw = APIToken.create_for_user(self.user, "Call Token", ["list_teams"])
        call = MCPCall.objects.create(
            token=token,
            capability="slotly.list_teams",
            status="ok",
            duration_ms=42,
            error_code="",
        )
        fetched = MCPCall.objects.get(pk=call.pk)
        self.assertEqual(fetched.capability, "slotly.list_teams")
        self.assertEqual(fetched.status, "ok")
        self.assertEqual(fetched.duration_ms, 42)
        self.assertEqual(fetched.error_code, "")
        self.assertEqual(fetched.token_id, token.pk)
