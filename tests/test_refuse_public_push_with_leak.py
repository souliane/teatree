"""Integration tests for the public-repo privacy pre-push gate (#685).

The gate refuses ``git push`` when the ``origin`` remote resolves to a
PUBLIC repository and the branch-vs-base diff fails ``t3 tool
privacy-scan`` (a planted secret, an internal path, a banned term).
A clean diff to a public remote, and any push to a private remote, are
allowed through.

These are integration tests in the spirit of the Test-Writing Doctrine:
a real ``git init`` repo under ``tmp_path``, a real second repo acting as
the ``origin`` remote, and a real ``gh`` shim on ``PATH`` that returns a
fixed visibility. Nothing about git or the filesystem is mocked.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "refuse-public-push-with-leak.sh"
SCAN = Path(__file__).resolve().parents[1] / "scripts" / "privacy_scan.py"


def _hermetic_env() -> dict[str, str]:
    """Env with all GIT_* vars stripped so tmp-repo git calls are hermetic."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env=_hermetic_env(),
    )


def _make_repo(path: Path, branch: str = "main") -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-b", branch)
    _git(path, "config", "user.email", "t@e.st")
    _git(path, "config", "user.name", "Tester")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "init")


def _make_gh_shim(bin_dir: Path, visibility: str) -> None:
    """Write a fake ``gh`` that answers ``repo view --json visibility``."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "gh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"repo view"* && "$*" == *"visibility"* ]]; then\n'
        '  if [[ "$*" == *"--jq"* ]]; then\n'
        f'    echo "{visibility}"\n'
        "  else\n"
        f'    echo \'{{"visibility":"{visibility}"}}\'\n'
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _clone_with_remote(tmp_path: Path, gh_visibility: str) -> tuple[Path, dict[str, str]]:
    """Create an origin repo, a working clone, and a gh shim PATH.

    Returns the working clone path and the env (PATH-prefixed with the
    gh shim, GIT_* scrubbed) to run the hook with.
    """
    origin = tmp_path / "origin"
    _make_repo(origin)
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.email", "t@e.st")
    _git(work, "config", "user.name", "Tester")
    # The origin URL is a local path; rewrite it to a github.com-looking
    # URL so the gate has an owner/repo to ask gh about.
    _git(work, "remote", "set-url", "origin", "https://github.com/acme/widget.git")

    bin_dir = tmp_path / "bin"
    _make_gh_shim(bin_dir, gh_visibility)
    env = _hermetic_env()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    # Point the hook at the real privacy_scan script via a known env knob
    # so it does not depend on a globally-installed `t3`.
    env["T3_PRIVACY_SCAN_CMD"] = f"python3 {SCAN}"
    return work, env


def _run_hook(cwd: Path, env: dict[str, str], stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HOOK), "origin", "https://github.com/acme/widget.git"],  # noqa: S607
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _push_stdin(work: Path) -> str:
    """Build the git pre-push stdin line for the current branch HEAD."""
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
        env=_hermetic_env(),
    ).stdout.strip()
    return f"refs/heads/main {sha} refs/heads/main 0000000000000000000000000000000000000000\n"


class TestRefusePublicPushWithLeak:
    def test_blocks_public_push_with_planted_secret(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "leak.txt").write_text(
            "token = glpat-XXXXXXXXXXXXXXXX\n",
            encoding="utf-8",
        )
        _git(work, "add", "leak.txt")
        _git(work, "commit", "-m", "add config")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        combined = (result.stdout + result.stderr).lower()
        assert "privacy" in combined

    def test_blocks_public_push_with_internal_path(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "notes.txt").write_text("see /Users/someone/secret/path\n", encoding="utf-8")
        _git(work, "add", "notes.txt")
        _git(work, "commit", "-m", "add notes")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr

    def test_allows_public_push_with_clean_diff(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "feature.txt").write_text("a clean new feature line\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_allows_private_repo_push_even_with_secret(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PRIVATE")
        (work / "leak.txt").write_text(
            "token = glpat-XXXXXXXXXXXXXXXX\n",
            encoding="utf-8",
        )
        _git(work, "add", "leak.txt")
        _git(work, "commit", "-m", "add config")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_passthrough_when_gh_unavailable(self, tmp_path: Path) -> None:
        """No gh on PATH → visibility unknown → fail open (do not block).

        The gate is a safety net, not the only line of defence; blocking
        every push on a machine without gh would break the workflow.
        """
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        # Keep system bins (bash/git) but drop the gh shim dir so `gh`
        # is genuinely unavailable.
        env["PATH"] = "/usr/bin:/bin"
        (work / "leak.txt").write_text(
            "token = glpat-XXXXXXXXXXXXXXXX\n",
            encoding="utf-8",
        )
        _git(work, "add", "leak.txt")
        _git(work, "commit", "-m", "add config")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_hook_is_executable(self) -> None:
        assert os.access(HOOK, os.X_OK), f"{HOOK} must be chmod +x"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
