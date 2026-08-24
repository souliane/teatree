"""Let a ``Prompt`` body be blank, so a standing directive can be switched off (#4166).

Validation-only: ``blank`` never reaches the database, so this ``AlterField`` emits
no column change and touches no data. What it changes is reachability — the
already-tested "an empty override switches that slot off" mechanism had no surface
that could write one, because the admin form marked ``body`` required. The
loosening is narrowed at the model on both write surfaces — ``Prompt.clean`` and
``Prompt.revise`` — each still refusing an empty body on a prompt a ``Loop`` runs.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0060_session_handover_subagent_wrapup")]

    operations = [migrations.AlterField(model_name="prompt", name="body", field=models.TextField(blank=True))]
