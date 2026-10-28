"""
Data migration: backfill WriteCalendarDefault for every user who already
has at least one Google or Microsoft account.

Preference order: Google first (mirrors `_pick_write_provider` logic),
then Microsoft as fallback.  If a user has accounts on both providers
they get a Google-based WriteCalendarDefault (their first Google account
is used).
"""
from django.db import migrations


def forwards(apps, schema_editor):
    GoogleAccount = apps.get_model("scheduling", "GoogleAccount")
    MicrosoftAccount = apps.get_model("scheduling", "MicrosoftAccount")
    WriteCalendarDefault = apps.get_model("scheduling", "WriteCalendarDefault")

    seen_users: set = set()

    for g in GoogleAccount.objects.select_related("user").all():
        if g.user_id in seen_users:
            continue
        WriteCalendarDefault.objects.create(user=g.user, google_account=g)
        seen_users.add(g.user_id)

    for m in MicrosoftAccount.objects.select_related("user").all():
        if m.user_id in seen_users:
            continue
        WriteCalendarDefault.objects.create(user=m.user, microsoft_account=m)
        seen_users.add(m.user_id)


def backwards(apps, schema_editor):
    apps.get_model("scheduling", "WriteCalendarDefault").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0012_multi_account_schema"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
