"""Regression tests for the prek-install step during worktree provisioning.

souliane/teatree#1253 — sub-agent commits bypassed the migration-scoping
pre-commit hook because ``prek install`` failed silently during worktree
provisioning. The runner only logged a warning and returned ok; the worktree
then served as a coding environment with no pre-commit gate at all, so the
next ``git commit`` shipped unchecked.

Two failure paths covered here:

1. **Real subprocess path**: when ``.pre-commit-config.yaml`` is present in a
    freshly created worktree and ``prek`` is on PATH, ``_setup_worktree_dir``
    must produce an executable hook script at the resolved hooks directory
    (``git rev-parse --git-path hooks``). A worktree without that hook script
    is a silent bypass surface.

2. **Failure surfacing**: when ``prek install`` fails (binary missing, exit
    non-zero, the worktree's ``.pre-commit-config.yaml`` rejected), the
    provisioning runner MUST surface the failure (``RunnerResult.ok == False``)
    instead of swallowing it as a warning. Returning ok-with-a-warning is the
    bypass class the issue reports.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core import prek_hook
from teatree.core.models import Ticket, Worktree
from teatree.core.provision.step_runner import StepResult
from teatree.core.runners.worktree_provision import WorktreeProvisionRunner, _setup_worktree_dir

# Minimal valid prek/pre-commit config. The hook body itself never runs in
# these tests (we either drive ``_setup_worktree_dir`` directly with a real
# prek binary, or stub ``run_step`` entirely); the file only needs to exist
# so the ``.pre-commit-config.yaml`` gate in ``_setup_worktree_dir`` opens.
# Indentation inside the literal is 4-space-multiple to keep editorconfig
# happy on the *.py side while still being valid YAML on disk.
_HOOK_YAML = (
    "default_install_hook_types: [pre-commit, commit-msg]\n"
    "default_stages: [pre-commit, manual]\n"
    "fail_fast: false\n"
    "repos:\n"
    "    - repo: local\n"
    "      hooks:\n"
    "          - id: noop\n"
    "            name: noop\n"
    "            language: system\n"
    "            entry: 'true'\n"
    "            pass_filenames: false\n"
    "            always_run: true\n"
)

_GIT_BIN = shutil.which("git") or "/usr/bin/git"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [_GIT_BIN, "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def real_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Initialise a main git clone and add a worktree off it.

    Returns ``(main_clone, worktree_path)``. The worktree carries a real
    ``.pre-commit-config.yaml`` so the hook-install path is exercised end to
    end.
    """
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "--initial-branch=main", "-q")
    _git(main, "config", "user.email", "test@example.com")
    _git(main, "config", "user.name", "test")
    _git(main, "commit", "--allow-empty", "-m", "root")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt), "-b", "feature/1253")
    (wt / ".pre-commit-config.yaml").write_text(_HOOK_YAML)
    return main, wt


@pytest.mark.skipif(shutil.which("prek") is None, reason="prek not on PATH")
class TestPrekInstallProducesHookFile:
    """The real provisioning path must install a usable pre-commit hook script.

    This is the structural invariant the #1253 bypass violated: every
    provisioned worktree's resolved hooks directory must contain an executable
    ``pre-commit`` script that dispatches to ``prek hook-impl``. Without it,
    nothing intercepts ``git commit`` and the next sub-agent commit slips
    through unchecked.
    """

    def test_setup_writes_executable_pre_commit_hook(self, real_worktree: tuple[Path, Path]) -> None:
        _main, wt = real_worktree
        worktree = MagicMock()
        overlay = MagicMock()
        overlay.get_envrc_lines.return_value = []

        _setup_worktree_dir(str(wt), worktree, overlay)

        # ``git rev-parse --git-path hooks`` resolves to the SHARED hooks dir
        # for worktrees (main clone's ``.git/hooks``), which is where prek
        # writes the dispatch scripts. The worktree-first invariant only
        # holds if THAT file is present and executable.
        proc = subprocess.run(
            [_GIT_BIN, "-C", str(wt), "rev-parse", "--git-path", "hooks"],
            check=True,
            capture_output=True,
            text=True,
        )
        hooks_dir = (wt / proc.stdout.strip()).resolve()
        pre_commit = hooks_dir / "pre-commit"
        assert pre_commit.is_file(), f"expected pre-commit hook at {pre_commit}"
        # Executable bit — git refuses to run a non-executable hook script.
        mode = pre_commit.stat().st_mode
        assert mode & 0o100, f"pre-commit hook at {pre_commit} is not executable (mode={mode:o})"
        # The dispatch script must reference prek's hook-impl entry point;
        # a stub that ``exit 0``s would also satisfy the executable bit but
        # is the very bypass class we're guarding against.
        body = pre_commit.read_text()
        assert "prek" in body.lower(), f"pre-commit hook at {pre_commit} does not dispatch to prek:\n{body}"


@pytest.mark.skipif(shutil.which("prek") is None, reason="prek not on PATH")
class TestPrekInstallTolerantOfRedundantHooksPath:
    """A redundant ``core.hooksPath`` must not silently disable commit gating.

    souliane/teatree#2706 — a provisioned clone carried a LOCAL
    ``core.hooksPath`` pointing at git's own default hooks dir
    (``<git-common-dir>/hooks``). The value is redundant (it equals the
    default), but its mere presence makes ``prek install`` refuse (it
    cowardly refuses to install hooks while ``core.hooksPath`` is set), so
    provisioning installed NO hook and every ``git commit`` / ``git push``
    ran nothing — a ruff-failing push reached CI as the only gate.
    ``install()`` must clear a redundant ``core.hooksPath`` so prek installs
    a real hook, while NEVER clobbering a genuinely custom one (that would
    change the user's intended hook routing).
    """

    def _resolved(self, path: Path) -> Path:
        return path.resolve()

    def _default_hooks_dir(self, wt: Path) -> Path:
        # ``--git-path hooks`` resolves the SHARED hooks dir even from a
        # worktree (it points at the main clone's ``.git/hooks``), matching
        # production's ``prek_hook._shared_hooks_dir`` rather than the
        # worktree-private gitdir that ``--git-common-dir`` returns relative
        # to the worktree.
        rel = subprocess.run(
            [_GIT_BIN, "-C", str(wt), "rev-parse", "--git-path", "hooks"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        path = Path(rel)
        if not path.is_absolute():
            path = (wt / path).resolve()
        return path.resolve()

    def test_redundant_hookspath_is_cleared_and_hook_installs(self, real_worktree: tuple[Path, Path]) -> None:
        main, wt = real_worktree
        default_hooks = self._default_hooks_dir(wt)
        # Set the redundant LOCAL core.hooksPath — the exact #2706 footgun.
        # Its mere presence makes ``prek install`` refuse (it cowardly refuses
        # to install hooks while ``core.hooksPath`` is set) on a real
        # provisioned clone, leaving NO active pre-commit/pre-push hook.
        # ``install()`` must clear the redundant value so the hook installs.
        _git(main, "config", "--local", "core.hooksPath", str(default_hooks))

        result = prek_hook.install(str(wt))

        assert result.success, f"install() must succeed after clearing a redundant hooksPath: {result.error!r}"
        # The redundant local value must be gone — this is the load-bearing
        # observable: on the unfixed code nothing unsets it, so it stays set.
        readback = subprocess.run(
            [_GIT_BIN, "-C", str(wt), "config", "--local", "--get", "core.hooksPath"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert readback.returncode != 0, (
            f"redundant local core.hooksPath must be unset, still: {readback.stdout.strip()!r}"
        )
        # And a real, executable, prek-dispatching pre-commit hook now exists.
        pre_commit = default_hooks / "pre-commit"
        assert pre_commit.is_file(), f"expected pre-commit hook at {pre_commit}"
        assert pre_commit.stat().st_mode & 0o100, f"pre-commit hook at {pre_commit} is not executable"
        assert "prek" in pre_commit.read_text().lower()

    def test_custom_hookspath_is_preserved_not_clobbered(self, real_worktree: tuple[Path, Path]) -> None:
        main, wt = real_worktree
        custom = wt / "custom-hooks"
        custom.mkdir()
        _git(main, "config", "--local", "core.hooksPath", str(custom))

        prek_hook.install(str(wt))

        # A genuinely custom hooksPath is the user's intent — never silently
        # discarded. It must still be configured after install().
        readback = subprocess.run(
            [_GIT_BIN, "-C", str(wt), "config", "--local", "--get", "core.hooksPath"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert readback.returncode == 0, "a custom (non-redundant) core.hooksPath must be preserved"
        assert self._resolved(Path(readback.stdout.strip())) == self._resolved(custom), (
            f"custom core.hooksPath was changed: {readback.stdout.strip()!r}"
        )


class TestPrekInstallFailureSurfaces(TestCase):
    """When ``prek install`` exits non-zero, the runner must surface the failure.

    Today's behaviour: log a warning and return ``RunnerResult(ok=True)`` —
    the worktree is reported "provisioned" with no pre-commit gate at all,
    which is the structural bypass surface from souliane/teatree#1253.

    Expected behaviour (this regression test enforces): the runner returns
    ``RunnerResult(ok=False)`` so the worktree FSM does NOT flip to
    PROVISIONED, the CLI prints a diagnosable error, and the operator fixes
    the prek install problem before any commit happens.
    """

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/1253",
        )

    def _worktree(self, tmp_path: Path) -> Worktree:
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir(parents=True, exist_ok=True)
        (wt_dir / ".pre-commit-config.yaml").write_text(_HOOK_YAML)
        return Worktree.objects.create(
            ticket=self.ticket,
            repo_path="backend",
            branch="b",
            db_name="",
            extra={"worktree_path": str(wt_dir)},
        )

    def test_failed_prek_install_makes_provision_runner_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = self._worktree(Path(tmp))

            overlay = MagicMock()
            overlay.get_envrc_lines.return_value = []
            overlay.get_db_import_strategy.return_value = None
            overlay.get_provision_steps.return_value = []
            overlay.get_post_db_steps.return_value = []
            overlay.get_pre_run_steps.return_value = []
            overlay.get_run_commands.return_value = {}
            overlay.get_reset_passwords_command.return_value = ""
            overlay.get_env_extra.return_value = {}
            overlay.get_health_checks.return_value = []
            overlay.metadata.get_skill_metadata.return_value = {}

            # Stub ``run_step`` so direnv passes and prek install fails — the
            # real binary may or may not be on PATH on a contributor's
            # machine, so we model the failure directly rather than depend
            # on its absence. ``prek install`` runs inside ``prek_hook``, so the
            # step factory there is patched too (direnv stays in the runner).
            def fake_run_step(name: str, *_args: object, **_kwargs: object) -> StepResult:
                if name == "prek-install":
                    return StepResult(
                        name=name,
                        success=False,
                        error="prek: command not found",
                    )
                return StepResult(name=name, success=True)

            with (
                patch(
                    "teatree.core.runners.worktree_provision.run_step",
                    side_effect=fake_run_step,
                ),
                patch(
                    "teatree.core.prek_hook.run_step",
                    side_effect=fake_run_step,
                ),
                patch("teatree.core.runners.worktree_provision.write_env_cache", return_value=None),
            ):
                result = WorktreeProvisionRunner(worktree, overlay=overlay).run()

            assert not result.ok, (
                "prek install failed but the provision runner returned ok=True — "
                "this is the silent-bypass class from souliane/teatree#1253. "
                f"detail={result.detail!r}"
            )
            assert "prek" in result.detail.lower(), (
                f"failure detail must name prek so the operator can fix it: {result.detail!r}"
            )
