"""A planted ``core.fsmonitor`` / ``core.hooksPath`` must not run through a control-plane git spawn.

The control plane runs git outside the agent sandbox, so a command an agent wrote into a
checkout's config would run with the control plane's authority. Every spawn goes through
``teatree.utils.run``, which pins both settings off for the verbs that would honour them.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from teatree.utils import git_run
from teatree.utils.run import (
    DEVNULL,
    CommandFailedError,
    CompletedProcess,
    run_allowed_to_fail,
    run_bounded_group,
    run_checked,
    run_streamed,
    spawn,
    spawn_byte_pipe,
    spawn_session_leader,
)
from tests._git_repo import git_identity_env, make_git_repo, run_git
from tests._planted_git_config import PlantedConfig, executable, plant_exec_config, recording_script


@dataclass(frozen=True)
class Checkout(PlantedConfig):
    repo: Path

    @property
    def reference_marker(self) -> Path:
        return self.repo.parent / "fetch-reference-transaction"

    def argv(self, *verb: str) -> list[str]:
        return ["git", "-C", str(self.repo), *verb]


@pytest.fixture
def planted(tmp_path: Path) -> Checkout:
    origin = make_git_repo(tmp_path / "origin", bare=True)
    repo = make_git_repo(tmp_path / "repo")
    run_git(repo, "remote", "add", "origin", str(origin))
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    run_git(repo, "add", "tracked.txt")
    run_git(repo, "commit", "-q", "-m", "base")
    run_git(repo, "push", "-q", "origin", "main")
    upstream = tmp_path / "upstream"
    run_git(tmp_path, "clone", "-q", str(origin), str(upstream))
    (upstream / "tracked.txt").write_text("upstream\n", encoding="utf-8")
    run_git(upstream, "add", "tracked.txt")
    run_git(upstream, "commit", "-q", "-m", "upstream")
    run_git(upstream, "push", "-q", "origin", "main")
    assert run_git(origin, "rev-parse", "main") != run_git(repo, "rev-parse", "HEAD")
    assert run_git(repo, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main", check=False) != run_git(
        origin, "rev-parse", "main"
    )
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    config = plant_exec_config(repo, tmp_path)
    recording_script(
        config.hooks_dir / "reference-transaction", tmp_path / "fetch-reference-transaction", "fetched-ref"
    )
    return Checkout(repo=repo, marker=config.marker, hooks_dir=config.hooks_dir)


VERBS: dict[str, Callable[[Path], list[str]]] = {
    "status": lambda repo: ["status", "--porcelain"],
    "ls-files": lambda repo: ["ls-files"],
    "diff": lambda repo: ["diff"],
    "add": lambda repo: ["add", "tracked.txt"],
    "checkout": lambda repo: ["checkout", "-q", "-b", "side"],
    "worktree-add": lambda repo: ["worktree", "add", "-q", str(repo.parent / "wt"), "-b", "wt-branch"],
    "fetch": lambda repo: ["fetch", "-q", "origin"],
}


def _drain_byte_pipe(argv: list[str]) -> int:
    pipe = spawn_byte_pipe(argv)
    with pipe.process:
        pipe.stdin.close()
        pipe.stdout.read()
    return pipe.process.wait()


def _spawned(argv: list[str]) -> int:
    with spawn(argv, stdout=DEVNULL, stderr=DEVNULL) as process:
        return process.wait()


def _session_leader(argv: list[str]) -> int:
    with spawn_session_leader(argv, stdio=(DEVNULL, DEVNULL, DEVNULL)) as process:
        return process.wait()


def _git_run_caller(call: Callable[..., object]) -> Callable[[list[str]], object]:
    def run(argv: list[str]) -> object:
        return call(repo=argv[2], args=argv[3:])

    return run


ENTRY_POINTS: dict[str, Callable[[list[str]], object]] = {
    "run_checked": run_checked,
    "run_allowed_to_fail": lambda argv: run_allowed_to_fail(argv, expected_codes=None),
    "run_bounded_group": lambda argv: run_bounded_group(argv, timeout=30, expected_codes=None),
    "run_streamed": lambda argv: run_streamed(argv, check=False),
    "spawn": _spawned,
    "spawn_session_leader": _session_leader,
    "spawn_byte_pipe": _drain_byte_pipe,
    "git_run.run": _git_run_caller(git_run.run),
    "git_run.run_strict": _git_run_caller(git_run.run_strict),
    "git_run.run_strict_verbatim": _git_run_caller(git_run.run_strict_verbatim),
    "git_run.check": _git_run_caller(git_run.check),
    "git_run.run_with_status": _git_run_caller(git_run.run_with_status),
    "git_run.run_with_status_bounded": lambda argv: git_run.run_with_status(repo=argv[2], args=argv[3:], timeout=30),
}


@pytest.mark.parametrize("verb", VERBS)
def test_a_bare_git_honours_the_planted_config(planted: Checkout, verb: str) -> None:
    """Control: the plant is live for every verb, so an empty marker below is the pin and not a dud fixture."""
    run_git(planted.repo, *VERBS[verb](planted.repo))

    assert planted.fired
    if verb == "fetch":
        assert "fetched-ref" in planted.reference_marker.read_text(encoding="utf-8").splitlines()
        assert run_git(planted.repo, "rev-parse", "origin/main") == run_git(
            planted.repo.parent / "origin", "rev-parse", "main"
        )


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
@pytest.mark.parametrize("verb", VERBS)
def test_no_planted_command_runs_through_a_control_plane_spawn(planted: Checkout, entry_point: str, verb: str) -> None:
    result = ENTRY_POINTS[entry_point](planted.argv(*VERBS[verb](planted.repo)))

    if isinstance(result, bool):
        assert result
    elif isinstance(result, CompletedProcess):
        assert result.returncode == 0
    elif isinstance(result, int):
        assert result == 0
    elif entry_point == "git_run.run" and verb in {"status", "ls-files", "diff"}:
        assert result
    if verb == "add":
        assert run_checked(planted.argv("show", ":tracked.txt")).stdout == "changed\n"
    elif verb == "checkout":
        assert run_checked(planted.argv("symbolic-ref", "--short", "HEAD")).stdout.strip() == "side"
    elif verb == "worktree-add":
        assert (planted.repo.parent / "wt" / ".git").is_file()
    elif verb == "fetch":
        assert "main" in (planted.repo / ".git" / "FETCH_HEAD").read_text(encoding="utf-8")
        assert run_git(planted.repo, "rev-parse", "origin/main") == run_git(
            planted.repo.parent / "origin", "rev-parse", "main"
        )
    assert planted.fired == []
    assert not planted.reference_marker.exists()


def test_an_explicit_caller_hooks_path_cannot_override_the_pin(planted: Checkout) -> None:
    argv = ["git", "-C", str(planted.repo), "-c", f"core.hooksPath={planted.hooks_dir}", "checkout", "-q", "-b", "side"]

    run_checked(argv)

    assert planted.fired == []


def test_an_explicit_caller_fsmonitor_cannot_override_the_pin(planted: Checkout, tmp_path: Path) -> None:
    fsmonitor = tmp_path / "caller-fsmonitor"
    recording_script(fsmonitor, planted.marker, "caller-fsmonitor")
    argv = ["git", "-C", str(planted.repo), "-c", f"core.fsmonitor={fsmonitor}", "status", "--porcelain"]

    run_checked(argv)

    assert planted.fired == []


class TestHooksTheGateLayerDependsOn:
    @pytest.fixture
    def hooked(self, tmp_path: Path) -> Checkout:
        repo = make_git_repo(tmp_path / "repo")
        origin = make_git_repo(tmp_path / "origin", bare=True)
        run_git(repo, "remote", "add", "origin", str(origin))
        return Checkout(repo=repo, hooks_dir=repo / ".git" / "hooks", marker=tmp_path / "marker")

    def test_commit_still_runs_the_pre_commit_and_commit_msg_hooks(self, hooked: Checkout) -> None:
        for name in ("pre-commit", "commit-msg"):
            recording_script(hooked.hooks_dir / name, hooked.marker, name)

        run_checked(hooked.argv("commit", "-q", "--allow-empty", "-m", "x"), env=git_identity_env())

        assert hooked.fired == ["pre-commit", "commit-msg"]

    def test_a_refusing_pre_push_hook_still_refuses_the_push(self, hooked: Checkout) -> None:
        executable(hooked.hooks_dir / "pre-push", "printf 'pre-push-denied\\n' >&2\nexit 1")

        with pytest.raises(CommandFailedError) as raised:
            run_checked(hooked.argv("push", "origin", "main"), env=git_identity_env())

        assert "pre-push-denied" in raised.value.stderr

    def test_a_local_hooks_path_refuses_the_pre_push_gate(self, hooked: Checkout, tmp_path: Path) -> None:
        planted_hooks = tmp_path / "planted-hooks"
        recording_script(planted_hooks / "pre-push", hooked.marker, "planted")
        recording_script(hooked.hooks_dir / "pre-push", hooked.marker, "default")
        run_git(hooked.repo, "config", "core.hooksPath", str(planted_hooks))

        with pytest.raises(CommandFailedError, match=r"untrusted local core\.hooksPath"):
            run_checked(hooked.argv("push", "origin", "main"), env=git_identity_env())

        assert hooked.fired == []

    def test_a_local_hooks_path_refuses_the_pre_commit_gate(self, hooked: Checkout, tmp_path: Path) -> None:
        planted_hooks = tmp_path / "planted-hooks"
        recording_script(planted_hooks / "pre-commit", hooked.marker, "planted")
        recording_script(hooked.hooks_dir / "pre-commit", hooked.marker, "default")
        run_git(hooked.repo, "config", "core.hooksPath", str(planted_hooks))

        with pytest.raises(CommandFailedError, match=r"untrusted local core\.hooksPath"):
            run_checked(hooked.argv("commit", "-q", "--allow-empty", "-m", "x"), env=git_identity_env())

        assert hooked.fired == []

    def test_a_linked_worktree_refuses_the_shared_local_hooks_override(self, hooked: Checkout, tmp_path: Path) -> None:
        linked = tmp_path / "linked"
        run_git(hooked.repo, "worktree", "add", "-q", str(linked), "-b", "side")
        planted_hooks = tmp_path / "planted-hooks"
        recording_script(planted_hooks / "pre-push", hooked.marker, "planted")
        recording_script(hooked.hooks_dir / "pre-push", hooked.marker, "default")
        run_git(hooked.repo, "config", "core.hooksPath", str(planted_hooks))

        with pytest.raises(CommandFailedError, match=r"untrusted local core\.hooksPath"):
            run_bounded_group(["git", "-C", str(linked), "push", "origin", "side"], env=git_identity_env(), timeout=10)

        assert hooked.fired == []

    @pytest.mark.parametrize("verb", ["commit", "push"])
    def test_a_global_hooks_path_keeps_its_refusing_hook(self, hooked: Checkout, tmp_path: Path, verb: str) -> None:
        custom_hooks = tmp_path / "operator-hooks"
        marker = tmp_path / "operator-hook-fired"
        hook_name = "pre-commit" if verb == "commit" else "pre-push"
        executable(custom_hooks / hook_name, f"touch '{marker}'\nexit 1")
        global_config = tmp_path / "global.gitconfig"
        run_git(hooked.repo, "config", "--file", str(global_config), "core.hooksPath", str(custom_hooks))
        env = {**git_identity_env(), "GIT_CONFIG_GLOBAL": str(global_config)}
        args = ("commit", "-q", "--allow-empty", "-m", "x") if verb == "commit" else ("push", "origin", "main")

        with pytest.raises(CommandFailedError):
            run_checked(hooked.argv(*args), env=env)

        assert marker.exists()

    @pytest.mark.parametrize("verb", ["commit", "push"])
    def test_command_line_hooks_path_cannot_replace_gate(self, hooked: Checkout, tmp_path: Path, verb: str) -> None:
        custom_hooks = tmp_path / "planted-hooks"
        marker = tmp_path / "planted-hook-fired"
        hook_name = "pre-commit" if verb == "commit" else "pre-push"
        executable(custom_hooks / hook_name, f"touch '{marker}'\nexit 0")
        args = ("commit", "-q", "--allow-empty", "-m", "x") if verb == "commit" else ("push", "origin", "main")
        argv = ["git", "-C", str(hooked.repo), "-c", f"core.hooksPath={custom_hooks}", *args]

        with pytest.raises(CommandFailedError, match=r"core\.hooksPath"):
            run_checked(argv, env=git_identity_env())

        assert not marker.exists()

    def test_a_merge_still_runs_the_commit_msg_hook(self, hooked: Checkout) -> None:
        run_git(hooked.repo, "checkout", "-q", "-b", "side")
        run_git(hooked.repo, "commit", "-q", "--allow-empty", "-m", "side")
        run_git(hooked.repo, "checkout", "-q", "main")
        recording_script(hooked.hooks_dir / "commit-msg", hooked.marker, "commit-msg")

        run_checked(hooked.argv("merge", "--no-ff", "-q", "-m", "m", "side"), env=git_identity_env())

        assert hooked.fired == ["commit-msg"]


class TestConfigReadersKeepTheRealValue:
    def test_config_get_returns_the_hooks_path(self, planted: Checkout) -> None:
        result = run_checked(planted.argv("config", "--get", "core.hooksPath"))

        assert result.stdout.strip() == str(planted.hooks_dir)

    def test_rev_parse_git_path_returns_the_hooks_dir(self, planted: Checkout) -> None:
        result = run_checked(planted.argv("rev-parse", "--git-path", "hooks"))

        assert result.stdout.strip() == str(planted.hooks_dir)

    def test_the_failure_carries_the_callers_argv_not_the_pinned_one(self, planted: Checkout) -> None:
        argv = planted.argv("status", "--no-such-flag")

        with pytest.raises(CommandFailedError) as raised:
            run_checked(argv)

        assert raised.value.cmd == argv
        assert "core.fsmonitor" not in str(raised.value)


@pytest.mark.parametrize(
    ("probe_output", "probe_rc"),
    [("", 2), ("", 0), ("relative", 0), ("bad\n/path", 0)],
    ids=["probe-failed", "empty-answer", "relative-answer", "multiline-answer"],
)
def test_an_unreadable_common_dir_never_starts_a_hook_bearing_command(
    tmp_path: Path, probe_output: str, probe_rc: int
) -> None:
    fake_bin = tmp_path / "bin"
    fake_git = fake_bin / "git"
    marker = tmp_path / "started"
    executable(
        fake_git,
        'case " $* " in *" rev-parse "*) printf "%s" "$FAKE_OUTPUT"; exit "$FAKE_RC";; esac\nprintf push > "$MARKER"',
    )
    argv = ["git", "push", "origin", "main"]
    env = {"PATH": str(fake_bin), "FAKE_OUTPUT": probe_output, "FAKE_RC": str(probe_rc), "MARKER": str(marker)}

    with pytest.raises(CommandFailedError) as raised:
        run_checked(argv, env=env)

    assert raised.value.cmd == argv
    assert not marker.exists()


@pytest.mark.parametrize("runner", ["allowed-to-fail", "bounded-group"])
def test_a_lenient_push_from_a_non_repo_returns_a_failed_result(tmp_path: Path, runner: str) -> None:
    argv = ["git", "-C", str(tmp_path), "push", "origin", "main"]

    if runner == "bounded-group":
        result = run_bounded_group(argv, expected_codes=None, timeout=10)
    else:
        result = run_allowed_to_fail(argv, expected_codes=None)

    assert result.returncode != 0
    assert result.args == argv


def test_a_lenient_streamed_push_from_a_non_repo_returns_a_failed_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["git", "-C", str(tmp_path), "push", "origin", "main"]

    returncode = run_streamed(argv, check=False)

    assert returncode != 0
    assert capsys.readouterr().err


@pytest.mark.parametrize("runner", ["run_checked", "run_allowed_to_fail", "git_run.run_with_status"])
def test_success_returns_the_callers_git_argv(planted: Checkout, runner: str) -> None:
    argv = planted.argv("status", "--porcelain")
    result = ENTRY_POINTS[runner](argv)

    assert isinstance(result, CompletedProcess)
    assert result.args == argv
