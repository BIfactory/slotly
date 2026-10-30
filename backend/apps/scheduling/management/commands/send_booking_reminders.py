"""
Mail visitors a reminder for their confirmed Slotly booking.

Runs from a Container Apps Job on a cron. Each pass targets one
*stage* (default "24h") and only touches bookings that (a) fall in
the stage's window ahead of now and (b) haven't had that stage
delivered yet (``reminded_stages`` list authoritative).

Typical prod schedule:
    slotly-booking-reminders-24h  → --stage 24h  every 30 min
    slotly-booking-reminders-1h   → --stage 1h   every 10 min

Same booking can receive several stages back-to-back; each stage
uses its own copy so a "meeting in 1 hour" mail doesn't read like
"meeting tomorrow".

Usage:
    python manage.py send_booking_reminders                 # 24h default
    python manage.py send_booking_reminders --stage 1h
    python manage.py send_booking_reminders --dry-run
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.notifications.emails import html_shell, kv_rows, paragraph
from apps.scheduling.models import Booking

logger = logging.getLogger(__name__)


# Stage → (offset before start_at, cadence slack, subject template, intro copy).
# Slack is the half-width of the "due now" window: cron cadence must be
# ≤ 2 × slack so no booking slips between runs.
STAGES = {
    "24h": {
        "hours_before": 24,
        "slack_hours": 1.0,        # cron every 30 min
        "subject_verb": "Tomorrow",
        "intro": "Just a friendly reminder — your meeting is tomorrow.",
    },
    "1h": {
        "hours_before": 1,
        "slack_hours": 0.5,        # cron every 10 min
        "subject_verb": "In 1 hour",
        "intro": "Heads up — your meeting starts in about an hour.",
    },
}


class Command(BaseCommand):
    help = "Mail visitors a reminder for confirmed public Slotly bookings."

    def add_arguments(self, parser):
        parser.add_argument(
            "--stage",
            default="24h",
            help=f"Which stage to send. One of: {', '.join(STAGES)}. Default 24h.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log what would be sent without touching mail or the DB.",
        )

    def handle(self, *args, stage: str, dry_run: bool, **_):
        from django.conf import settings as _settings

        if stage not in STAGES:
            raise CommandError(
                f"Unknown stage {stage!r}. Available: {', '.join(STAGES)}",
            )
        cfg = STAGES[stage]

        now = timezone.now()
        window_center = now + timedelta(hours=cfg["hours_before"])
        window_start = window_center - timedelta(hours=cfg["slack_hours"])
        window_end = window_center + timedelta(hours=cfg["slack_hours"])

        qs = Booking.objects.filter(
            status=Booking.Status.CONFIRMED,
            start_at__gte=window_start,
            start_at__lte=window_end,
        ).select_related("host")

        # Filter out rows that already have this stage mailed. Doing the
        # containment check in Python keeps the query DB-agnostic (JSONB
        # __contains would work on postgres but not on sqlite tests).
        rows = [b for b in qs if stage not in (b.reminded_stages or [])]
        self.stdout.write(
            f"Stage {stage}: window {window_start.isoformat()} → "
            f"{window_end.isoformat()}: {len(rows)} booking(s) to remind"
        )

        sent = 0
        for b in rows:
            try:
                self._send(b, stage=stage, cfg=cfg, dry_run=dry_run,
                           frontend_base=_settings.FRONTEND_BASE_URL)
                if not dry_run:
                    stages = list(b.reminded_stages or [])
                    if stage not in stages:
                        stages.append(stage)
                    Booking.objects.filter(pk=b.pk).update(
                        reminded_at=timezone.now(),
                        reminded_stages=stages,
                    )
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("reminder [%s] failed for booking %s: %s", stage, b.uuid, exc)
                self.stdout.write(self.style.WARNING(f"  {b.uuid}: FAILED — {exc}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Sent {sent}/{len(rows)} '{stage}' reminders"
                + (" (dry-run — no emails, no DB writes)" if dry_run else "")
            )
        )

    def _send(self, b: Booking, *, stage: str, cfg: dict, dry_run: bool, frontend_base: str) -> None:
        """Render + send a reminder in the booking's locale. `cfg` is still
        passed in from the stage registry (defines the stage key, delay, etc.)
        but the human-readable verbs now come from `emails_i18n` so a CS
        visitor gets "Zítra:" / "Za hodinu:" instead of "Tomorrow" / "In
        an hour"."""
        from django.conf import settings as _settings
        from apps.scheduling import emails_i18n as _i18n

        if not b.visitor_email:
            self.stdout.write(f"  {b.uuid}: skipped — no visitor email on row")
            return

        locale = b.locale or "en"
        host_name = f"{b.host.first_name} {b.host.last_name}".strip() or b.host.email
        when = _i18n.format_when(b.start_at, locale)
        strings = _i18n.t_reminder_strings(locale, stage=stage)
        greeting_name = b.visitor_name or (
            "tam" if _i18n._norm(locale) == "cs" else "there"
        )

        subject = (
            f"{strings['subject_verb']}: "
            + (
                f"schůzka s {host_name} v {b.start_at.strftime('%H:%M')}"
                if _i18n._norm(locale) == "cs"
                else f"your meeting with {host_name} at {b.start_at.strftime('%H:%M')}"
            )
        )
        text_body = (
            strings["greeting"].format(name=greeting_name)
            + "\n\n"
            + strings["body_meeting_line"].format(
                intro=strings["intro"], host=host_name, when=when,
            )
        )
        if b.location:
            text_body += f"\n\n{strings['where_prefix']}{b.location}"
        text_body += f"\n\n{strings['cancel_prefix']}{frontend_base.rstrip('/')}/b/{b.uuid}"

        html_body = html_shell(
            title=strings["title_meeting_prefix"].format(
                host=host_name, verb_lower=strings["subject_verb"].lower(),
            ),
            intro_html=(
                paragraph(
                    strings["body_meeting_line"].format(
                        intro=strings["intro"], host=host_name, when=when,
                    )
                )
                + kv_rows([
                    (strings["kv_when"], when),
                    (strings["kv_where"] if b.location else "", b.location),
                ])
                + paragraph(strings["calendar_hint"], muted=True)
            ),
            cta_label=strings["cta_label"],
            cta_url=f"{frontend_base.rstrip('/')}/b/{b.uuid}?lang={_i18n._norm(locale)}",
        )

        self.stdout.write(f"  {b.uuid}: → {b.visitor_email} ({when}) [stage={stage}, locale={locale}]")
        if dry_run:
            return

        msg = EmailMultiAlternatives(
            subject=f"{_i18n.subject_tag(locale)} {subject}",
            body=text_body,
            from_email=_settings.DEFAULT_FROM_EMAIL,
            to=[b.visitor_email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
