"""Drop the dead ``Mode.availability_mode`` column.

Deliberately data-destructive and not reversible in data terms: the three stored
tokens are lost. Safe by construction — their entire semantic payload is already
carried by ``defers_questions`` / ``pauses_self_pump`` / ``presence_sensitive``,
back-filled by ``0022_mode_booleans`` and re-derived from ``defaults.toml`` by the
``get_or_create``-by-name seeder, so no ``RunPython`` companion is needed.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0056_task_admitted_at")]

    operations = [migrations.RemoveField(model_name="mode", name="availability_mode")]
