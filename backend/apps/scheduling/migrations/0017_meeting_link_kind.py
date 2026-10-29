"""
Combined migration: add ``meeting_link_kind`` to both ``Booking`` and
``MeetingType``.

A single migration file is used (rather than two separate files 0017 + 0018)
because both additions are independent, purely additive schema changes with
the same default ("auto").  Combining them keeps the migration history
compact and avoids an intermediate state where only one model has the field.
Existing rows get "auto" via Django's column-level DEFAULT — no data migration
step is needed.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0016_write_default_per_provider_backfill"),
    ]

    operations = [
        migrations.AddField(
            model_name="meetingtype",
            name="meeting_link_kind",
            field=models.CharField(
                choices=[
                    ("auto", "Auto (use host's default)"),
                    ("google_meet", "Google Meet"),
                    ("teams", "Microsoft Teams"),
                    ("none", "No video link"),
                ],
                default="auto",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="meeting_link_kind",
            field=models.CharField(
                choices=[
                    ("auto", "Auto (use host's default)"),
                    ("google_meet", "Google Meet"),
                    ("teams", "Microsoft Teams"),
                    ("none", "No video link"),
                ],
                default="auto",
                max_length=16,
            ),
        ),
    ]
