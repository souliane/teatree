"""Tests for ``e2e in-tree`` — the runner that runs THIS checkout's Playwright config.

The third source beside ``project`` (the repo's own pytest suite) and ``external``
(a cloned specs repo). What separates it is the absence of every precondition the
other two carry: no specs clone, no frontend port, no env cache, no credentials —
so a browserless CI lane reproduces locally byte-for-byte.
"""

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

import teatree.core.management.commands._e2e_in_tree as in_tree_mod
import teatree.utils.run as utils_run_mod
from tests.teatree_core.management_commands._overlays import _PLAYWRIGHT_ARGS_OVERLAY, _patch_overlays

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)

_GIT = shutil.which("git") or "git"


def _popen_returning(returncode: int) -> MagicMock:
    """A ``Popen`` double: records the call, yields a proc that exits *returncode*."""
    proc = MagicMock()
    proc.stderr = iter(())
    proc.wait.return_value = returncode
    ctx = MagicMock()
    ctx.__enter__.return_value = proc
    ctx.__exit__.return_value = False
    return MagicMock(return_value=ctx)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "client-workspace"
    (root / "e2e" / "api-flow").mkdir(parents=True)
    subprocess.run([_GIT, "init", "-q", "-b", "main"], cwd=root, check=True, capture_output=True)
    return root


@pytest.fixture
def _at_checkout(checkout: Path) -> Iterator[None]:
    with patch.dict(os.environ, {"TEATREE_INVOCATION_CWD": str(checkout)}):
        yield


@pytest.fixture
def popen() -> Iterator[MagicMock]:
    mock = _popen_returning(0)
    with patch.object(utils_run_mod, "Popen", mock):
        yield mock


def _invocation(popen: MagicMock) -> tuple[list[str], str, dict[str, str] | None]:
    args, kwargs = popen.call_args
    return list(args[0]), kwargs["cwd"], kwargs["env"]


@pytest.mark.usefixtures("_at_checkout")
class TestRunsTheCheckoutsOwnLane:
    def test_overlay_resolves_the_config_and_the_e2e_dir_is_the_cwd(self, checkout: Path, popen: MagicMock) -> None:
        with _patch_overlays(_PLAYWRIGHT_ARGS_OVERLAY):
            call_command("e2e", "in-tree", test_path="e2e/api-flow/checkout.spec.ts")

        cmd, cwd, _env = _invocation(popen)
        assert cmd == ["npx", "playwright", "test", "-c", "api.config.ts", "api-flow/checkout.spec.ts"]
        assert Path(cwd) == checkout / "e2e"

    def test_a_whole_lane_directory_is_a_valid_filter(self, popen: MagicMock) -> None:
        with _patch_overlays(_PLAYWRIGHT_ARGS_OVERLAY):
            call_command("e2e", "in-tree", test_path="e2e/api-flow/")

        assert _invocation(popen)[0][-1] == "api-flow/"

    def test_a_path_already_relative_to_the_e2e_dir_is_passed_through(self, popen: MagicMock) -> None:
        with _patch_overlays(_PLAYWRIGHT_ARGS_OVERLAY):
            call_command("e2e", "in-tree", test_path="api-flow/checkout.spec.ts")

        assert _invocation(popen)[0][-1] == "api-flow/checkout.spec.ts"

    def test_explicit_config_overrides_the_overlay_mapping(self, popen: MagicMock) -> None:
        with _patch_overlays(_PLAYWRIGHT_ARGS_OVERLAY):
            call_command("e2e", "in-tree", test_path="e2e/api-flow", config="unit.config.ts")

        assert _invocation(popen)[0] == ["npx", "playwright", "test", "-c", "unit.config.ts", "api-flow"]

    def test_a_config_alone_runs_the_whole_lane(self, popen: MagicMock) -> None:
        with _patch_overlays(_PLAYWRIGHT_ARGS_OVERLAY):
            call_command("e2e", "in-tree", config="unit.config.ts")

        assert _invocation(popen)[0] == ["npx", "playwright", "test", "-c", "unit.config.ts"]

    def test_nothing_about_a_stack_or_a_tenant_reaches_the_run(self, popen: MagicMock) -> None:
        with _patch_overlays(_PLAYWRIGHT_ARGS_OVERLAY):
            call_command("e2e", "in-tree", test_path="e2e/api-flow/")

        assert _invocation(popen)[2] is None

    def test_a_failing_lane_exits_with_playwrights_code(self) -> None:
        with (
            patch.object(utils_run_mod, "Popen", _popen_returning(3)),
            _patch_overlays(_PLAYWRIGHT_ARGS_OVERLAY),
            pytest.raises(SystemExit) as exc,
        ):
            call_command("e2e", "in-tree", test_path="e2e/api-flow/")

        assert exc.value.code == 3


class TestRefusesRatherThanRunTheWrongThing:
    @pytest.mark.usefixtures("_at_checkout")
    def test_a_spec_the_overlay_maps_to_no_config_is_refused(
        self, popen: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _patch_overlays(_PLAYWRIGHT_ARGS_OVERLAY), pytest.raises(SystemExit) as exc:
            call_command("e2e", "in-tree", test_path="e2e/smoke/login.spec.ts")

        assert exc.value.code == 2
        assert popen.call_count == 0
        err = capsys.readouterr().err
        assert "e2e/smoke/login.spec.ts" in err
        assert "--config" in err

    def test_a_cwd_outside_a_checkout_is_refused(
        self, tmp_path: Path, popen: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        loose = tmp_path / "not-a-repo"
        loose.mkdir()
        with (
            patch.dict(os.environ, {"TEATREE_INVOCATION_CWD": str(loose)}),
            _patch_overlays(_PLAYWRIGHT_ARGS_OVERLAY),
            pytest.raises(SystemExit) as exc,
        ):
            call_command("e2e", "in-tree", test_path="e2e/api-flow/")

        assert exc.value.code == 2
        assert popen.call_count == 0
        err = capsys.readouterr().err
        assert str(loose) in err
        assert "not inside a git working tree" in err

    def test_a_checkout_without_the_e2e_dir_is_refused(
        self, tmp_path: Path, popen: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bare = tmp_path / "backend"
        bare.mkdir()
        subprocess.run([_GIT, "init", "-q", "-b", "main"], cwd=bare, check=True, capture_output=True)
        with (
            patch.dict(os.environ, {"TEATREE_INVOCATION_CWD": str(bare)}),
            _patch_overlays(_PLAYWRIGHT_ARGS_OVERLAY),
            pytest.raises(SystemExit) as exc,
        ):
            call_command("e2e", "in-tree", config="unit.config.ts")

        assert exc.value.code == 2
        assert popen.call_count == 0
        err = capsys.readouterr().err
        assert str(bare.resolve()) in err
        assert "has no 'e2e' directory" in err


class TestASpecOutsideTheLaneDirIsRefused:
    """A foreign absolute spec matches nothing under Playwright's cwd — a vacuous pass."""

    def test_an_absolute_spec_from_another_tree_is_refused(self, checkout: Path, tmp_path: Path) -> None:
        with (
            patch.dict(os.environ, {"TEATREE_INVOCATION_CWD": str(checkout)}),
            pytest.raises(in_tree_mod.SpecOutsideLaneDirError),
        ):
            in_tree_mod.resolve_run(
                test_path=str(tmp_path / "other-repo" / "e2e" / "api-flow" / "checkout.spec.ts"),
                config="unit.config.ts",
                e2e_dir="e2e",
                overlay_args=[],
            )


class TestRunsFromASubdirectoryOfTheCheckout:
    def test_the_checkout_root_is_resolved_not_the_cwd(self, checkout: Path, popen: MagicMock) -> None:
        with (
            patch.dict(os.environ, {"TEATREE_INVOCATION_CWD": str(checkout / "e2e" / "api-flow")}),
            _patch_overlays(_PLAYWRIGHT_ARGS_OVERLAY),
        ):
            call_command("e2e", "in-tree", test_path="e2e/api-flow/")

        assert Path(_invocation(popen)[1]) == checkout / "e2e"
