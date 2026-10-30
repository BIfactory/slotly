"""Czech + English translation strings for the three visitor-facing email
flows: host cancellation, host rejection (physical request), and the 24h/1h
booking reminders.

Design decision: no Django `gettext` / `.po` files. The strings are small,
only ever consumed from Python (never templates), and we want full
sentence-level translator control (not word-level substitution). A simple
dict keyed on `locale` is enough and keeps diff-review trivial.

Locale picks come from `booking.locale` (or `booking_request.locale`).
Default when nothing is passed is English, which matches the pre-i18n
behaviour for existing rows.

Each entry is either:
  - a plain string (used verbatim), or
  - a format-string with named placeholders — callers pass kwargs.

Keep helper functions here so sender functions in services.py / views.py /
management command stay small — they just call `t_subject_host_cancel(...)`.
"""
from __future__ import annotations

from typing import Literal

Locale = Literal["en", "cs"]


def _norm(locale: str | None) -> Locale:
    """Clamp any input to a supported locale. Everything that isn't 'cs'
    becomes 'en' — ensures a bad DB row or stale request can't 500 the
    email render path."""
    return "cs" if (locale or "").lower() == "cs" else "en"


# ---------------------------------------------------------------------------
# Host cancellation — _mail_visitor_host_cancellation in services.py
# ---------------------------------------------------------------------------

def t_subject_host_cancel(locale: str | None, *, host_name: str, when: str) -> str:
    if _norm(locale) == "cs":
        return f"{host_name} zrušil(a) vaši schůzku {when}"
    return f"{host_name} cancelled your meeting on {when}"


def t_body_host_cancel(
    locale: str | None,
    *,
    greeting_name: str,
    host_name: str,
    when: str,
    reason: str = "",
) -> dict:
    """Returns a dict with keys expected by the sender:
    - `greeting`, `summary`, `note_header` (optional), `cta_label`, `title`
    """
    if _norm(locale) == "cs":
        return {
            "greeting": f"Dobrý den {greeting_name},",
            "summary": f"{host_name} musel(a) zrušit vaši schůzku.",
            "note_header": f"Zpráva od {host_name}:",
            "cta_label": "Vybrat jiný termín",
            "title": f"{host_name} zrušil(a) vaši schůzku",
            "kv_when": "Kdy",
            "kv_where": "Kde",
        }
    return {
        "greeting": f"Hi {greeting_name},",
        "summary": f"{host_name} had to cancel your meeting.",
        "note_header": f"Note from {host_name}:",
        "cta_label": "Pick another slot",
        "title": f"{host_name} cancelled your meeting",
        "kv_when": "When",
        "kv_where": "Where",
    }


# ---------------------------------------------------------------------------
# Host rejection of a physical BookingRequest — _mail_visitor_rejection
# ---------------------------------------------------------------------------

def t_subject_rejection(locale: str | None, *, when: str) -> str:
    if _norm(locale) == "cs":
        return f"Vaše žádost o schůzku {when} byla odmítnuta"
    return f"Your Slotly meeting request on {when} was declined"


def t_body_rejection(
    locale: str | None,
    *,
    visitor_name: str,
    host_name: str,
    when: str,
    note: str = "",
) -> dict:
    if _norm(locale) == "cs":
        return {
            "greeting": f"Dobrý den {visitor_name},",
            "summary": f"{host_name} se vám bohužel nemůže věnovat {when}.",
            "note_header": f"Zpráva od {host_name}:",
            "closing": "Nic se neděje — můžete si vybrat jiný termín na jeho stránce.",
            "cta_label": "Vybrat jiný termín",
            "title": f"{host_name} v tento čas nemůže",
            "kv_when": "Kdy",
            "kv_where": "Kde",
        }
    return {
        "greeting": f"Hi {visitor_name},",
        "summary": f"{host_name} isn't able to meet on {when}.",
        "note_header": f"Note from {host_name}:",
        "closing": "Feel free to pick another slot from their booking page — no hard feelings.",
        "cta_label": "Pick another slot",
        "title": f"{host_name} can't make that time",
        "kv_when": "When",
        "kv_where": "Where",
    }


# ---------------------------------------------------------------------------
# Reminder (24h / 1h) — _send in send_booking_reminders.py
# ---------------------------------------------------------------------------

# Stage-specific verbs used in subject + title: pass through from the
# existing cfg dict in the management command, but translated.
REMINDER_STAGE_CFG = {
    "24h": {
        "en": {"subject_verb": "Tomorrow", "intro": "Reminder:"},
        "cs": {"subject_verb": "Zítra", "intro": "Připomínka:"},
    },
    "1h": {
        "en": {"subject_verb": "In an hour", "intro": "Starting soon:"},
        "cs": {"subject_verb": "Za hodinu", "intro": "Blíží se:"},
    },
}


def t_reminder_strings(locale: str | None, *, stage: str) -> dict:
    """Return a dict with keys: subject_verb, intro, body_meeting_line,
    body_where_label, body_cancel_line, title_meeting_prefix, cta_label,
    kv_when, kv_where, calendar_hint."""
    loc = _norm(locale)
    cfg = REMINDER_STAGE_CFG.get(stage, REMINDER_STAGE_CFG["24h"])[loc]
    if loc == "cs":
        return {
            "subject_verb": cfg["subject_verb"],
            "intro": cfg["intro"],
            "greeting": "Dobrý den {name},",
            "body_meeting_line": "{intro} schůzka s {host} je {when}.",
            "where_prefix": "Kde: ",
            "cancel_prefix": "Potřebujete zrušit? ",
            "title_meeting_prefix": "Schůzka s {host} — {verb_lower}",
            "cta_label": "Spravovat nebo zrušit",
            "kv_when": "Kdy",
            "kv_where": "Kde",
            "calendar_hint": (
                "V kalendáři máte pozvánku i s případným odkazem na video schůzku."
            ),
        }
    return {
        "subject_verb": cfg["subject_verb"],
        "intro": cfg["intro"],
        "greeting": "Hi {name},",
        "body_meeting_line": "{intro} You're meeting {host} {when}.",
        "where_prefix": "Where: ",
        "cancel_prefix": "Need to cancel? ",
        "title_meeting_prefix": "Meeting with {host} — {verb_lower}",
        "cta_label": "Manage or cancel",
        "kv_when": "When",
        "kv_where": "Where",
        "calendar_hint": (
            "The calendar invite is already in your inbox with any video link."
        ),
    }


# ---------------------------------------------------------------------------
# Human-readable date formatting (CS locale month/weekday names via Babel-ish
# minimal map). Keep tiny — Python stdlib strftime respects `locale.setlocale`
# but that's a process-global, which is dangerous in a web process. Prefer
# a local map for the handful of weekday + month names we need.
# ---------------------------------------------------------------------------

_WEEKDAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "cs": ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"],
}

_MONTHS = {
    "en": [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ],
    "cs": [
        "ledna", "února", "března", "dubna", "května", "června",
        "července", "srpna", "září", "října", "listopadu", "prosince",
    ],
}


def format_when(dt, locale: str | None) -> str:
    """Format a datetime as a visitor-friendly "weekday day month, HH:MM"
    string in the given locale.

    English: "Monday 03 November, 14:00"
    Czech:   "pondělí 3. listopadu, 14:00"
    """
    loc = _norm(locale)
    weekday = _WEEKDAYS[loc][dt.weekday()]
    month = _MONTHS[loc][dt.month - 1]
    if loc == "cs":
        return f"{weekday} {dt.day}. {month}, {dt.strftime('%H:%M')}"
    return f"{weekday} {dt.strftime('%d')} {month}, {dt.strftime('%H:%M')}"


# ---------------------------------------------------------------------------
# Subject prefix (shared across all three flows)
# ---------------------------------------------------------------------------

def subject_tag(_locale: str | None) -> str:
    """Brand tag that prefixes every outbound subject. Kept locale-agnostic
    so filters in visitor's inbox stay stable even if they change language."""
    return "[Slotly]"
