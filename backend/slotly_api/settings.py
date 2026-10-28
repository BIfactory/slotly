"""
Django settings for Slotly API.

Reads configuration from environment variables (see .env.example at repo root).
Production hardening (HSTS, secure cookies, etc.) lives in slotly_api/settings_prod.py
when that file is added; the defaults below are dev-safe.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    EMAIL_USE_TLS=(bool, False),
    EMAIL_PORT=(int, 25),
)

for candidate in (BASE_DIR / ".env", REPO_ROOT / ".env"):
    if candidate.exists():
        environ.Env.read_env(str(candidate))
        break

# --- Core ---
SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-only-INSECURE-replace-me")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "allauth",
    "allauth.account",
    "allauth.headless",
    "django_celery_beat",
    "apps.accounts",
    "apps.teams",
    "apps.calendars",
    "apps.availability",
    "apps.search",
    "apps.notifications",
    "apps.connections",
    "apps.scheduling",
    "apps.mcp",
]

SITE_ID = 1

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = "slotly_api.urls"
WSGI_APPLICATION = "slotly_api.wsgi.application"
ASGI_APPLICATION = "slotly_api.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://slotly:slotly_dev_password@localhost:5432/slotly",
    ),
}

# --- Auth ---
AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- allauth (email-only) ---
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_AUTHENTICATION_METHOD = "email"          # legacy alias — read by headless /config
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_EMAIL_REQUIRED = True                    # legacy alias still checked at boot
ACCOUNT_USERNAME_REQUIRED = False                # legacy alias for usernameless model
ACCOUNT_USER_MODEL_USERNAME_FIELD = None         # we removed `username` from User
ACCOUNT_USER_MODEL_EMAIL_FIELD = "email"
ACCOUNT_EMAIL_SUBJECT_PREFIX = "[Slotly] "
ACCOUNT_RATE_LIMITS = {"login_failed": "5/15m/key"}
HEADLESS_ONLY = True
HEADLESS_FRONTEND_URLS = {
    "account_confirm_email": "http://localhost:3000/auth/verify/{key}",
    "account_reset_password": "http://localhost:3000/auth/forgot",
    "account_reset_password_from_key": "http://localhost:3000/auth/reset/{key}",
    "account_signup": "http://localhost:3000/auth/register",
}

# --- DRF ---
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}

# --- Sessions / cookies (PRD §5.1) ---
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 30  # 30 days
SESSION_SAVE_EVERY_REQUEST = True  # rolling expiry

CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = not DEBUG

# --- CORS (Next.js dev server) ---
CORS_ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]

# --- Email (MailHog locally) ---
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env("EMAIL_PORT")
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env("EMAIL_USE_TLS")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@slotly.local")

# --- Celery / Redis ---
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/1")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/2")
CELERY_TASK_ALWAYS_EAGER = DEBUG and env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)

# PRD §5.2: poll every ICS calendar every 5 minutes. Run with:
#   celery -A slotly_api beat -l info
# Beat schedule lives in code; switch to DatabaseScheduler later if operators
# want to edit cadence at runtime.
CELERY_BEAT_SCHEDULE = {
    "calendars-sync-all-due": {
        "task": "calendars.sync_all_due",
        "schedule": 300.0,  # seconds
    },
    # Daily archive purge: drop Unavailability blocks whose ends_at is
    # >90 days in the past (per UX decision — the user already moved on).
    "availability-purge-old-blocks": {
        "task": "availability.purge_old_blocks",
        "schedule": 86400.0,  # 24h
    },
    "mcp-purge-idempotency": {
        "task": "mcp.purge_old_idempotency",
        "schedule": 3600.0,  # hourly
    },
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env("REDIS_URL", default="redis://localhost:6379/0"),
    }
}

# --- I18N / TZ ---
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Prague"
USE_I18N = True
USE_TZ = True

# --- Static / media ---
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Calendar URL encryption (used in milestone 3) ---
CALENDAR_URL_ENCRYPTION_KEY = env("CALENDAR_URL_ENCRYPTION_KEY", default="")

# --- Google Calendar OAuth (M18a) ---
# Web-application OAuth 2.0 client from Google Cloud Console. The redirect
# URI registered in GCC must match GOOGLE_OAUTH_REDIRECT_URI exactly. Empty
# defaults are fine in dev when the booking flow isn't being exercised; the
# connect endpoint surfaces a clear 503 when unconfigured.
GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", default="")
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET", default="")
GOOGLE_OAUTH_REDIRECT_URI = env(
    "GOOGLE_OAUTH_REDIRECT_URI",
    default="http://localhost:8000/api/oauth/google/callback",
)
# After OAuth callback completes, the user is bounced back to the frontend.
# Path is appended to FRONTEND_BASE_URL (defined in settings_prod.py for
# production; here for dev). The query string carries a status flag the
# frontend reads to render success/failure.
GOOGLE_OAUTH_FRONTEND_RETURN = "/settings/calendars"
FRONTEND_BASE_URL = env("FRONTEND_BASE_URL", default="http://localhost:3000")
# Requested scopes.
#   calendar.events      — read+write events in specific calendars
#   calendar.freebusy    — read free/busy across all calendars
#   calendar.readonly    — required to list the user's calendars via calendarList
#                          (calendar.events alone returns 403 on that endpoint —
#                          the settings dropdown "Pick a calendar to write into"
#                          couldn't populate without it)
#   openid/email/profile — SSO identity + given_name/family_name for signup
GOOGLE_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/calendar.readonly",
    "openid",
    "email",
    "profile",
]

# --- Microsoft 365 / Outlook OAuth (M18b) ---
# Azure AD "app registration" client. Use the "common" tenant when
# registering the redirect URI so both work-school (M365) and personal
# (outlook.com) accounts can sign in. Empty defaults are safe in dev —
# the connect endpoint returns 503 when unconfigured.
MICROSOFT_OAUTH_CLIENT_ID = env("MICROSOFT_OAUTH_CLIENT_ID", default="")
MICROSOFT_OAUTH_CLIENT_SECRET = env("MICROSOFT_OAUTH_CLIENT_SECRET", default="")
MICROSOFT_OAUTH_REDIRECT_URI = env(
    "MICROSOFT_OAUTH_REDIRECT_URI",
    default="http://localhost:8000/api/oauth/microsoft/callback",
)
MICROSOFT_OAUTH_FRONTEND_RETURN = "/settings/calendars"
# offline_access → refresh_token. Calendars.ReadWrite → create events on
# any calendar the user can edit. profile → given_name/family_name for
# SSO signup so the new user's name isn't blank.
MICROSOFT_OAUTH_SCOPES = [
    "openid",
    "email",
    "profile",
    "offline_access",
    "Calendars.ReadWrite",
]
