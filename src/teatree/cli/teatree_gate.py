"""``t3 <overlay> gate`` — the orchestrator's guaranteed self-rescue.

The orchestrator-execution-boundary gate (``handle_enforce_orchestrator_boundary``
in ``hooks/scripts/hook_router.py``, [#115]/[#1472]) can deny the MAIN agent's
heavy foreground ``Bash``. If detection ever misbehaves — e.g. a sub-agent is
misclassified as the main agent — the orchestrator (and, by sidechain
misdetection, every sub-agent) can be locked out of Bash entirely.

This module backs the always-reachable escape hatch. ``t3 <overlay> gate
disable`` flips the durable kill-switch
``[teatree] orchestrator_bash_gate_enabled = false`` in ``~/.teatree.toml``.
The command is unconditionally runnable EVEN WHEN the gate is enabled, because
the gate's heavy-Bash denylist (``_ORCHESTRATOR_HEAVY_BASH_RE``) does not match
a ``t3 …`` command, and ``t3 …`` invocations are the orchestration prefix the
gate is built to allow. The kill-switch lives out-of-repo so it survives
``t3 update``.

This is a pure-Python local read/modify/write of ``~/.teatree.toml`` — it does
NOT route through Django or an overlay ``manage.py`` subprocess, so it stays
runnable even when the heavier overlay machinery is wedged.
"""

import tomllib
from pathlib import Path

import typer

GATE_KEY = "orchestrator_bash_gate_enabled"


def _config_path() -> Path:
    return Path.home() / ".teatree.toml"


def gate_is_enabled() -> bool:
    """Resolve ``[teatree] orchestrator_bash_gate_enabled`` (default True).

    Mirrors the hook layer's ``_orchestrator_bash_gate_enabled`` resolution:
    the gate is enabled unless an explicit ``false`` is recorded. Fails OPEN to
    enabled on a missing/broken config so the reported status matches what the
    gate itself would do.
    """
    config_path = _config_path()
    if not config_path.is_file():
        return True
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return True
    teatree = config.get("teatree") if isinstance(config, dict) else None
    if not isinstance(teatree, dict):
        return True
    return teatree.get(GATE_KEY) is not False


def _set_gate_enabled(*, enabled: bool) -> None:
    # ``tomlkit`` is imported inline (matching ``slack_setup``) so loading this
    # module — pulled transitively by ``teatree.config`` on every CLI bootstrap
    # — never eagerly imports the toml-preserving dep.
    import tomlkit  # noqa: PLC0415
    from tomlkit import items as tomlkit_items  # noqa: PLC0415

    config_path = _config_path()
    document = tomlkit.parse(config_path.read_text(encoding="utf-8")) if config_path.is_file() else tomlkit.document()
    teatree = document.get("teatree")
    if not isinstance(teatree, tomlkit_items.Table):
        teatree = tomlkit.table()
        document["teatree"] = teatree
    teatree[GATE_KEY] = enabled
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")


def register_gate_commands(overlay_app: typer.Typer) -> None:
    """Attach the ``gate`` subgroup to an overlay's Typer app."""
    gate_group = typer.Typer(
        no_args_is_help=True,
        help="Orchestrator-execution-boundary gate kill-switch (self-rescue).",
    )

    @gate_group.command(name="status")
    def status() -> None:
        """Show whether the orchestrator heavy-Bash gate is enabled."""
        if gate_is_enabled():
            typer.echo("gate ENABLED — heavy orchestrator bash blocked")
        else:
            typer.echo("gate DISABLED — no-op")

    @gate_group.command(name="disable")
    def disable() -> None:
        """Disable the gate (self-rescue from a Bash lockout)."""
        _set_gate_enabled(enabled=False)
        typer.echo(f"gate DISABLED — wrote `{GATE_KEY} = false` to {_config_path()}")

    @gate_group.command(name="enable")
    def enable() -> None:
        """Re-enable the gate."""
        _set_gate_enabled(enabled=True)
        typer.echo(f"gate ENABLED — wrote `{GATE_KEY} = true` to {_config_path()}")

    overlay_app.add_typer(gate_group, name="gate")
