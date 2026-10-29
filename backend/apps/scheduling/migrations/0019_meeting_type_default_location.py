"""
Add nullable FK MeetingType.default_location → UserLocation (SET_NULL).

If the referenced UserLocation is deleted, the FK becomes NULL but the
existing free-form MeetingType.location CharField is preserved as-is.
Purely additive — no existing data changes.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0018_user_location"),
    ]

    operations = [
        migrations.AddField(
            model_name="meetingtype",
            name="default_location",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="scheduling.userlocation",
            ),
        ),
    ]
