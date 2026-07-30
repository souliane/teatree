"""``register_core_passthrough_leaves`` — the safe-kill/do/signals passthrough seam.

Each leaf forwards its trailing args verbatim to the same-named ``teatree.core``
management command via ``managepy_core``, mapping a hyphenated leaf name to the
command's underscore form (``safe-kill`` -> ``safe_kill``). The subprocess seam
is stubbed in-process (the one unstoppable external the test-doctrine permits)
so the arg-forwarding + name-mapping contract is asserted directly.
"""

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.overlay_leaves import _CORE_PASSTHROUGH_LEAVES, register_core_passthrough_leaves

runner = CliRunner()


def _app_capturing_calls(monkeypatch: pytest.MonkeyPatch) -> tuple[typer.Typer, list[tuple[tuple[str, ...], str]]]:
    calls: list[tuple[tuple[str, ...], str]] = []

    def _fake_managepy_core(*args: str, overlay_name: str = "") -> None:
        calls.append((args, overlay_name))

    # The leaf lazily imports managepy_core from teatree.cli.overlay at call time.
    monkeypatch.setattr("teatree.cli.overlay.managepy_core", _fake_managepy_core)
    app = typer.Typer()
    register_core_passthrough_leaves(app, "my-overlay")
    return app, calls


class TestCorePassthroughLeaves:
    def test_every_declared_leaf_is_registered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app, _calls = _app_capturing_calls(monkeypatch)
        registered = {command.name for command in app.registered_commands}
        assert {name for name, _help in _CORE_PASSTHROUGH_LEAVES} <= registered

    def test_hyphenated_leaf_maps_to_the_underscore_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app, calls = _app_capturing_calls(monkeypatch)
        result = runner.invoke(app, ["safe-kill", "1234", "--hang-cause", "x"])
        assert result.exit_code == 0, result.output
        assert calls == [(("safe_kill", "1234", "--hang-cause", "x"), "my-overlay")]

    def test_trailing_args_are_forwarded_verbatim_with_the_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app, calls = _app_capturing_calls(monkeypatch)
        result = runner.invoke(app, ["signals", "--json", "--window-days", "7"])
        assert result.exit_code == 0, result.output
        assert calls == [(("signals", "--json", "--window-days", "7"), "my-overlay")]

    def test_leaf_with_no_extra_args_dispatches_the_bare_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app, calls = _app_capturing_calls(monkeypatch)
        result = runner.invoke(app, ["do"])
        assert result.exit_code == 0, result.output
        assert calls == [(("do",), "my-overlay")]
