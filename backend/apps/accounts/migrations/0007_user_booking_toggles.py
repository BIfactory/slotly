from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0006_user_buffer_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="accepts_google_bookings",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="user",
            name="accepts_microsoft_bookings",
            field=models.BooleanField(default=True),
        ),
    ]
