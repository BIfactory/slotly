# Schema migration: replace XOR constraint with "at least one" constraint.
#
# WriteCalendarDefault previously enforced exactly-one-provider via
# `write_default_exactly_one_provider` (XOR). Users with both Google and
# Microsoft accounts connected can now independently designate a write target
# for each provider, so both FKs may be non-null simultaneously. The new
# constraint only requires at least one FK to be non-null.
#
# The FK `on_delete` behaviour remains CASCADE: deleting an account row still
# cascades to delete the WriteCalendarDefault row when both FKs would become
# null. The view layer handles promotion to a surviving account before the
# delete, so this only fires when the very last linked account is deleted.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0014_oauth_busy_window"),
    ]

    operations = [
        # 1. Remove the old XOR constraint.
        migrations.RemoveConstraint(
            model_name="writecalendardefault",
            name="write_default_exactly_one_provider",
        ),
        # 2. Add the new "at least one" constraint.
        migrations.AddConstraint(
            model_name="writecalendardefault",
            constraint=models.CheckConstraint(
                condition=models.Q(google_account__isnull=False)
                | models.Q(microsoft_account__isnull=False),
                name="write_default_has_at_least_one_provider",
            ),
        ),
    ]
