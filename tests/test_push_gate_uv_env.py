# test-path: cross-cutting — asserts over dev/push-gate.sh; no src/teatree/ mirror.
"""The push gate must not reconcile the environment it is importing from.

``dev/push-gate.sh`` runs from a repo that may be a uv workspace MEMBER (a vendored
``vendor/teatree`` inside a fork). A bare ``uv run`` there SYNCS the environment it
resolves, and for a member that is the workspace ROOT's shared ``.venv`` — reconciled
to the member's dependency set while this very gate imports from it. The failure is a
race, not a code defect: the gate dies on ``ImportError``/``ModuleNotFoundError``
naming packages no diff went near, the count varies run to run, and a re-run of
identical code passes.

``uv_project_run_prefix`` (``scripts/hooks/lib/resolve-uv.sh``) exists to close exactly
this, and redirects every member to a hook-owned ``UV_PROJECT_ENVIRONMENT``. These
tests drive the real script against a ``uv`` stub that records the environment each
invocation was pointed at — the only thing that separates "shared" from "ours".
"""

from pathlib import Path

import pytest

from tests._hook_env import HOOK_ENV_NAME
from tests._push_gate_lane import NO_REDIRECT, install_uv_stub, lane_repo, run_lane

#: What ``resolve-uv.sh`` redirects a workspace member to.
_HOOK_ENV = HOOK_ENV_NAME


@pytest.mark.integration
class TestAVendoredLaneNeverSyncsTheSharedEnvironment:
    def test_every_step_runs_against_the_hooks_own_environment(self, tmp_path: Path) -> None:
        install_uv_stub(tmp_path)

        completed, recorded, _ = run_lane(tmp_path, lane_repo(tmp_path, vendored=True))

        assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        assert recorded == [_HOOK_ENV] * 3, (
            "a step ran against the environment uv resolves on its own — for a workspace member that is "
            f"the ROOT's shared `.venv`, reconciled mid-run under the gate importing from it: {recorded}"
        )


@pytest.mark.integration
class TestAStandaloneCheckoutIsLeftAlone:
    """The control: the probe can observe a non-redirected run, and does here."""

    def test_a_repo_that_owns_its_environment_keeps_being_managed_in_place(self, tmp_path: Path) -> None:
        install_uv_stub(tmp_path)

        completed, recorded, _ = run_lane(tmp_path, lane_repo(tmp_path, vendored=False))

        assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        assert recorded == [NO_REDIRECT] * 3, f"a standalone checkout must not be redirected: {recorded}"


@pytest.mark.integration
class TestAnUnresolvableUvFailsClosed:
    def test_no_uv_at_all_refuses_the_push_rather_than_reporting_a_pass(self, tmp_path: Path) -> None:
        completed, recorded, _ = run_lane(tmp_path, lane_repo(tmp_path, vendored=True))

        assert completed.returncode != 0, "a gate that cannot run its own checks must never report a pass"
        assert "timeout(1)" not in completed.stderr, f"refused for timeout, not uv: {completed.stderr!r}"
        assert recorded == []


@pytest.mark.integration
class TestDjangoSettingsModuleDoesNotLeakIntoTheHooksOwnEnvironment:
    """No fork overlay is installed in the hook's own env; an inherited ``DJANGO_SETTINGS_MODULE`` would crash it."""

    def test_a_forks_ambient_settings_module_is_stripped_for_a_workspace_member(self, tmp_path: Path) -> None:
        install_uv_stub(tmp_path)

        completed, _, django_settings = run_lane(
            tmp_path,
            lane_repo(tmp_path, vendored=True),
            extra_env={"DJANGO_SETTINGS_MODULE": "afork.settings"},
        )

        assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        assert django_settings == [NO_REDIRECT] * 3, (
            "an ambient DJANGO_SETTINGS_MODULE reached the hook's own environment, where no fork "
            f"overlay is installed to satisfy it: {django_settings}"
        )

    def test_a_forks_ambient_settings_module_is_stripped_for_a_foreign_environment(self, tmp_path: Path) -> None:
        install_uv_stub(tmp_path)
        script = lane_repo(tmp_path, vendored=False)
        venv_cfg = script.parents[1] / ".venv" / "pyvenv.cfg"
        venv_cfg.parent.mkdir()
        venv_cfg.write_text(f"home = {tmp_path / 'interpreter-of-another-machine'}\n", encoding="utf-8")

        completed, recorded, django_settings = run_lane(
            tmp_path, script, extra_env={"DJANGO_SETTINGS_MODULE": "afork.settings"}
        )

        assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        assert recorded == [_HOOK_ENV] * 3, f"a foreign `.venv` must be redirected away from: {recorded}"
        assert django_settings == [NO_REDIRECT] * 3, (
            f"an ambient DJANGO_SETTINGS_MODULE reached the redirected environment: {django_settings}"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
