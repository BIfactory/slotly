"""
Add UserLocation model — a named physical location predefined by a user.

Purely additive: new table, no changes to existing tables. Zero migration risk.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0017_meeting_link_kind"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UserLocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="locations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("name", models.CharField(max_length=100)),
                ("address", models.CharField(max_length=400)),
                ("is_default", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=["user", "name"],
                        name="unique_location_name_per_user",
                    ),
                ],
                "indexes": [
                    models.Index(fields=["user", "is_default"], name="scheduling_userlocation_user_is_default_idx"),
                ],
            },
        ),
    ]
