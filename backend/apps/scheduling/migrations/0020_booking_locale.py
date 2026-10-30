from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0019_meeting_type_default_location"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="locale",
            field=models.CharField(
                choices=[("en", "English"), ("cs", "Czech")],
                default="en",
                max_length=2,
            ),
        ),
        migrations.AddField(
            model_name="bookingrequest",
            name="locale",
            field=models.CharField(
                choices=[("en", "English"), ("cs", "Czech")],
                default="en",
                max_length=2,
            ),
        ),
    ]
