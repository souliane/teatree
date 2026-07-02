"""Collapse pre-#2887 ``agent_runtime`` credential values into the two-layer config model.

Before #2887, ``agent_runtime`` conflated the dispatch LANE (interactive vs
headless) with the headless CREDENTIAL (``sdk_oauth`` / ``sdk_apikey`` / the
never-implemented ``api``) in one enum. The two axes are now split: the LANE
stays on ``agent_runtime`` (collapsed to ``interactive`` / ``headless``) and the
credential moves to the new Layer-2 setting ``agent_harness_provider``
(``subscription_oauth`` / ``api_key``, constrained by Layer 1 ``agent_harness``).

A stored ``ConfigSetting`` row from before this change carries a value the new
``AgentRuntime`` enum can no longer parse (``sdk_oauth`` / ``sdk_apikey`` /
``api``), which would raise loud on the very next settings read. This data
migration rewrites every such row's value to ``headless`` and, for the two
credential-carrying values, seeds the sibling ``agent_harness_provider`` row (same
scope) with the matching credential — so an existing install resolves to the
IDENTICAL effective credential after the upgrade as before it. ``api`` had no
working implementation (``run_headless`` always refused it), so it collapses to
``headless`` with no ``agent_harness_provider`` seed — that setting's own
default (``subscription_oauth``) applies, matching what a real dispatch would
have needed to configure anyway.
"""

from django.db import migrations

_RUNTIME_KEY = "agent_runtime"
_PROVIDER_KEY = "agent_harness_provider"

# Old agent_runtime value -> new agent_runtime value (every headless variant
# collapses to the single HEADLESS lane).
_RUNTIME_COLLAPSE: dict[str, str] = {
    "sdk_oauth": "headless",
    "sdk_apikey": "headless",
    "api": "headless",
}

# Old agent_runtime value -> the agent_harness_provider value that preserves the
# credential it used to select. ``api`` has no entry (never implemented).
_PROVIDER_FOR_OLD_RUNTIME: dict[str, str] = {
    "sdk_oauth": "subscription_oauth",
    "sdk_apikey": "api_key",
}


def collapse_agent_runtime_to_two_layer(apps, schema_editor):
    ConfigSetting = apps.get_model("core", "ConfigSetting")
    for row in ConfigSetting.objects.filter(key=_RUNTIME_KEY):
        old_value = row.value
        new_runtime = _RUNTIME_COLLAPSE.get(old_value)
        if new_runtime is None:
            continue
        row.value = new_runtime
        row.save(update_fields=["value"])
        provider_value = _PROVIDER_FOR_OLD_RUNTIME.get(old_value)
        if provider_value is not None:
            ConfigSetting.objects.update_or_create(
                scope=row.scope,
                key=_PROVIDER_KEY,
                defaults={"value": provider_value},
            )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_ticket_repo_namespaced_key"),
    ]

    operations = [
        migrations.RunPython(collapse_agent_runtime_to_two_layer, migrations.RunPython.noop),
    ]
