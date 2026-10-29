"""Debug: dump OAuthBusyWindow rows for a given user email in a time range.

Usage:
    python manage.py dump_oauth_busy --email hulin@bifactory.cz --date 2026-10-29
    python manage.py dump_oauth_busy --all --date 2026-10-29

Not for production cron. Short-lived diagnostic command.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.scheduling.models import GoogleAccount, MicrosoftAccount, OAuthBusyWindow

User = get_user_model()


class Command(BaseCommand):
    help = "Dump OAuthBusyWindow rows for a user + date (UTC + Prague view)."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--email", help="Dump for a single user by email.")
        group.add_argument(
            "--all", action="store_true", dest="all_users",
            help="Dump for every user, ordered by email.",
        )
        parser.add_argument("--date", required=True, help="YYYY-MM-DD (local date)")

    def handle(self, *args, **options):
        date = options["date"]
        if options["all_users"]:
            users = User.objects.order_by("email")
            for user in users:
                self.stdout.write(f"=== {user.email} ===")
                self._dump_user(user, date)
                self.stdout.write("")
        else:
            email = options["email"]
            user = User.objects.filter(email=email).first()
            if user is None:
                self.stdout.write(f"user {email!r} not found")
                return
            self._dump_user(user, date)

    def _dump_user(self, user, date: str) -> None:
        day = datetime.strptime(date, "%Y-%m-%d").date()
        prague = ZoneInfo("Europe/Prague")
        start_local = datetime.combine(day, time.min, tzinfo=prague)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)

        self.stdout.write(f"user: {user.email} (pk={user.pk})")
        self.stdout.write(
            f"range: {start_local.isoformat()} .. {end_local.isoformat()}"
        )
        self.stdout.write("")
        self.stdout.write("GoogleAccount rows:")
        for g in GoogleAccount.objects.filter(user=user):
            self.stdout.write(f"  pk={g.pk} email={g.google_email}")
        self.stdout.write("")
        self.stdout.write("MicrosoftAccount rows:")
        for m in MicrosoftAccount.objects.filter(user=user):
            self.stdout.write(f"  pk={m.pk} email={m.microsoft_email}")
        self.stdout.write("")

        rows = OAuthBusyWindow.objects.filter(
            user=user,
            starts_at__lt=end_utc,
            ends_at__gt=start_utc,
        ).order_by("starts_at")

        self.stdout.write(f"OAuthBusyWindow — {rows.count()} rows overlapping {date}:")
        for r in rows:
            s_utc = r.starts_at.astimezone(timezone.utc).strftime("%H:%M")
            e_utc = r.ends_at.astimezone(timezone.utc).strftime("%H:%M")
            s_prg = r.starts_at.astimezone(prague).strftime("%H:%M")
            e_prg = r.ends_at.astimezone(prague).strftime("%H:%M")
            acc = f"g:{r.google_account_id}" if r.google_account_id else f"m:{r.microsoft_account_id}"
            self.stdout.write(
                f"  {acc} cal={r.calendar_id[:40]:40s} UTC {s_utc}-{e_utc}  Prague {s_prg}-{e_prg}"
            )
