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

A second gate rides the same self-rescue surface: the skill-loading-on-task
gate (``handle_enforce_skill_loading_on_task_create``, [#1488]) can deny a
fanned-out ``TaskCreated`` until the matching teatree skill is loaded. If its
detection ever misbehaves, ``t3 <overlay> gate skill-loading disable`` flips the
``[teatree] skill_loading_gate_enabled = false`` kill-switch — reachable for the
same reason (``t3 …`` is the orchestration prefix every gate allows; the
``TaskCreated`` gate does not govern Bash at all).

This is a pure-Python local read/modify/write of ``~/.teatree.toml`` — it does
NOT route through Django or an overlay ``manage.py`` subprocess, so it stays
runnable even when the heavier overlay machinery is wedged.
"""

import tomllib
from pathlib import Path

import typer

GATE_KEY = "orchestrator_bash_gate_enabled"
SKILL_GATE_KEY = "skill_loading_gate_enabled"
PLAN_GATE_KEY = "plan_edit_gate_enabled"
CONFIG_OVERWRITE_GATE_KEY = "config_overwrite_gate_enabled"
COMPLETION_CLAIM_GATE_KEY = "completion_claim_gate_enabled"
MEMORY_RECALL_GATE_KEY = "memory_recall_enabled"
# Master fail-open switch (NEVER-LOCKOUT). Unlike the per-gate kill-switches
# above (which default ENABLED and read ``is not False``), this is OFF by
# default and reads ``is True`` — it must NEVER relax a gate by accident, only
# by an explicit operator opt-in. When ON, every OVER-DENY gate flips to
# fail-open at once; the PUBLIC-egress leak gate ignores it (fail-closed always).
# The ``danger_`` prefix makes a forgotten override in ``~/.teatree.toml``
# unmissable — this switch disables protective gates wholesale.
DANGER_GATE_FAIL_OPEN_KEY = "danger_gate_fail_open"


def _config_path() -> Path:
    return Path.home() / ".teatree.toml"


def _gate_key_is_enabled(key: str) -> bool:
    """Resolve ``[teatree] <key>`` (default True), failing OPEN to enabled.

    Mirrors the hook layer's gate resolution: the gate is enabled unless an
    explicit ``false`` is recorded. Fails OPEN to enabled on a
    missing/broken config so the reported status matches what the gate
    itself would do.
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
    return teatree.get(key) is not False


def gate_is_enabled() -> bool:
    """Resolve the orchestrator heavy-Bash gate (``GATE_KEY``, default True)."""
    return _gate_key_is_enabled(GATE_KEY)


def skill_loading_gate_is_enabled() -> bool:
    """Resolve the skill-loading-on-task gate (``SKILL_GATE_KEY``, default True)."""
    return _gate_key_is_enabled(SKILL_GATE_KEY)


def plan_edit_gate_is_enabled() -> bool:
    """Resolve the plan-edit gate (``PLAN_GATE_KEY``, default True)."""
    return _gate_key_is_enabled(PLAN_GATE_KEY)


def config_overwrite_gate_is_enabled() -> bool:
    """Resolve the read-before-overwrite config gate (``CONFIG_OVERWRITE_GATE_KEY``, default True)."""
    return _gate_key_is_enabled(CONFIG_OVERWRITE_GATE_KEY)


def completion_claim_gate_is_enabled() -> bool:
    """Resolve the completion-claim Stop gate (``COMPLETION_CLAIM_GATE_KEY``, default True)."""
    return _gate_key_is_enabled(COMPLETION_CLAIM_GATE_KEY)


def memory_recall_gate_is_enabled() -> bool:
    """Resolve the cold-tier memory recall injector (``MEMORY_RECALL_GATE_KEY``, default True)."""
    return _gate_key_is_enabled(MEMORY_RECALL_GATE_KEY)


def danger_gate_fail_open_is_enabled() -> bool:
    """Resolve the master fail-open switch (``DANGER_GATE_FAIL_OPEN_KEY``, default False).

    Reads ``[teatree] danger_gate_fail_open`` and returns True ONLY when it is
    an explicit ``true``. Fails CLOSED to disabled (the protective default) on a
    missing/broken config or a non-table ``teatree`` section — the inverse
    posture of :func:`gate_is_enabled`, because accidentally relaxing every
    over-deny gate is exactly the failure this switch must never cause. The
    over-deny gates consult this; the PUBLIC-egress leak gate never does.
    """
    config_path = _config_path()
    if not config_path.is_file():
        return False
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    teatree = config.get("teatree") if isinstance(config, dict) else None
    if not isinstance(teatree, dict):
        return False
    return teatree.get(DANGER_GATE_FAIL_OPEN_KEY) is True


def _set_gate_key(key: str, *, enabled: bool) -> None:
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
    teatree[key] = enabled
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")


def _register_keyed_gate(parent: typer.Typer, *, name: str, key: str, label: str) -> None:
    """Attach a ``status``/``disable``/``enable`` subgroup for ``[teatree] <key>``."""
    group = typer.Typer(no_args_is_help=True, help=f"{label} kill-switch (self-rescue).")

    @group.command(name="status")
    def status() -> None:
        """Show whether the gate is enabled."""
        typer.echo("gate ENABLED" if _gate_key_is_enabled(key) else "gate DISABLED — no-op")

    @group.command(name="disable")
    def disable() -> None:
        """Disable the gate (self-rescue from a lockout)."""
        _set_gate_key(key, enabled=False)
        typer.echo(f"gate DISABLED — wrote `{key} = false` to {_config_path()}")

    @group.command(name="enable")
    def enable() -> None:
        """Re-enable the gate."""
        _set_gate_key(key, enabled=True)
        typer.echo(f"gate ENABLED — wrote `{key} = true` to {_config_path()}")

    parent.add_typer(group, name=name)


def register_gate_commands(overlay_app: typer.Typer) -> None:
    """Attach the ``gate`` subgroup (heavy-Bash + skill-loading kill-switches)."""
    gate_group = typer.Typer(
        no_args_is_help=True,
        help="Enforcement-gate kill-switches (self-rescue).",
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
        _set_gate_key(GATE_KEY, enabled=False)
        typer.echo(f"gate DISABLED — wrote `{GATE_KEY} = false` to {_config_path()}")

    @gate_group.command(name="enable")
    def enable() -> None:
        """Re-enable the gate."""
        _set_gate_key(GATE_KEY, enabled=True)
        typer.echo(f"gate ENABLED — wrote `{GATE_KEY} = true` to {_config_path()}")

    _register_keyed_gate(
        gate_group,
        name="skill-loading",
        key=SKILL_GATE_KEY,
        label="Skill-loading-on-task gate",
    )

    _register_keyed_gate(
        gate_group,
        name="plan",
        key=PLAN_GATE_KEY,
        label="Plan-before-code edit-block gate",
    )

    _register_keyed_gate(
        gate_group,
        name="config-overwrite",
        key=CONFIG_OVERWRITE_GATE_KEY,
        label="Read-before-overwrite config/dotfile gate",
    )

    _register_keyed_gate(
        gate_group,
        name="completion-claim",
        key=COMPLETION_CLAIM_GATE_KEY,
        label="Completion-claim gate (on-target evidence before done)",
    )

    _register_keyed_gate(
        gate_group,
        name="memory-recall",
        key=MEMORY_RECALL_GATE_KEY,
        label="Cold-tier memory recall injector",
    )

    overlay_app.add_typer(gate_group, name="gate")


def register_fail_open_gate_commands(review_app: typer.Typer) -> None:
    """Attach ``review gate fail-open enable|disable|status`` to the review app.

    The master fail-open switch lives under ``t3 review gate fail-open`` (the
    same surface as the rest of the review-gate machinery). ``enable`` flips
    every OVER-DENY gate to fail-open at once; ``disable`` restores their
    protective posture; ``status`` reports the current state. Default OFF.
    """
    gate_group = typer.Typer(no_args_is_help=True, help="Review-gate master switches.")
    fail_open = typer.Typer(no_args_is_help=True, help="Master fail-open switch for the over-deny gates.")

    @fail_open.command(name="status")
    def status() -> None:
        """Show whether the master fail-open switch is on."""
        if danger_gate_fail_open_is_enabled():
            typer.echo("fail-open ON — every over-deny gate is fail-open (leak gate still fail-closed)")
        else:
            typer.echo("fail-open OFF — over-deny gates enforce normally")

    @fail_open.command(name="enable")
    def enable() -> None:
        """Turn the master fail-open switch ON (self-rescue from an over-deny lockout)."""
        _set_gate_key(DANGER_GATE_FAIL_OPEN_KEY, enabled=True)
        typer.echo(f"fail-open ON — wrote `{DANGER_GATE_FAIL_OPEN_KEY} = true` to {_config_path()}")

    @fail_open.command(name="disable")
    def disable() -> None:
        """Turn the master fail-open switch OFF (restore normal gate enforcement)."""
        _set_gate_key(DANGER_GATE_FAIL_OPEN_KEY, enabled=False)
        typer.echo(f"fail-open OFF — wrote `{DANGER_GATE_FAIL_OPEN_KEY} = false` to {_config_path()}")

    gate_group.add_typer(fail_open, name="fail-open")
    review_app.add_typer(gate_group, name="gate")
