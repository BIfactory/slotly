"""
Management command: synchronously sync busy intervals from every connected
Google/Microsoft OAuth account into OAuthBusyWindow.

In Phase-1 production this replaces the Celery worker + Beat duo
(same pattern as apps.calendars.management.commands.poll_calendars). The
intended deployment is an Azure Container Apps Job triggered by a 5-minute
cron. Each invocation:

    docker run ... python manage.py sync_oauth_busy

iterates every GoogleAccount + MicrosoftAccount row, fetches freebusy from
the provider, and replaces OAuthBusyWindow rows for that account.

The task function (apps.scheduling.tasks.sync_one_oauth_account) was
originally written as a Celery @shared_task but is callable directly —
the Celery dispatch (apply_async) is not used in prod.
"""

from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand

from apps.scheduling.models import GoogleAccount, MicrosoftAccount
from apps.scheduling.tasks import sync_one_oauth_account

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sync busy intervals from all connected OAuth accounts into OAuthBusyWindow."

    def handle(self, *args, **options):
        t0 = time.perf_counter()
        totals = {"google_accounts": 0, "microsoft_accounts": 0, "errors": 0, "written": 0}

        for pk in GoogleAccount.objects.values_list("pk", flat=True):
            totals["google_accounts"] += 1
            try:
                result = sync_one_oauth_account("google", pk)
                if result.get("error"):
                    totals["errors"] += 1
                    self.stdout.write(f"google pk={pk}: {result['error']}")
                else:
                    totals["written"] += result.get("written", 0)
                    self.stdout.write(
                        f"google pk={pk}: wrote {result.get('written', 0)} windows"
                    )
            except Exception as exc:  # noqa: BLE001
                totals["errors"] += 1
                logger.exception("sync_one_oauth_account google pk=%s crashed", pk)
                self.stdout.write(f"google pk={pk}: CRASH {exc}")

        for pk in MicrosoftAccount.objects.values_list("pk", flat=True):
            totals["microsoft_accounts"] += 1
            try:
                result = sync_one_oauth_account("microsoft", pk)
                if result.get("error"):
                    totals["errors"] += 1
                    self.stdout.write(f"microsoft pk={pk}: {result['error']}")
                else:
                    totals["written"] += result.get("written", 0)
                    self.stdout.write(
                        f"microsoft pk={pk}: wrote {result.get('written', 0)} windows"
                    )
            except Exception as exc:  # noqa: BLE001
                totals["errors"] += 1
                logger.exception("sync_one_oauth_account microsoft pk=%s crashed", pk)
                self.stdout.write(f"microsoft pk={pk}: CRASH {exc}")

        dt = time.perf_counter() - t0
        self.stdout.write(
            f"done in {dt:.1f}s — {totals['google_accounts']}G + {totals['microsoft_accounts']}M accounts, "
            f"{totals['written']} windows written, {totals['errors']} errors"
        )
