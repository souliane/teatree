"""Move "which overlays does this box sweep" off the preset model onto a box setting.

Which overlays a factory looks after is a property of the BOX. Carried per preset it was
N identical copies of one fact, and every new preset was a fresh place to get it wrong.

Behaviour-preserving by construction: the value is copied out before the column goes, and
the copy REFUSES rather than picks when the presets disagree. A divergence would mean
per-preset scoping was actually in use, which is an owner decision about what this box
should sweep — not something a migration may resolve by taking the first row it reads.
An empty scope on every preset seeds nothing, because the setting's own shipped default
already means "sweep the whole fleet".
"""

from django.db import migrations

_SETTING = "scanner_overlay_scope"
_GLOBAL_SCOPE = ""


def _names(raw: object) -> tuple[str, ...]:
    """The stored scope as a comparable tuple; anything malformed reads as no scope."""
    if not isinstance(raw, list):
        return ()
    return tuple(sorted(entry for entry in raw if isinstance(entry, str) and entry))


def _relocate(apps, schema_editor) -> None:
    mode = apps.get_model("core", "Mode")
    config_setting = apps.get_model("core", "ConfigSetting")

    scopes = {_names(row.overlay_scope) for row in mode.objects.all()}
    held = {scope for scope in scopes if scope}
    if len(held) > 1:
        readable = " | ".join(",".join(scope) for scope in sorted(held))
        refusal = (
            f"presets disagree about which overlays this box sweeps ({readable}); "
            f"per-preset scoping was in use, so set {_SETTING} deliberately and re-run"
        )
        raise RuntimeError(refusal)
    if not held:
        return
    config_setting.objects.update_or_create(
        key=_SETTING, scope=_GLOBAL_SCOPE, defaults={"value": list(next(iter(held)))}
    )


def _restore(apps, schema_editor) -> None:
    """Put the setting's value back on every preset, so the column reverses cleanly."""
    mode = apps.get_model("core", "Mode")
    config_setting = apps.get_model("core", "ConfigSetting")

    row = config_setting.objects.filter(key=_SETTING, scope=_GLOBAL_SCOPE).first()
    scope = list(_names(row.value)) if row is not None else []
    mode.objects.update(overlay_scope=scope)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0084_red_mr_fix_attempt_review_findings"),
    ]

    operations = [
        migrations.RunPython(_relocate, _restore),
        migrations.RemoveField(model_name="mode", name="overlay_scope"),
    ]
