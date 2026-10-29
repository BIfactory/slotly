"""
Data migration: safety placeholder for the WriteCalendarDefault per-provider
schema change (0015).

Existing rows all satisfy the new "at least one provider set" constraint
because the previous XOR constraint already guaranteed exactly one of the two
FKs was non-null. No row transformation is needed.

This file exists as an explicit second step in the pair so that:
  - `squashmigrations` keeps the schema and data phases cleanly separated.
  - Future data-repair migrations can depend on 0016 rather than on a schema
    migration, preserving the convention used throughout this codebase.
  - Reviewers and DBAs can confirm the no-op intent without grepping model
    history.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0015_write_default_per_provider"),
    ]

    operations = [
        # Intentional no-op: all existing WriteCalendarDefault rows already
        # satisfy the new constraint. The backwards direction is also a no-op
        # because 0015 will enforce the correct constraint on rollback.
        migrations.RunPython(
            migrations.RunPython.noop,
            migrations.RunPython.noop,
        ),
    ]
