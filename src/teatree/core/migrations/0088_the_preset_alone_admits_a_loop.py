"""Drop the two per-loop existence scalars a total preset made a second answer (D6).

Refuses rather than picks when a box's stored rows DISAGREE across scopes: a global row
saying on and an overlay row saying off is an opinion no single preset entry can carry, and
silently keeping one of them is how an operator's deliberate carve-out disappears.
"""

from django.db import migrations

RETIRED_SCALARS = ("issue_implementer_enabled", "triage_assessor_enabled")


class DivergentScopedRowsError(RuntimeError):
    """The stored rows disagree across scopes, so no single preset entry can replace them."""


def _fold_onto_the_preset(apps, schema_editor) -> None:
    config_setting = apps.get_model("core", "ConfigSetting")
    for key in RETIRED_SCALARS:
        values = set(config_setting.objects.filter(key=key).values_list("value", flat=True))
        if len(values) > 1:
            msg = (
                f"{key!r} is stored with disagreeing values across scopes ({sorted(values)!r}). "
                "A preset entry is box-global and cannot carry a per-overlay carve-out: decide the "
                "one answer, write it into the active preset, then re-run this migration."
            )
            raise DivergentScopedRowsError(msg)
        config_setting.objects.filter(key=key).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0087_manual_override_replaces_the_forced_plane")]

    operations = [migrations.RunPython(_fold_onto_the_preset, migrations.RunPython.noop)]
