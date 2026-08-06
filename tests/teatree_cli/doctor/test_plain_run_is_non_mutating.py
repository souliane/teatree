"""A plain ``t3 doctor check`` leaves the operator's settings file alone.

``--repair``'s promise is that a plain run reports drift and writes nothing, and the
plugin-registration pass is the one finaliser that WRITES — it used to rewrite
``~/.claude/settings.json`` on every session start. The switch enforcing that promise is
a single ``repair=repair`` at ``run_doctor_checks``'s call to the advisory finalisers, so
it is asserted here by its effect on a drifted settings file rather than by the flag's
value: flipping that one keyword to ``True`` restores the rewrite, and only a file-level
assertion notices.

The paired ``--repair`` case is the control — without it a test that never writes under
either flag would pass on a harness that cannot write at all.
"""

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

import teatree.cli.doctor.app as doctor_app_mod
from teatree.cli.doctor.app import run_doctor_checks

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_PLUGIN_ID = "t3@souliane"
_FINALISERS = "_run_advisory_finalisers"

#: The four checks ``run_doctor_checks`` imports inside its own body, patched where the
#: function binds them (they are not attributes of the doctor app module).
_DEFERRED_CALLS = (
    "teatree.core.gates.schema_guard.doctor_check_self_db_migrations",
    "teatree.cli.doctor.self_heal.run_self_heal_checks",
    "teatree.cli.update._collect_repos",
    "teatree.core.gates.clone_guard.doctor_check_clone_currency",
)

#: Names the aggregate references that are not checks — its own deferred imports of
#: ``django``/``teatree.core``, the exception it catches, and its output builtins.
_NOT_A_CHECK = frozenset({"django", "teatree.core", "ImportError", "typer", "echo", "all"})


@contextlib.contextmanager
def _aggregate_reduced_to_its_finalisers() -> Iterator[None]:
    """Stub every check ``run_doctor_checks`` calls except the advisory finalisers.

    The aggregate probes MCP, Slack and the forge and re-points the running install as
    it goes, so it can neither run for real under test nor have its forty-odd checks
    enumerated by hand without rotting. Reading the names off the function's own code
    object keeps the isolation exact as checks come and go.
    """
    with contextlib.ExitStack() as stack:
        for name in run_doctor_checks.__code__.co_names:
            if name != _FINALISERS and callable(getattr(doctor_app_mod, name, None)):
                stack.enter_context(mock.patch.object(doctor_app_mod, name, return_value=True))
        for target in _DEFERRED_CALLS:
            stack.enter_context(mock.patch(target, return_value=True))
        yield


@pytest.fixture
def drifted_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An operator home whose ``settings.json`` does not yet enable the t3 plugin."""
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "pyproject.toml").write_text('[project]\nname = "teatree"\n', encoding="utf-8")
    monkeypatch.setenv("T3_REPO", str(clone))

    home = tmp_path / "home"
    (home / ".claude" / "plugins").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(git status)"]}}, indent=2) + "\n", encoding="utf-8")
    return settings


class TestPlainRunNeverWritesTheOperatorsSettings:
    def test_a_plain_run_leaves_the_drifted_file_byte_identical(self, drifted_settings: Path) -> None:
        before = drifted_settings.read_bytes()

        with _aggregate_reduced_to_its_finalisers():
            run_doctor_checks(repair=False)

        assert drifted_settings.read_bytes() == before, (
            "a plain `t3 doctor check` must report the plugin-registration drift and write "
            "nothing — the operator's settings file was rewritten"
        )

    def test_a_repair_run_does_write_it(self, drifted_settings: Path) -> None:
        with _aggregate_reduced_to_its_finalisers():
            run_doctor_checks(repair=True)

        written = json.loads(drifted_settings.read_text(encoding="utf-8"))
        assert written["enabledPlugins"][_PLUGIN_ID] is True
        assert written["permissions"] == {"allow": ["Bash(git status)"]}

    def test_the_isolation_accounts_for_every_name_the_aggregate_calls(self) -> None:
        # A check added with a deferred import would otherwise run for real here — against
        # the operator's own machine — and only show up as a slow, occasionally-mutating test.
        deferred = {target.rsplit(".", 1)[-1] for target in _DEFERRED_CALLS}
        deferred |= {target.rsplit(".", 1)[0] for target in _DEFERRED_CALLS}
        unaccounted = (
            {name for name in run_doctor_checks.__code__.co_names if not callable(getattr(doctor_app_mod, name, None))}
            - _NOT_A_CHECK
            - deferred
        )

        assert not unaccounted, f"add these to _DEFERRED_CALLS (or _NOT_A_CHECK): {sorted(unaccounted)}"
