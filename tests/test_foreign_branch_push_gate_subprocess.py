# test-path: cross-cutting — drives hook_router.py (hooks/) as a subprocess; no src/teatree/ mirror.
"""SUBPROCESS-level liveness: the incident's own force-push must not move the ref.

The incident was a multi-line Bash call that force-pushed a colleague's branch.
Every in-process test here patches the forge seam and calls the handler directly;
only this file drives the real ``hook_router.main()`` in a fresh, un-bootstrapped
subprocess — the exact shape the harness runs — against a real bare repo, and
then checks the REF rather than the exit code alone.

The kill-switch case is the anti-vacuity control: with the gate off, the same
harness runs the same command and the ref DOES move. Without it, "the ref did
not move" would be satisfied by a harness that never pushes anything.

The remote is the RELATIVE ``../origin.git`` on purpose: the real forge seam
reads an ABSOLUTE bare-repo path as a GitHub slug, which would refuse for an
unreachable forge instead of on the commit authorship this file is about.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

HOOK_ROUTER = Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "hook_router.py"
_REPO_ROOT = HOOK_ROUTER.parent.parent.parent

OUR_EMAIL = "us@example.com"
THEIR_EMAIL = "colleague@example.com"

_GATE_ON: dict[str, object] = {
    "foreign_branch_push_gate_enabled": True,
    "unknown_repo_push_gate_enabled": False,
    "orchestrator_bash_gate_enabled": False,
}
_GATE_OFF: dict[str, object] = {**_GATE_ON, "foreign_branch_push_gate_enabled": False}
_FAIL_OPEN_ON: dict[str, object] = {**_GATE_ON, "danger_gate_fail_open": True}

_DRIVER = """
import io, sys, json
import hooks.scripts.hook_router as r
sys.argv = ["hook_router.py", "--event", "PreToolUse"]
sys.stdin = io.StringIO(json.dumps({payload}))
r.main()
"""

_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _seed_config_db(path: Path, rows: dict[str, object]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting "
        "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    for key, value in rows.items():
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)", ("", key, json.dumps(value))
        )
    conn.commit()
    conn.close()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],  # noqa: S607 — real git against a repo under tmp_path.
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


def _commit(cwd: Path, name: str, *, email: str, author: str) -> None:
    (cwd / name).write_text(name, encoding="utf-8")
    _git(cwd, "add", name)
    _git(
        cwd,
        "-c",
        f"user.email={email}",
        "-c",
        f"user.name={author}",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        name,
    )


def _remote_oid(work: Path, branch: str = "theirs") -> str:
    return _git(work, "ls-remote", "--heads", "origin", f"refs/heads/{branch}").split()[0]


class _Scene:
    """A colleague's branch on a real bare origin, and our clone sitting on it."""

    def __init__(self, root: Path) -> None:
        self.origin = root / "origin.git"
        self.origin.mkdir()
        _git(self.origin, "init", "--bare", "--initial-branch=main", ".")

        seed = root / "seed"
        seed.mkdir()
        _git(seed, "init", "--initial-branch=main", ".")
        _git(seed, "remote", "add", "origin", str(self.origin))
        _commit(seed, "README.md", email=OUR_EMAIL, author="Us")
        _git(seed, "push", "-q", "origin", "main")
        _git(seed, "checkout", "-q", "-b", "theirs")
        _commit(seed, "theirs.txt", email=THEIR_EMAIL, author="Colleague")
        _git(seed, "push", "-q", "origin", "theirs")

        self.work = root / "work"
        _git(root, "clone", "-q", str(self.origin), str(self.work))
        _git(self.work, "remote", "set-url", "origin", "../origin.git")
        _git(self.work, "config", "user.email", OUR_EMAIL)
        _git(self.work, "config", "user.name", "Us")
        _git(self.work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        _commit(self.work, "ours.txt", email=OUR_EMAIL, author="Us")
        self.their_oid = _remote_oid(self.work)
        self.our_head = _git(self.work, "rev-parse", "HEAD")


@pytest.fixture
def scene(tmp_path: Path) -> _Scene:
    return _Scene(tmp_path)


@pytest.fixture
def home() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _drive(command: str, scene: _Scene, home: Path, *, rows: dict[str, object]) -> subprocess.CompletedProcess[str]:
    if not (home / "config.sqlite3").exists():
        _seed_config_db(home / "config.sqlite3", rows)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(scene.work),
        "session_id": "subproc-foreign-push",
    }
    return subprocess.run(
        [sys.executable, "-c", _DRIVER.format(payload=json.dumps(payload))],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={
            **os.environ,
            **_GIT_ENV,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONPATH": str(_REPO_ROOT),
            "T3_CONFIG_DB": str(home / "config.sqlite3"),
        },
    )


def _run_if_allowed(result: subprocess.CompletedProcess[str], command: str, scene: _Scene) -> None:
    """What the harness does with the hook's verdict: run the command only on exit 0."""
    if result.returncode == 0:
        subprocess.run(  # noqa: S602 — the harness runs the agent's command through a shell; that is the point.
            command,
            shell=True,
            cwd=scene.work,
            check=False,
            capture_output=True,
            env={**os.environ, **_GIT_ENV},
        )


_INCIDENT = "cd {work}\ngit push --force origin theirs"


class TestTheIncidentsForcePushCannotLand:
    def test_the_ref_does_not_move_and_the_refusal_names_the_author(self, scene: _Scene, home: Path) -> None:
        command = _INCIDENT.format(work=scene.work)
        result = _drive(command, scene, home, rows=_GATE_ON)
        _run_if_allowed(result, command, scene)

        assert result.returncode == 2, f"expected a deny; stdout={result.stdout!r} stderr={result.stderr!r}"
        decision = json.loads(result.stdout)
        assert decision["permissionDecision"] == "deny"
        reason = decision["permissionDecisionReason"]
        assert "REFUSED: `git push --force origin theirs`" in reason
        assert THEIR_EMAIL in reason
        assert _remote_oid(scene.work) == scene.their_oid

    def test_three_identical_retries_all_deny(self, scene: _Scene, home: Path) -> None:
        """The deny-circuit breaker must classify this as SAFETY and never relax it."""
        command = _INCIDENT.format(work=scene.work)
        codes = []
        for _attempt in range(3):
            result = _drive(command, scene, home, rows=_GATE_ON)
            _run_if_allowed(result, command, scene)
            codes.append(result.returncode)
        assert codes == [2, 2, 2]
        assert _remote_oid(scene.work) == scene.their_oid

    def test_the_non_force_push_is_refused_through_the_shared_fail_open_chain(self, scene: _Scene, home: Path) -> None:
        command = f"cd {scene.work}\ngit push origin theirs"
        result = _drive(command, scene, home, rows=_GATE_ON)
        _run_if_allowed(result, command, scene)
        assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert _remote_oid(scene.work) == scene.their_oid

    def test_with_the_gate_off_the_same_harness_moves_the_ref(self, scene: _Scene, home: Path) -> None:
        """Anti-vacuity: without this control, 'the ref did not move' proves nothing."""
        command = _INCIDENT.format(work=scene.work)
        result = _drive(command, scene, home, rows=_GATE_OFF)
        _run_if_allowed(result, command, scene)
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert _remote_oid(scene.work) != scene.their_oid
        assert _remote_oid(scene.work) == scene.our_head


class TestPushingTheDefaultBranchItselfStillLands:
    """`<oid>..<oid>` is empty for EVERY default-branch push — arithmetic, not evidence.

    Reading that emptiness as "nothing on this branch says whose it is" refuses
    `git push origin main` in every repo, which the in-process ladder cannot show
    on its own: only driving the real hook and then reading the REF proves the
    push actually landed.
    """

    def test_our_own_commit_on_main_reaches_the_remote(self, scene: _Scene, home: Path) -> None:
        _git(scene.work, "checkout", "-q", "main")
        _commit(scene.work, "on-main.txt", email=OUR_EMAIL, author="Us")
        pushed = _git(scene.work, "rev-parse", "HEAD")
        command = f"cd {scene.work}\ngit push origin main"

        result = _drive(command, scene, home, rows=_GATE_ON)
        _run_if_allowed(result, command, scene)

        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert _remote_oid(scene.work, "main") == pushed


_HIDDEN_FORCE = "cd {work} && bash -s -- git push origin main </dev/null ; git push --force origin theirs"


class TestAForceHiddenBehindAnUnreadableSiblingCannotLand:
    """The fail-open bypass must come from EVERY refusing item, not the first read.

    A non-force `Unread` anywhere in the command was preferred over the real
    `--force`, so the refusal routed through the very chain the master fail-open
    switch releases — and the colleague's ref moved.
    """

    def test_the_ref_does_not_move_with_the_master_fail_open_on(self, scene: _Scene, home: Path) -> None:
        command = _HIDDEN_FORCE.format(work=scene.work)
        result = _drive(command, scene, home, rows=_FAIL_OPEN_ON)
        _run_if_allowed(result, command, scene)

        assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert json.loads(result.stdout)["permissionDecision"] == "deny"
        assert _remote_oid(scene.work) == scene.their_oid

    def test_with_the_gate_off_the_same_harness_moves_the_ref(self, scene: _Scene, home: Path) -> None:
        """Anti-vacuity: without this control, "the ref did not move" proves nothing."""
        command = _HIDDEN_FORCE.format(work=scene.work)
        result = _drive(command, scene, home, rows=_GATE_OFF)
        _run_if_allowed(result, command, scene)

        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert _remote_oid(scene.work) == scene.our_head


# A heredoc span runs from `<<DELIM` to its terminator, so the opener LINE's tail
# went with the body. Each left-hand side is chosen so the push really RUNS: `cat`
# succeeds, so only a FAILING one reaches the `||` branch.
_HEREDOC_OPENER_SHAPES = (
    ("cat <<'EOF' && git push --force origin theirs\nnote\nEOF", "and"),
    ("false <<'EOF' || git push --force origin theirs\nnote\nEOF", "or"),
    ("cat <<'EOF' ; git push --force origin theirs\nnote\nEOF", "semicolon"),
    ("cat <<'EOF' | git push --force origin theirs\nnote\nEOF", "pipe"),
    ("cat <<-EOF && git push --force origin theirs\n\tnote\n\tEOF", "tab-stripped"),
)


class TestAPushChainedOnAHeredocOpenerCannotLand:
    """The opener's tail after the redirect is ordinary shell, and must be READ.

    Stripping the span took that tail with it, so the gate produced no push and
    no unread and allowed the command unjudged — a fourth outcome, under which
    the colleague's ref moved.
    """

    @pytest.mark.parametrize(
        "opener", [shape for shape, _ in _HEREDOC_OPENER_SHAPES], ids=[name for _, name in _HEREDOC_OPENER_SHAPES]
    )
    def test_the_ref_does_not_move_for_any_chaining_operator(self, opener: str, scene: _Scene, home: Path) -> None:
        command = f"cd {scene.work}\n{opener}"
        result = _drive(command, scene, home, rows=_GATE_ON)
        _run_if_allowed(result, command, scene)

        assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert json.loads(result.stdout)["permissionDecision"] == "deny"
        assert _remote_oid(scene.work) == scene.their_oid

    @pytest.mark.parametrize(
        "opener", [shape for shape, _ in _HEREDOC_OPENER_SHAPES], ids=[name for _, name in _HEREDOC_OPENER_SHAPES]
    )
    def test_with_the_gate_off_the_same_harness_moves_the_ref(self, opener: str, scene: _Scene, home: Path) -> None:
        """Anti-vacuity: without this control, "the ref did not move" proves nothing."""
        command = f"cd {scene.work}\n{opener}"
        result = _drive(command, scene, home, rows=_GATE_OFF)
        _run_if_allowed(result, command, scene)

        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert _remote_oid(scene.work) == scene.our_head
