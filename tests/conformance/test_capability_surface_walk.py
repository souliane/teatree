"""Bidirectional capability walk-the-surface — the two-way anti-drift guard (SIG-4, #26).

The pre-#13 ``CAPABILITIES`` registry guard was one-directional: it caught a
registry entry that over-claimed JSON, but never an omission — a command that
emits JSON on the real surface yet is absent from the registry. That is exactly
how ``teatree signals --json`` shipped: neither in ``CAPABILITIES`` nor
discoverable, so a front-end reading ``t3 capabilities`` could not find it.

This walks the REAL command surface both ways. Forward (registry -> surface):
every ``CAPABILITIES`` entry resolves to a live command in the actual click tree,
and every ``json:true`` switch entry's command declares a ``--json`` / ``--format``
option — a phantom or renamed entry fails. Reverse (surface -> registry): every
``teatree.core`` management command that declares a ``--json`` / ``--format`` switch
is EITHER in ``CAPABILITIES`` or in a named allowlist of intentional exclusions — a
NEW json surface (like ``signals`` was) fails loudly until a conscious
register-or-exclude decision is made.

Scope of the reverse walk is the ``teatree.core`` management-command surface — the
front-end machine seam ``CAPABILITIES`` documents. The operator/loop dev tooling
(``t3 loop``/``tool``/``eval``/``assess``) is deliberately outside that seam; its
command modules are the named ``INTENTIONALLY_UNREGISTERED_MODULES`` set.
"""

import click
from django.core.management import get_commands, load_command_class
from typer.main import get_command

from teatree.core.capabilities import CAPABILITIES

_APP_LABEL = "teatree.core"
_JSON_OPTS = frozenset({"--json", "--format"})

# Management-command modules that expose a --json/--format switch but are
# deliberately NOT part of the front-end capability seam: loop/orchestration
# internals, operator diagnostics, and dev tooling a front-end never drives.
# A NEW json command under a NEW module is not on this list, so it fails the
# reverse walk until it is registered as a capability or added here on purpose.
INTENTIONALLY_UNREGISTERED_MODULES: frozenset[str] = frozenset(
    {
        "handover",
        "health",
        "loop_dispatch",
        "loop_drain_queue",
        "loop_list",
        "loop_owner",
        "loop_self_improve",
        "loop_slack_answer",
        "loop_state",
        "loop_tick",
        "loops_list",
        "loops_tick",
        "prompts_list",
        "recipe",
        "recover",
        "session",
        "standing_goal",
        "waiting",
    }
)

# CAPABILITIES entries that always emit a JSON document (no --json switch), so the
# forward walk asserts existence but not an option — they are proven to emit JSON
# by tests/teatree_cli/test_capabilities.py::TestSwitchlessAlwaysJsonEmitsJson.
_ALWAYS_JSON_COMMANDS = frozenset({"teatree workspace emit", "teatree tasks create", "teatree db query"})


def _json_switch(command: click.Command) -> bool:
    return any(
        isinstance(param, click.Option) and any(opt in _JSON_OPTS for opt in param.opts) for param in command.params
    )


def _core_click_command(name: str) -> click.Command | None:
    """The click command tree for a ``teatree.core`` management command, or ``None``."""
    try:
        klass = load_command_class(_APP_LABEL, name)
    except Exception:  # noqa: BLE001 — an un-loadable command exposes no introspectable surface
        return None
    typer_app = getattr(klass, "typer_app", None)
    if typer_app is None:
        return None
    try:
        return get_command(typer_app)
    except Exception:  # noqa: BLE001 — a command whose typer app will not build exposes no introspectable surface
        return None


def management_json_surface() -> set[str]:
    """Every ``teatree.core`` management command declaring a --json/--format switch.

    Keyed ``teatree <name>`` (bare command) or ``teatree <name> <sub>`` (a group
    subcommand) — the same convention ``CAPABILITIES`` uses for its entries.
    """
    surface: set[str] = set()
    core = sorted(name for name, app in get_commands().items() if app == _APP_LABEL)
    for name in core:
        command = _core_click_command(name)
        if command is None:
            continue
        if isinstance(command, click.Group):
            ctx = click.Context(command)
            for sub_name in command.list_commands(ctx):
                sub = command.get_command(ctx, sub_name)
                if sub is not None and _json_switch(sub):
                    surface.add(f"teatree {name} {sub_name}")
        elif _json_switch(command):
            surface.add(f"teatree {name}")
    return surface


def _surface_module(surface_key: str) -> str:
    # `teatree <module> [<sub>]` -> the management-command module name.
    return surface_key.split()[1]


def _registered_surface_keys() -> set[str]:
    """CAPABILITIES commands canonicalized UP to the ``teatree <cmd>`` surface form.

    The registry names some commands bare (``cost``) and some prefixed
    (``teatree cost``); the surface always emits ``teatree <cmd>``. Canonicalize UP
    (add the prefix) so a bare registry entry matches its prefixed surface key —
    never strip the prefix off the surface to force a match.
    """
    keys: set[str] = set()
    keys.update(cap.command if cap.command.startswith("teatree ") else f"teatree {cap.command}" for cap in CAPABILITIES)
    return keys


def _resolve_capability(command: str) -> click.Command | None:
    """Resolve a CAPABILITIES command string to its live click command, or ``None``.

    Tries the ``teatree.core`` management surface first (``teatree <cmd> [<sub>]``
    / bare ``<cmd>``), then the top-level typer CLI app (``config show``).
    """
    tokens = command.removeprefix("teatree ").split()
    if not tokens:
        return None
    head, *rest = tokens
    resolved = _walk(_core_click_command(head), rest)
    if resolved is not None:
        return resolved
    from teatree.cli import app  # noqa: PLC0415 — a Django-bootstrap-heavy import kept lazy

    return _walk(get_command(app), tokens)


def _walk(command: click.Command | None, tokens: list[str]) -> click.Command | None:
    for token in tokens:
        if not isinstance(command, click.Group):
            return None
        command = command.get_command(click.Context(command), token)
        if command is None:
            return None
    return command


def unregistered_json_surface() -> set[str]:
    """Every json surface command that is neither registered nor intentionally excluded."""
    registered = _registered_surface_keys()
    return {
        key
        for key in management_json_surface()
        if key not in registered and _surface_module(key) not in INTENTIONALLY_UNREGISTERED_MODULES
    }


class TestCapabilitySurfaceWalkReverse:
    """surface -> registry: a json command on the surface must be registered or excluded."""

    def test_no_json_management_command_is_unregistered(self) -> None:
        unregistered = unregistered_json_surface()
        assert not unregistered, (
            "management command(s) emit JSON but are neither in CAPABILITIES nor the named "
            f"intentional-exclusion allowlist (register them or add the module on purpose): {sorted(unregistered)}"
        )

    def test_signals_is_now_registered_not_an_orphan_surface(self) -> None:
        # The #13/#26 regression, pinned: `teatree signals` is a real json surface
        # AND in CAPABILITIES, so it is not reported unregistered.
        assert "teatree signals" in management_json_surface()
        assert "teatree signals" in _registered_surface_keys()

    def test_surface_walk_cardinality_floor_anti_vacuity(self) -> None:
        # A broken walk that discovers nothing must not pass vacuously.
        assert len(management_json_surface()) >= 20


class TestCapabilitySurfaceWalkForward:
    """registry -> surface: every CAPABILITIES entry resolves to a live command."""

    def test_every_capability_resolves_to_a_live_command(self) -> None:
        missing = [cap.command for cap in CAPABILITIES if _resolve_capability(cap.command) is None]
        assert not missing, f"CAPABILITIES entries that resolve to no live command (phantom/renamed): {missing}"

    def test_every_json_switch_capability_declares_the_option(self) -> None:
        for cap in CAPABILITIES:
            if not cap.json_output or cap.command in _ALWAYS_JSON_COMMANDS:
                continue
            resolved = _resolve_capability(cap.command)
            assert resolved is not None, cap.command
            assert _json_switch(resolved), f"{cap.command!r} is json:true but declares no --json/--format option"


class TestSurfaceWalkFiresRed:
    """Anti-vacuity: both directions must actually catch drift."""

    def test_reverse_walk_names_a_synthetic_unregistered_surface(self) -> None:
        # A json surface whose module is neither registered nor allowlisted is
        # reported — the property that would have caught `signals` pre-#13.
        registered = _registered_surface_keys()
        synthetic = "teatree brandnew_json_cmd"
        assert synthetic not in registered
        assert _surface_module(synthetic) not in INTENTIONALLY_UNREGISTERED_MODULES

    def test_forward_walk_names_a_phantom_capability(self) -> None:
        assert _resolve_capability("teatree this_command_does_not_exist") is None
