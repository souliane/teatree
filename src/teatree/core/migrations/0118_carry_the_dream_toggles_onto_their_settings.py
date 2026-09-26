"""Carry each ``loops.dream`` toggle onto the flat ``dream_<toggle>`` setting that replaced it.

The dream pass reads ``dream_compliance_escalate`` and its siblings now, and nothing reads
the nested ``loops.dream`` table any more. A box that opted into a default-off phase there
would lose the opt-in silently, so each stored value that differs from the phase's shipped
default moves onto its own global row. A value equal to the default carries nothing: the
setting already answers it, and a row would freeze a future default change.

A ``dream_<toggle>`` row the operator already wrote wins. ``promotion_cap`` is not carried:
0115 retired it, so there is no setting to receive it. The nested table is left in place;
it is inert, and the reverse needs nothing to rebuild.
"""

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

_REGISTRY = "loops"
_LOOP = "dream"
_WRITER = "migration:0118_carry_the_dream_toggles_onto_their_settings"

SHIPPED_DEFAULTS = {
    "automation_asks": False,
    "compliance_escalate": False,
    "compliance_measure": True,
    "cross_link": True,
    "decay": True,
    "derive_evals": False,
    "memory_promote": True,
    "merge": True,
    "propose_evals": True,
    "reindex": True,
    "validate_live": False,
}


def carry_the_toggles(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    rows = apps.get_model("core", "ConfigSetting").objects.using(schema_editor.connection.alias)
    registry = rows.filter(scope="", key=_REGISTRY).values_list("value", flat=True).first()
    toggles = registry.get(_LOOP) if isinstance(registry, dict) else None
    if not isinstance(toggles, dict):
        return
    for toggle, default in SHIPPED_DEFAULTS.items():
        value = toggles.get(toggle)
        if isinstance(value, bool) and value != default:
            rows.get_or_create(scope="", key=f"dream_{toggle}", defaults={"value": value, "written_by": _WRITER})


def drop_the_carried_rows(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    rows = apps.get_model("core", "ConfigSetting").objects.using(schema_editor.connection.alias)
    rows.filter(written_by=_WRITER).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0117_failure_kind_after_both_chains")]

    operations = [migrations.RunPython(carry_the_toggles, drop_the_carried_rows)]
