"""
Tests for the `prompt` parameter in authorize_url for Google and Microsoft.

Both IdPs support space-separated multi-value prompt strings:
  - Google: https://developers.google.com/identity/protocols/oauth2/web-server#creatingclient
  - Microsoft v2.0: same syntax (space-separated values in a single prompt param)

We assert that both `select_account` and `consent` appear in the `prompt` value.
`select_account` forces the account picker on reconnect so users adding a second
account are never silently re-linked to the existing one.
`consent` is kept alongside it (space-separated) to guarantee a refresh_token on
every (re-)authorization.

Note: if the user has only one account in their browser session, the IdP still
shows the picker with that single option — this test documents the URL contract,
not the browser-side rendering behavior.
"""
from urllib.parse import parse_qs, urlparse

from django.test import TestCase, override_settings


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="test-google-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="test-google-client-secret",
    GOOGLE_OAUTH_REDIRECT_URI="http://localhost:8000/api/oauth/google/callback",
    GOOGLE_OAUTH_SCOPES=["openid", "email", "https://www.googleapis.com/auth/calendar"],
)
class GoogleAuthorizeUrlPromptTests(TestCase):
    def test_prompt_includes_select_account(self):
        """Google authorize URL must carry select_account in the prompt param."""
        from apps.scheduling.google_client import authorize_url

        url = authorize_url("test_state_google")
        qs = parse_qs(urlparse(url).query)
        prompt_value = qs["prompt"][0]
        self.assertIn(
            "select_account",
            prompt_value,
            f"Expected 'select_account' in prompt; got: {prompt_value!r}",
        )

    def test_prompt_includes_consent(self):
        """Google authorize URL must still carry consent to guarantee refresh_token."""
        from apps.scheduling.google_client import authorize_url

        url = authorize_url("test_state_google")
        qs = parse_qs(urlparse(url).query)
        prompt_value = qs["prompt"][0]
        self.assertIn(
            "consent",
            prompt_value,
            f"Expected 'consent' in prompt; got: {prompt_value!r}",
        )

    def test_prompt_is_single_param(self):
        """Both values should be in one `prompt` param, not two separate ones."""
        from apps.scheduling.google_client import authorize_url

        url = authorize_url("test_state_google")
        qs = parse_qs(urlparse(url).query)
        self.assertEqual(
            len(qs["prompt"]),
            1,
            "prompt should appear exactly once in the query string",
        )


@override_settings(
    MICROSOFT_OAUTH_CLIENT_ID="test-ms-client-id",
    MICROSOFT_OAUTH_CLIENT_SECRET="test-ms-client-secret",
    MICROSOFT_OAUTH_REDIRECT_URI="http://localhost:8000/api/oauth/microsoft/callback",
    MICROSOFT_OAUTH_SCOPES=[
        "openid",
        "email",
        "profile",
        "offline_access",
        "Calendars.ReadWrite",
    ],
)
class MicrosoftAuthorizeUrlPromptTests(TestCase):
    def test_prompt_includes_select_account(self):
        """Microsoft authorize URL must carry select_account in the prompt param."""
        from apps.scheduling.graph_client import authorize_url

        url = authorize_url("test_state_ms")
        qs = parse_qs(urlparse(url).query)
        prompt_value = qs["prompt"][0]
        self.assertIn(
            "select_account",
            prompt_value,
            f"Expected 'select_account' in prompt; got: {prompt_value!r}",
        )

    def test_prompt_includes_consent(self):
        """Microsoft authorize URL must still carry consent to guarantee refresh_token."""
        from apps.scheduling.graph_client import authorize_url

        url = authorize_url("test_state_ms")
        qs = parse_qs(urlparse(url).query)
        prompt_value = qs["prompt"][0]
        self.assertIn(
            "consent",
            prompt_value,
            f"Expected 'consent' in prompt; got: {prompt_value!r}",
        )

    def test_prompt_is_single_param(self):
        """Both values should be in one `prompt` param, not two separate ones."""
        from apps.scheduling.graph_client import authorize_url

        url = authorize_url("test_state_ms")
        qs = parse_qs(urlparse(url).query)
        self.assertEqual(
            len(qs["prompt"]),
            1,
            "prompt should appear exactly once in the query string",
        )
