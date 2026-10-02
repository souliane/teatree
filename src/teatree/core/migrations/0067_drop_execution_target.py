from django.db import migrations, models

# The column drops are driven by explicit SQL rather than ``RemoveField`` alone.
# ``execution_target`` is a NOT NULL CharField, so SQLite's ``add_field`` takes the
# ``_remake_table`` path on the REVERSE — and that rebuild rendered the table from a
# column set missing ``park_repeats`` / ``reasoning_effort`` / ``skills_loaded``,
# silently dropping three live columns and breaking every later rewind past this
# migration (measured: `no such column: "park_repeats"` on `migrate core 0028`).
# ``ALTER TABLE … DROP/ADD COLUMN`` needs no rebuild in either direction, so the
# state change and the schema change are declared separately.
_DROP_TASK = 'ALTER TABLE "teatree_task" DROP COLUMN "execution_target"'
_ADD_TASK = 'ALTER TABLE "teatree_task" ADD COLUMN "execution_target" varchar(32) NOT NULL DEFAULT \'headless\''
_DROP_ATTEMPT = 'ALTER TABLE "teatree_taskattempt" DROP COLUMN "execution_target"'
_ADD_ATTEMPT = (
    'ALTER TABLE "teatree_taskattempt" ADD COLUMN "execution_target" varchar(32) NOT NULL DEFAULT \'headless\''
)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0066_admit_load_bearing_loops"),
    ]

    # The cost-cover index led on ``execution_target``, so it is dropped BEFORE the
    # column and rebuilt (leading on the cycle range instead) once both are gone.
    operations = [
        migrations.RemoveIndex(
            model_name="taskattempt",
            name="taskattempt_cost_cover",
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="taskattempt", name="execution_target"),
                migrations.RemoveField(model_name="task", name="execution_target"),
            ],
            database_operations=[
                migrations.RunSQL(sql=_DROP_ATTEMPT, reverse_sql=_ADD_ATTEMPT),
                migrations.RunSQL(sql=_DROP_TASK, reverse_sql=_ADD_TASK),
            ],
        ),
        migrations.AddIndex(
            model_name="taskattempt",
            index=models.Index(
                fields=[
                    "started_at",
                    "model",
                    "lane",
                    "cost_is_estimated",
                    "cost_usd",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "task",
                ],
                name="taskattempt_cost_cover",
            ),
        ),
    ]
