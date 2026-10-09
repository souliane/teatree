import subprocess
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

import teatree.provisioning.skill_provenance as provenance_module
from teatree.provisioning.skill_provenance import (
    GitProbeState,
    SkillInstallationProber,
    SkillInstallKind,
    SkillProvenanceProbe,
)


class RecordedRunner:
    def __init__(self, *responses: subprocess.CompletedProcess[str]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(command))
        return self.responses.popleft()


class ScriptedRunner:
    def __init__(self, *steps: subprocess.CompletedProcess[str] | OSError) -> None:
        self.steps = deque(steps)

    def __call__(self, _command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        step = self.steps.popleft()
        if isinstance(step, OSError):
            raise step
        return step


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=("git",), returncode=returncode, stdout=stdout, stderr=stderr)


def test_symlink_probe_fetches_origin_and_records_full_main_provenance(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    front_door = tmp_path / ".claude" / "skills" / "review"
    front_door.parent.mkdir(parents=True)
    front_door.symlink_to(checkout, target_is_directory=True)
    local_sha = "1" * 40
    default_sha = "2" * 40
    runner = RecordedRunner(
        _completed(f"{checkout}\n"),
        _completed(),
        _completed(f"{local_sha}\n"),
        _completed("feature/dashboard\n"),
        _completed("origin/main\n"),
        _completed(f"{default_sha}\n"),
        _completed("3\t2\n"),
    )

    fact = SkillProvenanceProbe(runner=runner).probe(front_door)

    assert fact.kind is SkillInstallKind.SYMLINK
    assert fact.link_target == str(checkout)
    assert fact.git is not None
    assert fact.git.state is GitProbeState.KNOWN
    assert fact.git.local_sha == local_sha
    assert fact.git.branch == "feature/dashboard"
    assert fact.git.detached is False
    assert fact.git.default_branch == "main"
    assert fact.git.default_sha == default_sha
    assert fact.git.ahead == 3
    assert fact.git.behind == 2
    assert fact.git.newer_on_default is True
    assert fact.git.diagnostic is None
    assert runner.calls == [
        ("git", "-C", str(checkout), "rev-parse", "--show-toplevel"),
        ("git", "-C", str(checkout), "fetch", "origin"),
        ("git", "-C", str(checkout), "rev-parse", "HEAD"),
        ("git", "-C", str(checkout), "symbolic-ref", "--quiet", "--short", "HEAD"),
        ("git", "-C", str(checkout), "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"),
        ("git", "-C", str(checkout), "rev-parse", "refs/remotes/origin/main"),
        ("git", "-C", str(checkout), "rev-list", "--left-right", "--count", "HEAD...refs/remotes/origin/main"),
    ]


def test_probe_distinguishes_missing_copy_and_broken_symlink(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    copied = tmp_path / "copied"
    copied.mkdir()
    broken = tmp_path / "broken"
    broken.symlink_to(tmp_path / "absent", target_is_directory=True)
    runner = RecordedRunner()
    probe = SkillProvenanceProbe(runner=runner)

    missing_fact = probe.probe(missing)
    copied_fact = probe.probe(copied)
    broken_fact = probe.probe(broken)

    assert missing_fact.kind is SkillInstallKind.MISSING
    assert missing_fact.git is None
    assert copied_fact.kind is SkillInstallKind.COPY
    assert copied_fact.git is None
    assert broken_fact.kind is SkillInstallKind.BROKEN_SYMLINK
    assert broken_fact.link_target == str(tmp_path / "absent")
    assert broken_fact.git is not None
    assert broken_fact.git.state is GitProbeState.UNKNOWN
    assert broken_fact.git.diagnostic == "symlink target is missing"
    assert runner.calls == []


def test_symlink_filesystem_probe_failure_is_safe_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    front_door = tmp_path / "review"
    front_door.symlink_to(checkout, target_is_directory=True)

    def fail_readlink(_path: Path) -> Path:
        message = "secret mount detail"
        raise OSError(message)

    monkeypatch.setattr(Path, "readlink", fail_readlink)

    fact = SkillProvenanceProbe(runner=RecordedRunner()).probe(front_door)

    assert fact.kind is SkillInstallKind.SYMLINK
    assert fact.link_target is None
    assert fact.git is not None
    assert fact.git.state is GitProbeState.UNKNOWN
    assert fact.git.diagnostic == "symlink probe failed"
    assert "secret mount detail" not in repr(fact)


def test_detached_probe_falls_back_to_origin_master(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    front_door = tmp_path / "review"
    front_door.symlink_to(checkout, target_is_directory=True)
    local_sha = "3" * 40
    default_sha = "4" * 40
    runner = RecordedRunner(
        _completed(f"{checkout}\n"),
        _completed(),
        _completed(f"{local_sha}\n"),
        _completed(returncode=1),
        _completed(returncode=1),
        _completed(returncode=1),
        _completed(),
        _completed(f"{default_sha}\n"),
        _completed("0 0\n"),
    )

    fact = SkillProvenanceProbe(runner=runner).probe(front_door)

    assert fact.git is not None
    assert fact.git.state is GitProbeState.KNOWN
    assert fact.git.branch is None
    assert fact.git.detached is True
    assert fact.git.default_branch == "master"
    assert fact.git.ahead == 0
    assert fact.git.behind == 0
    assert fact.git.newer_on_default is False
    assert ("git", "-C", str(checkout), "show-ref", "--verify", "--quiet", "refs/remotes/origin/main") in runner.calls
    assert ("git", "-C", str(checkout), "show-ref", "--verify", "--quiet", "refs/remotes/origin/master") in runner.calls


def test_fetch_failure_records_safe_unknown_state(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    front_door = tmp_path / "review"
    front_door.symlink_to(checkout, target_is_directory=True)
    runner = RecordedRunner(
        _completed(f"{checkout}\n"),
        _completed(stderr="authorization: bearer top-secret", returncode=128),
    )

    fact = SkillProvenanceProbe(runner=runner).probe(front_door)

    assert fact.git is not None
    assert fact.git.state is GitProbeState.UNKNOWN
    assert fact.git.diagnostic == "origin fetch failed"
    assert "top-secret" not in repr(fact)


def test_non_git_symlink_records_safe_unknown_state(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    front_door = tmp_path / "review"
    front_door.symlink_to(checkout, target_is_directory=True)
    runner = RecordedRunner(_completed(stderr="private path", returncode=128))

    fact = SkillProvenanceProbe(runner=runner).probe(front_door)

    assert fact.git is not None
    assert fact.git.state is GitProbeState.UNKNOWN
    assert fact.git.diagnostic == "git repository probe failed"
    assert "private path" not in repr(fact)


def test_default_command_runner_uses_the_shared_subprocess_chokepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = _completed()
    calls: list[tuple[tuple[str, ...], object, int]] = []

    def fake_run(
        command: Sequence[str],
        *,
        expected_codes: object,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((tuple(command), expected_codes, timeout))
        return completed

    monkeypatch.setattr(provenance_module, "run_allowed_to_fail", fake_run)

    assert provenance_module._run_command(("git", "--version")) is completed
    assert calls == [(("git", "--version"), None, 30)]


def test_git_timeout_records_safe_unknown_state(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    front_door = tmp_path / "review"
    front_door.symlink_to(checkout, target_is_directory=True)

    def time_out(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout=30, stderr="credential=secret")

    fact = SkillProvenanceProbe(runner=time_out).probe(front_door)

    assert fact.git is not None
    assert fact.git.state is GitProbeState.UNKNOWN
    assert fact.git.diagnostic == "git repository probe failed"
    assert "secret" not in repr(fact)


def test_probe_reuses_one_fetched_snapshot_for_skills_in_the_same_repo(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    first_target = checkout / "skills" / "review"
    second_target = checkout / "skills" / "code"
    first_target.mkdir(parents=True)
    second_target.mkdir()
    first_front_door = tmp_path / ".claude" / "skills" / "review"
    second_front_door = tmp_path / ".codex" / "skills" / "code"
    first_front_door.parent.mkdir(parents=True)
    second_front_door.parent.mkdir(parents=True)
    first_front_door.symlink_to(first_target, target_is_directory=True)
    second_front_door.symlink_to(second_target, target_is_directory=True)
    runner = RecordedRunner(
        _completed(f"{checkout}\n"),
        _completed(),
        _completed(f"{'1' * 40}\n"),
        _completed("main\n"),
        _completed("origin/main\n"),
        _completed(f"{'2' * 40}\n"),
        _completed("0 1\n"),
        _completed(f"{checkout}\n"),
    )
    probe = SkillProvenanceProbe(runner=runner)

    first = probe.probe(first_front_door)
    second = probe.probe(second_front_door)

    assert first.git == second.git
    assert [call for call in runner.calls if call[-2:] == ("fetch", "origin")] == [
        ("git", "-C", str(checkout), "fetch", "origin")
    ]


def test_prober_protocol_method_has_no_silent_default() -> None:
    prober = cast("SkillInstallationProber", object())
    with pytest.raises(NotImplementedError):
        SkillInstallationProber.probe(prober, Path("/skill"))


def test_probe_degrades_every_incomplete_git_state_to_unknown(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    front_door = tmp_path / "review"
    front_door.symlink_to(checkout, target_is_directory=True)
    root = _completed(f"{checkout}\n")
    ok = _completed()
    sha = _completed(f"{'5' * 40}\n")
    branch = _completed("feature\n")
    default = _completed("origin/main\n")
    default_sha = _completed(f"{'6' * 40}\n")
    counts = _completed("0 0\n")
    scripts = (
        (OSError("git absent"),),
        (_completed(),),
        (root, OSError("fetch absent")),
        (root, ok, OSError("head absent"), branch, default),
        (root, ok, sha, OSError("branch absent"), default),
        (root, ok, sha, branch, OSError("default absent")),
        (root, ok, sha, branch, _completed(returncode=1), OSError("refs absent")),
        (root, ok, _completed(returncode=1), branch, default),
        (root, ok, _completed(), branch, default),
        (root, ok, sha, branch, _completed(returncode=1), _completed(returncode=1), _completed(returncode=1)),
        (root, ok, sha, branch, default, OSError("sha absent"), counts),
        (root, ok, sha, branch, default, default_sha, OSError("counts absent")),
        (root, ok, sha, branch, default, _completed(returncode=1), counts),
        (root, ok, sha, branch, default, default_sha, _completed(returncode=1)),
        (root, ok, sha, branch, default, default_sha, _completed("not-counts")),
        (root, ok, sha, branch, default, _completed(), counts),
    )

    for script in scripts:
        fact = SkillProvenanceProbe(runner=ScriptedRunner(*script)).probe(front_door)
        assert fact.git is not None
        assert fact.git.state is GitProbeState.UNKNOWN
        assert fact.git.diagnostic in {
            "git repository probe failed",
            "origin fetch failed",
            "git provenance probe failed",
        }
