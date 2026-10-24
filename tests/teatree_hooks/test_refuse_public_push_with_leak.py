"""Integration tests for the public-repo privacy pre-push gate (#685).

The gate refuses ``git push`` when the ``origin`` remote resolves to a
PUBLIC repository and any commit in the push range — its own patch or
message — fails ``t3 tool privacy-scan`` (a planted secret, an internal
path, a banned term).
A clean diff to a public remote, and any push to a private remote, are
allowed through.

These are integration tests in the spirit of the Test-Writing Doctrine:
a real ``git init`` repo under ``tmp_path``, a real second repo acting as
the ``origin`` remote, and a real ``gh`` shim on ``PATH`` that returns a
fixed visibility. Nothing about git or the filesystem is mocked.
"""

import importlib.machinery
import importlib.util
import json
import marshal
import os
import secrets
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from teatree.hooks._repo_visibility import slug_for_remote_url
from tests._leak_hook_checkout import (
    HOOK,
    REPO_ROOT,
    CommittedFingerprint,
    FingerprintState,
    SourceFixture,
    commit_guarded,
    default_hook,
    place_hook,
)

SCAN = REPO_ROOT / "scripts" / "privacy_scan.py"
#: The scanner refuses to run without a registry, so every hook run is given a placeholder one.
_DEFAULT_REGISTRY = json.dumps({"leak": ["demo-leak-term"], "prose_collider": []})
ZERO_SHA = "0" * 40
#: The registry the hook is placed with, one it is not, and the fingerprint committed for the first.
_REAL_REGISTRY = json.dumps({"leak": ["demo-leak-term", "second-leak-term"], "overlay": ["demo-overlay-term"]})
_FAKE_REGISTRY = json.dumps({"leak": ["fake-picked-term"]})
_COMMITTED_FOR_THE_REAL_ONE = CommittedFingerprint(registry=_REAL_REGISTRY)
# Planted fixture values the gate-under-test must flag; annotated so this
# repo's own pre-push gate does not flag the test source itself.
_PLANTED_SECRET = "token = glpat-XXXXXXXXXXXXXXXX\n"  # privacy-scan:allow fixture
_PLANTED_HOME_PATH = "see /Users/someone/secret/path\n"  # privacy-scan:allow fixture
_SQUASH_COMMITTER = "noreply@github.com"  # privacy-scan:allow fixture
_REAL_EMAIL = "real.dev@internal.example"  # privacy-scan:allow fixture


def _hermetic_env() -> dict[str, str]:
    """Env with all GIT_* vars stripped so tmp-repo git calls are hermetic, and a placeholder registry set."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["TEATREE_TERM_REGISTRY"] = _DEFAULT_REGISTRY
    return env


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env=_hermetic_env(),
    )


# A GitHub-noreply identity so the shared fixtures exercise only the
# leak-scan dimension of the gate, not the #730 author-identity guard
# (which has its own dedicated test class). The login form keeps these
# commits passing the noreply pattern just like a real loop commit.
_NOREPLY_EMAIL = "21343492+souliane@users.noreply.github.com"
_NOREPLY_NAME = "souliane"


_PATH_WITHOUT_GH = f"{Path(sys.executable).parent}{os.pathsep}/usr/bin:/bin"


def _make_repo(path: Path, branch: str = "main") -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-b", branch)
    _git(path, "config", "user.email", _NOREPLY_EMAIL)
    _git(path, "config", "user.name", _NOREPLY_NAME)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "init")


#: The forge URL every hook test pushes to; ``_repoint_origin`` gives the clone an ``ssh`` that serves it.
_FORGE_URL = "git@github.com:acme/widget.git"

_FAKE_SSH = """#!/bin/sh
repo=$(cat "$(dirname "$0")/repo-path") || exit 1
case "$2" in
    git-upload-pack*) exec git upload-pack "$repo" ;;
    git-receive-pack*) exec git receive-pack "$repo" ;;
esac
exit 1
"""


def _repoint_origin(work: Path, origin: Path, url: str) -> None:
    """Point ``origin`` at the forge *url* and serve that URL from the real *origin* repo through a stand-in ``ssh``.

    The hook lists the remote's heads at the URL git pushes to, and git hands a hook the URL it really pushes to, so
    the URL has to answer as it stands, with no ``insteadOf`` between it and the repository.
    """
    serve = work.parent / "fake-forge"
    serve.mkdir(exist_ok=True)
    (serve / "repo-path").write_text(str(origin), encoding="utf-8")
    ssh = serve / "ssh"
    ssh.write_text(_FAKE_SSH, encoding="utf-8")
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    _git(work, "remote", "set-url", "origin", url)
    _git(work, "config", "core.sshCommand", str(ssh))
    _git(work, "config", "ssh.variant", "simple")


def _make_the_remote_unlistable(work: Path) -> None:
    """Make the stand-in ``ssh`` fail, so ``git ls-remote`` fails at once and offline."""
    (work.parent / "fake-forge" / "repo-path").unlink()


def _git_stdout(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [str(shutil.which("git")), *args], cwd=cwd, check=True, capture_output=True, text=True, env=_hermetic_env()
    ).stdout


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
    _git(work, "config", "user.email", _NOREPLY_EMAIL)
    _git(work, "config", "user.name", _NOREPLY_NAME)
    # The origin URL is a local path; rewrite it to a github.com-looking
    # URL so the gate has an owner/repo to ask gh about.
    _repoint_origin(work, origin, _FORGE_URL)

    bin_dir = tmp_path / "bin"
    _make_gh_shim(bin_dir, gh_visibility)
    env = _hermetic_env()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["T3_DATA_DIR"] = str(_isolated_state_dir(tmp_path))
    return work, env


def _isolated_state_dir(tmp_path: Path) -> Path:
    """Per-test root for the day-cached visibility verdict.

    The gate resolves visibility through the shared, slug-keyed
    ``repo-visibility-cache.json``. Every test here uses the same ``acme/widget``
    slug, so without isolation a PUBLIC verdict cached by one test would be served
    to a test that set up a PRIVATE shim — and the test would pass (or fail) for a
    reason that has nothing to do with the gate.
    """
    state = tmp_path / "hook-state"
    state.mkdir(parents=True, exist_ok=True)
    return state


def _run_hook(
    cwd: Path,
    env: dict[str, str],
    stdin: str,
    remote_url: str = _FORGE_URL,
    hook: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the hook the way git does; by default the real one, placed where its fingerprint matches ``env``."""
    placed = hook or default_hook(cwd.parent, env)
    return subprocess.run(
        # `bash <hook>` on PATH is git's real pre-push invocation shape; test-only driver.
        [str(shutil.which("bash")), str(placed), "origin", remote_url],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _push_stdin(work: Path, local_ref: str = "refs/heads/main", remote_ref: str | None = None) -> str:
    """Build the git pre-push stdin line for the current branch HEAD."""
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
        env=_hermetic_env(),
    ).stdout.strip()
    return f"{local_ref} {sha} {remote_ref or local_ref} {ZERO_SHA}\n"


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

    def test_refusal_message_includes_concrete_finding_detail(self, tmp_path: Path) -> None:
        """The gate must show *which* file/line/category tripped it (#696).

        Not just "carries privacy findings" — the scanner's plain-text
        summary (category + redacted match + line) must be in the
        caller-visible refusal output so the user can act without a
        manual rerun.
        """
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "leak.txt").write_text(
            "token = glpat-XXXXXXXXXXXXXXXX\n",
            encoding="utf-8",
        )
        _git(work, "add", "leak.txt")
        _git(work, "commit", "-m", "add config")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        combined = result.stdout + result.stderr
        assert "api_key" in combined, combined
        assert "glpat-" in combined, combined

    def test_blocks_public_push_with_internal_path(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        planted = "see /Users/someone/secret/path\n"
        (work / "notes.txt").write_text(planted, encoding="utf-8")
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

    def test_scans_and_blocks_leak_when_gh_unavailable(self, tmp_path: Path) -> None:
        """No gh on PATH → visibility undetermined → fail CLOSED (scan anyway).

        The gate now skips the scan only when the remote is KNOWN to be
        private/internal. An undetermined remote (no gh) still gets
        scanned, so a real leak cannot ride out to a public remote from a
        gh-less machine (privacy-gate fail-closed hardening, §3f #14).
        """
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        # Keep system bins (bash/git) and the interpreter the hook runs the scanner with,
        # but drop the gh shim dir so `gh` is genuinely unavailable.
        env["PATH"] = _PATH_WITHOUT_GH
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

    def test_allows_clean_push_when_gh_unavailable(self, tmp_path: Path) -> None:
        """Undetermined visibility + a clean diff still passes.

        Fail-closed means "scan anyway", not "block anyway": the scan
        blocks only on a real finding, so a clean push on a gh-less
        machine is unaffected (§3f #14).
        """
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["PATH"] = _PATH_WITHOUT_GH
        (work / "feature.txt").write_text("a clean new feature line\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_non_github_remote_shape_scans_and_blocks_leak(self, tmp_path: Path) -> None:
        """A remote URL with no owner/repo shape is undetermined → scan.

        Previously a non-owner/repo slug exited 0 (fail open). Now it is
        treated as undetermined visibility and the diff is scanned, so a
        leak to an unrecognised remote is still caught (§3f #14).
        """
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "leak.txt").write_text(
            "token = glpat-XXXXXXXXXXXXXXXX\n",
            encoding="utf-8",
        )
        _git(work, "add", "leak.txt")
        _git(work, "commit", "-m", "add config")

        # Drive the hook with a bare, non-owner/repo remote URL.
        result = _run_hook(work, env, _push_stdin(work), remote_url="https://example.invalid/no-slug-here")

        assert result.returncode == 1, result.stdout + result.stderr
        combined = (result.stdout + result.stderr).lower()
        assert "privacy" in combined

    def test_annotated_fixture_does_not_block_but_real_leak_in_same_diff_does(self, tmp_path: Path) -> None:
        """The allow-annotation is line-scoped, not a file-level exclusion.

        An inline-allowed fixture line passes the gate, but a real
        un-annotated secret elsewhere in the same branch still blocks the
        push — proving the gate stays honest.
        """
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        # A scanner-fixture-shaped file: the secret carries the marker.
        fixture = work / "scanner_fixture.py"
        fixture.write_text(
            'SECRET = "glpat-ZZZZZZZZZZZZZZZZ"  # privacy-scan:allow fixture\n',
            encoding="utf-8",
        )
        _git(work, "add", "scanner_fixture.py")
        _git(work, "commit", "-m", "add scanner fixture")

        clean = _run_hook(work, env, _push_stdin(work))
        assert clean.returncode == 0, "annotated fixture must not block: " + clean.stdout + clean.stderr

        # Now a genuinely leaking, un-annotated file in a later commit.
        leaking = 'API = "glpat-ZZZZZZZZZZZZZZZZ"\n'
        (work / "config.py").write_text(leaking, encoding="utf-8")
        _git(work, "add", "config.py")
        _git(work, "commit", "-m", "add config")

        blocked = _run_hook(work, env, _push_stdin(work))
        assert blocked.returncode == 1, "real leak must still block: " + blocked.stdout + blocked.stderr

    def test_gate_passes_a_diff_whose_only_secrets_are_annotated(self, tmp_path: Path) -> None:
        """A diff whose secret-shaped strings are all inline-allowed is clean.

        This is the end-to-end analogue of "scan this branch's own diff":
        the scanner's own fixtures and the hook's doc examples carry the
        marker, so the gate does not self-block.
        """
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        body = (
            'def a(): assert scan("glpat-AAAAAAAAAAAAAAAA")  # privacy-scan:allow fixture\n'
            'def b(): assert scan("see /Users/dev/x")  # privacy-scan:allow fixture\n'
            "#   git@github.com:owner/repo  # privacy-scan:allow doc example\n"
        )
        (work / "test_scanner.py").write_text(body, encoding="utf-8")
        _git(work, "add", "test_scanner.py")
        _git(work, "commit", "-m", "add scanner tests")

        result = _run_hook(work, env, _push_stdin(work))
        assert result.returncode == 0, result.stdout + result.stderr

    def test_blocks_public_push_when_only_the_commit_message_leaks(self, tmp_path: Path) -> None:
        """Commit messages reach public history too (#703).

        The file diff is clean, but the message body carries a
        secret-shaped token. The gate must scan ``git log`` bodies in the
        push range, not only ``git diff`` — a ``Co-authored-by:`` trailer
        with an internal/customer-domain address is the real-world case
        that motivated this.
        """
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "feature.txt").write_text("a perfectly clean feature line\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(
            work,
            "commit",
            "-m",
            "add feature",
            "-m",
            "token = glpat-XXXXXXXXXXXXXXXX",
        )

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        combined = (result.stdout + result.stderr).lower()
        assert "privacy" in combined

    def test_allows_public_push_with_clean_multiline_commit_message(self, tmp_path: Path) -> None:
        """Message scanning must not become a blanket block on trailers.

        A clean diff plus a clean multi-paragraph message (including a
        benign example-domain ``Co-authored-by`` trailer) still passes.
        """
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "feature.txt").write_text("another clean feature line\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(
            work,
            "commit",
            "-m",
            "add feature",
            "-m",
            "Longer body explaining the change in plain prose.\n\nCo-authored-by: A Dev <dev@example.com>",
        )

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_blocks_public_push_when_commit_message_carries_banned_term(self, tmp_path: Path) -> None:
        """A banned term in a commit MESSAGE is caught before it reaches a public remote (#703/#1415).

        This is the backstop the commit-time downgrade relies on: the
        commit-time gate now downgrades a banned term on a LOCAL commit to a
        warn (Case A), so the pre-push gate MUST re-scan the commit MESSAGE
        (not only the diff) for banned terms on a push to a public remote.
        The file diff is clean — the term lives only in the message body — so
        this proves the message-scan dimension, not the diff scan.
        """
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["TEATREE_TERM_REGISTRY"] = '{"leak":[],"prose_collider":["democorp"]}'
        (work / "feature.txt").write_text("a perfectly clean feature line\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "feat: onboard democorp customer")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        combined = (result.stdout + result.stderr).lower()
        assert "banned_term" in combined
        assert "privacy" in combined

    def test_allows_public_push_with_clean_message_under_banned_terms_config(self, tmp_path: Path) -> None:
        """Anti-vacuity for the message-banned-term block.

        Same public remote and the SAME ``TEATREE_TERM_REGISTRY`` config, but a clean
        message with no configured term still passes — proving the block above
        measures the term, not the mere presence of the banned-terms config.
        """
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["TEATREE_TERM_REGISTRY"] = '{"leak":[],"prose_collider":["democorp"]}'
        (work / "feature.txt").write_text("a perfectly clean feature line\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "feat: onboard the new customer pipeline")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_hook_is_executable(self) -> None:
        assert os.access(HOOK, os.X_OK), f"{HOOK} must be chmod +x"


def _relocated_checkout(tmp_path: Path, *, scanner: str | None) -> Path:
    """A scratch checkout holding the real hook, the real ``src`` and a scanner this test authors.

    The hook runs the scanner from the tree it lives in, so replacing
    ``scripts/privacy_scan.py`` here is the only way to give it another scanner.
    """
    return place_hook(tmp_path / "scratch-checkout", registry=_DEFAULT_REGISTRY, sources=SourceFixture(scanner=scanner))


#: A stand-in scanner agrees the registry is the committed one: these tests are about what the hook does next.
_VERIFIES = "import sys\nif '--verify-registry' in sys.argv:\n    sys.exit(0)\n"

#: Which of the hook's scans a stand-in scanner crashes on; it is clean on the others.
_CRASH_WHEN = {
    "always": "True",
    "ref": "data.strip() == 'main'",
    "message": "'add feature' in data and 'diff --git' not in data",
    "patch": "'diff --git' in data",
}


def _crashing_scanner(exit_code: int, *, when: str = "always") -> str:
    """A scanner that exits ``exit_code`` (never the findings code) on the scan named by ``when``."""
    return (
        _VERIFIES + "data = sys.stdin.read()\n"
        f"if {_CRASH_WHEN[when]}:\n"
        "    sys.stderr.write('scanner blew up\\n')\n"
        f"    sys.exit({exit_code})\n"
        "print('Privacy scan: clean (0 findings)')\n"
    )


_FINDING_SCANNER = _VERIFIES + "sys.stdin.read()\nprint('line 1: banned_term: leak#0')\nsys.exit(3)\n"
_CLEAN_SCANNER = _VERIFIES + "sys.stdin.read()\nprint('Privacy scan: clean (0 findings)')\n"


def _hook_failing_the_reads_of(tmp_path: Path, form: list[str]) -> Path:
    hook = place_hook(tmp_path / "scratch-checkout", registry=_DEFAULT_REGISTRY)
    reader = hook.parents[2] / "src" / "teatree" / "hooks" / "commit_objects.py"
    committed = reader.read_text(encoding="utf-8")
    failing = committed.replace(
        '\nif __name__ == "__main__":',
        f"\n_git = lambda *a, _read=_git: None if sys.argv[1:{len(form) + 1}] == {form!r} else _read(*a)\n"
        '\nif __name__ == "__main__":',
    )
    assert failing != committed, "commit_objects.py no longer has the anchor the plant is inserted before"
    reader.write_text(failing, encoding="utf-8")
    _git(hook.parents[2], "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qam", "one read")
    return hook


class TestLeakGateFailsClosedOnScannerCrash:
    """A scan that could not run is not a clean scan: the push is refused (hooks/CLAUDE.md: leak gates fail closed).

    The dedicated findings exit code still marks a genuine finding and names the commit and
    file; every other non-zero exit (a crash, a usage error, an unset registry) refuses with
    its own wording, so nobody mistakes an outage for a leak.
    """

    def _clean_feature_commit(self, tmp_path: Path) -> tuple[Path, dict[str, str]]:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "feature.txt").write_text("a clean feature line\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")
        return work, env

    def _annotated_tag_push(self, tmp_path: Path) -> tuple[Path, dict[str, str], str]:
        work, env = self._clean_feature_commit(tmp_path)
        _git(work, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "tag", "-a", "v1", "-m", "release v1")
        return work, env, f"refs/tags/v1 {_rev(work, 'refs/tags/v1')} refs/tags/v1 {ZERO_SHA}\n"

    @pytest.mark.parametrize(
        ("when", "exit_code"),
        [("always", 1), ("patch", 1), ("patch", 2), ("patch", 4), ("message", 1), ("message", 4)],
        ids=str,
    )
    def test_scanner_crash_fails_closed(self, tmp_path: Path, when: str, exit_code: int) -> None:
        work, env = self._clean_feature_commit(tmp_path)
        hook = _relocated_checkout(tmp_path, scanner=_crashing_scanner(exit_code, when=when))

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "a scanner crash must refuse the push: " + result.stdout + result.stderr
        assert "fail closed" in result.stdout.lower(), result.stdout
        assert f"exit {exit_code}" in result.stdout, result.stdout

    def test_scanner_usage_error_fails_closed(self, tmp_path: Path) -> None:
        work, env = self._clean_feature_commit(tmp_path)
        hook = _relocated_checkout(tmp_path, scanner=_VERIFIES + "sys.exit(2)\n")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "could not run" in result.stdout, result.stdout

    @pytest.mark.parametrize("form", [["messages"], ["commit", "raw"], ["commit", "blank"]], ids="-".join)
    def test_a_commit_object_read_that_fails_alone_refuses_the_push(self, tmp_path: Path, form: list[str]) -> None:
        work, env = self._clean_feature_commit(tmp_path)
        hook = _hook_failing_the_reads_of(tmp_path, form)

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "a read that did not happen passed the push: " + result.stdout + result.stderr
        assert "could not run (exit 1)" in result.stdout, result.stdout
        assert result.stdout.count("commit_objects: could not read") == 1, result.stdout

    @pytest.mark.parametrize("form", [["tag", "raw"], ["tag", "blank"]], ids="-".join)
    def test_an_annotated_tag_read_that_fails_alone_refuses_the_push(self, tmp_path: Path, form: list[str]) -> None:
        work, env, stdin = self._annotated_tag_push(tmp_path)

        result = _run_hook(work, env, stdin, hook=_hook_failing_the_reads_of(tmp_path, form))

        assert result.returncode == 1, "a failed tag read passed the push: " + result.stdout + result.stderr
        assert "the annotated-tag privacy scan could not run (exit 1)" in result.stdout, result.stdout
        assert result.stdout.count("commit_objects: could not read") == 1, result.stdout

    def test_the_same_annotated_tag_push_passes_when_every_read_works(self, tmp_path: Path) -> None:
        work, env, stdin = self._annotated_tag_push(tmp_path)

        result = _run_hook(work, env, stdin, hook=place_hook(tmp_path / "scratch-checkout", registry=_DEFAULT_REGISTRY))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_unresolvable_scanner_refuses(self, tmp_path: Path) -> None:
        work, env = self._clean_feature_commit(tmp_path)
        hook = _relocated_checkout(tmp_path, scanner=None)

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "privacy_scan.py is missing" in result.stdout, result.stdout

    def test_a_host_without_python_3_13_refuses_and_names_the_remedy(self, tmp_path: Path) -> None:
        work, env = self._clean_feature_commit(tmp_path)
        bin_dir = tmp_path / "old-python-bin"
        _make_unusable_bare_pythons(bin_dir)
        for tool in (
            *("bash", "git", "gh", "sed", "tr", "grep", "sort", "cat", "mktemp", "rm", "dirname"),
            *("env", "awk", "paste", "uniq", "head", "wc", "find", "mkdir"),
        ):
            found = shutil.which(tool, path=env["PATH"])
            assert found is not None
            (bin_dir / tool).symlink_to(found)
        env["PATH"] = str(bin_dir)

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        assert "No Python 3.13+ interpreter" in result.stdout, result.stdout

    def test_genuine_finding_still_blocks(self, tmp_path: Path) -> None:
        """The real scanner reports a finding on the dedicated code, and the gate blocks on it."""
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "leak.txt").write_text(
            "token = glpat-XXXXXXXXXXXXXXXX\n",
            encoding="utf-8",
        )
        _git(work, "add", "leak.txt")
        _git(work, "commit", "-m", "add config")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, "genuine finding must still block: " + result.stdout + result.stderr
        assert "carries privacy findings" in result.stdout, result.stdout

    def test_an_unset_registry_refuses_instead_of_leaving_the_term_check_inert(self, tmp_path: Path) -> None:
        work, env = self._clean_feature_commit(tmp_path)
        env.pop("TEATREE_TERM_REGISTRY")
        env["T3_CONFIG_DB"] = str(_seed_private_repos(tmp_path / "config.sqlite3", []))

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        assert "banned_term_registry" in result.stdout, result.stdout

    def test_the_scanner_runs_from_the_checkout_tree(self, tmp_path: Path) -> None:
        work, env = self._clean_feature_commit(tmp_path)
        bin_dir = tmp_path / "fake-installed-bin"
        bin_dir.mkdir()
        called = bin_dir / "t3-was-called"
        (bin_dir / "t3").write_text(f"#!/bin/sh\ntouch {called}\nexit 0\n", encoding="utf-8")
        (bin_dir / "t3").chmod(0o755)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

        flagged = _run_hook(work, env, _push_stdin(work), hook=_relocated_checkout(tmp_path, scanner=_FINDING_SCANNER))

        assert flagged.returncode == 1, "an installed t3 that always passes must change nothing: " + flagged.stdout
        assert "carries privacy findings" in flagged.stdout, flagged.stdout
        assert not called.exists(), "the hook called an installed t3 instead of the checkout's scanner"

    def test_a_clean_scanner_in_the_checkout_lets_the_same_push_through(self, tmp_path: Path) -> None:
        work, env = self._clean_feature_commit(tmp_path)

        passed = _run_hook(work, env, _push_stdin(work), hook=_relocated_checkout(tmp_path, scanner=_CLEAN_SCANNER))

        assert passed.returncode == 0, passed.stdout + passed.stderr


class TestRefusePublicPushWithNonNoreplyAuthor:
    """#730 — public history must never carry a real author/committer email.

    On a PUBLIC remote every commit in the push range must have an author
    AND committer email matching the GitHub noreply pattern; anything else
    (e.g. a customer-domain address) blocks. Private remotes are exempt
    (real internal emails there are fine).
    """

    def _commit_as(self, work: Path, name: str, email: str, filename: str) -> None:
        (work / filename).write_text("clean feature line\n", encoding="utf-8")
        _git(work, "add", filename)
        _git(
            work,
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={email}",
            "commit",
            "-m",
            "add clean feature",
        )

    def test_blocks_public_push_with_customer_domain_author(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        self._commit_as(
            work,
            "Real Dev",
            "real.dev@internal.example",
            "feature.txt",
        )

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode != 0, result.stdout + result.stderr
        assert "noreply" in (result.stdout + result.stderr)

    def test_blocks_public_push_when_only_committer_is_real_email(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "feature.txt").write_text("clean feature line\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        # Author is a valid noreply; committer is a real customer email.
        env_commit = _hermetic_env()
        env_commit["GIT_AUTHOR_NAME"] = "souliane"
        env_commit["GIT_AUTHOR_EMAIL"] = "21343492+souliane@users.noreply.github.com"
        env_commit["GIT_COMMITTER_NAME"] = "Real Dev"
        env_commit["GIT_COMMITTER_EMAIL"] = "real.dev@internal.example"
        subprocess.run(
            ["git", "commit", "-m", "add clean feature"],  # noqa: S607
            cwd=work,
            check=True,
            capture_output=True,
            env=env_commit,
        )

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode != 0, result.stdout + result.stderr

    def test_allows_public_push_with_souliane_noreply_author(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        self._commit_as(
            work,
            "souliane",
            "21343492+souliane@users.noreply.github.com",
            "feature.txt",
        )

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_allows_public_push_with_other_github_noreply_author(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        self._commit_as(
            work,
            "Octo Cat",
            "987654321+octocat@users.noreply.github.com",
            "feature.txt",
        )

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_allows_private_repo_push_with_real_email_author(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PRIVATE")
        self._commit_as(
            work,
            "Real Dev",
            "real.dev@internal.example",
            "feature.txt",
        )

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr


def _rev(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=_hermetic_env(),
    ).stdout.strip()


def _commit_file(
    repo: Path,
    filename: str,
    content: str,
    msg: str,
    *,
    committer_email: str | None = None,
) -> str:
    """Commit ``content`` to ``repo``; optionally forge the committer email.

    A forged committer email (e.g. GitHub's web-UI squash identity
    ``noreply@github.com``) models an already-public commit whose metadata
    the #730 identity guard flags — the exact shape that false-positives
    when a stale range re-includes it (#3414). Only the email matters to the
    gate (it scans ``%ae``/``%ce``), so the committer name is left fixed.
    """
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    if committer_email is not None:
        env = _hermetic_env()
        env["GIT_AUTHOR_NAME"] = _NOREPLY_NAME
        env["GIT_AUTHOR_EMAIL"] = _NOREPLY_EMAIL
        env["GIT_COMMITTER_NAME"] = "Forged Identity"
        env["GIT_COMMITTER_EMAIL"] = committer_email
        subprocess.run(
            ["git", "commit", "-m", msg],  # noqa: S607
            cwd=repo,
            check=True,
            capture_output=True,
            env=env,
        )
    else:
        _git(repo, "commit", "-m", msg)
    return _rev(repo, "HEAD")


def _public_work_clone(tmp_path: Path, origin: Path) -> tuple[Path, dict[str, str]]:
    """Clone an already-populated ``origin`` into a PUBLIC-remote work tree.

    Unlike ``_clone_with_remote`` (which builds its own single-commit
    origin), this takes a caller-populated origin so the test controls the
    already-public history the stale-range false positive depends on.
    """
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.email", _NOREPLY_EMAIL)
    _git(work, "config", "user.name", _NOREPLY_NAME)
    _repoint_origin(work, origin, _FORGE_URL)
    bin_dir = tmp_path / "bin"
    _make_gh_shim(bin_dir, "PUBLIC")
    env = _hermetic_env()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["T3_DATA_DIR"] = str(_isolated_state_dir(tmp_path))
    return work, env


_REMOTE_URL = _FORGE_URL


def _run_hook_synth(
    work: Path,
    env: dict[str, str],
    *,
    to_ref: str,
    from_ref: str,
    remote_branch: str = "refs/heads/feature",
) -> subprocess.CompletedProcess[str]:
    """Drive the hook through the prek synthesized-from-env path (empty stdin).

    prek consumes pre-push stdin and hands the range to the hook via
    ``PRE_COMMIT_*`` env vars, so the hook reads EMPTY stdin and synthesizes
    the ref-update line. ``from_ref`` is what prek passes as ``remote_sha`` —
    which git reports as a STALE ancestor for the first push of a branch that
    merged main (#3414). ``remote_branch`` doubles as the local branch (the
    push maps the branch onto its own name on the remote).
    """
    synth = dict(env)
    synth["PRE_COMMIT_REMOTE_NAME"] = "origin"
    synth["PRE_COMMIT_TO_REF"] = to_ref
    synth["PRE_COMMIT_FROM_REF"] = from_ref
    synth["PRE_COMMIT_LOCAL_BRANCH"] = remote_branch
    synth["PRE_COMMIT_REMOTE_BRANCH"] = remote_branch
    return subprocess.run(
        [str(shutil.which("bash")), str(default_hook(work.parent, synth)), "origin", _REMOTE_URL],
        cwd=work,
        input="",
        capture_output=True,
        text=True,
        check=False,
        env=synth,
    )


class TestLeakGateRecomputesBaseFromRemoteTrackingRef:
    """#3414 — the synthesized-from-env range must not trust a stale ``PRE_COMMIT_FROM_REF``.

    prek reports ``PRE_COMMIT_FROM_REF`` as the push's ``remote_sha``. For
    the first push of a long-lived branch that merged ``main`` since it was
    created, git reports that value as a weeks-old ``main`` ancestor — NOT
    all-zeros and NOT the current ``origin/main`` tip. Trusting it as the
    scan base re-includes dozens of already-public, already-merged commits,
    so the identity guard and the content scan false-positive on immutable
    public history. The fix recomputes the base from the real
    remote-tracking ref (merge-base with the remote default branch) unless
    the reported sha is confirmed to be the branch's actual remote tip.
    """

    def _origin_with_public_identity_commit(self, tmp_path: Path) -> tuple[Path, str, str]:
        """origin/main = C0 (clean) → C1 (GitHub web-UI squash committer).

        Returns (origin, c0, c1). C1's committer email ``noreply@github.com``
        is NOT the ``<id>+<login>@users.noreply.github.com`` shape the #730
        guard accepts, so any range that re-includes C1 trips the identity
        block — the classic already-public false positive.
        """
        origin = tmp_path / "origin"
        _make_repo(origin)
        c0 = _rev(origin, "HEAD")
        c1 = _commit_file(
            origin,
            "merged_from_main.txt",
            "a line merged via the GitHub web UI\n",
            "prior PR squash-merged on main",
            committer_email="noreply@github.com",
        )
        return origin, c0, c1

    def test_stale_from_ref_no_longer_false_positives_on_public_identity(self, tmp_path: Path) -> None:
        """RED before fix: a stale range re-includes an already-public non-noreply commit.

        The branch's OWN new commit is clean (noreply identity, clean
        content), but ``PRE_COMMIT_FROM_REF`` points at C0 — before the
        already-public ``noreply@github.com`` squash commit C1. The current
        hook trusts C0 as the base, so ``C0..HEAD`` re-includes C1 and the
        identity guard blocks a legitimate first push. The fix must scan only
        the true new-content range (``origin/main..HEAD``), which excludes C1.
        """
        origin, c0, _c1 = self._origin_with_public_identity_commit(tmp_path)
        work, env = _public_work_clone(tmp_path, origin)
        _git(work, "checkout", "-b", "feature")
        head = _commit_file(work, "feature.txt", "a clean new feature line\n", "add feature")

        result = _run_hook_synth(work, env, to_ref=head, from_ref=c0)

        assert result.returncode == 0, (
            "stale FROM_REF must not re-flag already-public identity: " + result.stdout + result.stderr
        )

    def test_stale_from_ref_no_longer_false_positives_on_public_content(self, tmp_path: Path) -> None:
        """RED before fix: a stale range re-includes already-public flagged CONTENT.

        C1 (already public on main) legitimately contains an internal-looking
        path. The branch's own new commit is clean. A stale ``C0`` base scans
        ``C0..HEAD``, re-flagging C1's content; the true ``origin/main..HEAD``
        range is clean.
        """
        origin = tmp_path / "origin"
        _make_repo(origin)
        c0 = _rev(origin, "HEAD")
        _commit_file(
            origin,
            "prior.txt",
            "see /Users/someone/secret/path\n",
            "prior PR merged on main",
        )
        work, env = _public_work_clone(tmp_path, origin)
        _git(work, "checkout", "-b", "feature")
        head = _commit_file(work, "feature.txt", "a clean new feature line\n", "add feature")

        result = _run_hook_synth(work, env, to_ref=head, from_ref=c0)

        assert result.returncode == 0, (
            "stale FROM_REF must not re-flag already-public content: " + result.stdout + result.stderr
        )

    def test_real_leak_in_new_commit_still_blocks_under_stale_from_ref(self, tmp_path: Path) -> None:
        """Anti-vacuity: narrowing the base must NOT let a real new leak through.

        Same stale ``C0`` base, but the branch's own new commit plants a real
        secret. The true ``origin/main..HEAD`` range still contains it, so the
        gate must block both before AND after the fix — the fix narrows the
        scan, it does not weaken it.
        """
        origin, c0, _c1 = self._origin_with_public_identity_commit(tmp_path)
        work, env = _public_work_clone(tmp_path, origin)
        _git(work, "checkout", "-b", "feature")
        head = _commit_file(work, "leak.txt", "token = glpat-XXXXXXXXXXXXXXXX\n", "add config")

        result = _run_hook_synth(work, env, to_ref=head, from_ref=c0)

        assert result.returncode == 1, "a real new-commit leak must still block: " + result.stdout + result.stderr
        assert "privacy" in (result.stdout + result.stderr).lower()

    def test_real_identity_leak_in_new_commit_still_blocks_under_stale_from_ref(self, tmp_path: Path) -> None:
        """Anti-vacuity for the identity guard: a real non-noreply NEW commit still blocks.

        The stale base excludes the already-public C1, but the branch's own
        new commit carries a real customer-domain committer email. That is a
        genuine PII leak in new history and must block after the fix.
        """
        origin, c0, _c1 = self._origin_with_public_identity_commit(tmp_path)
        work, env = _public_work_clone(tmp_path, origin)
        _git(work, "checkout", "-b", "feature")
        head = _commit_file(
            work,
            "feature.txt",
            "a clean new feature line\n",
            "add feature",
            committer_email="real.dev@internal.example",
        )

        result = _run_hook_synth(work, env, to_ref=head, from_ref=c0)

        assert result.returncode == 1, (
            "a real new-commit identity leak must still block: " + result.stdout + result.stderr
        )
        assert "noreply" in (result.stdout + result.stderr)

    def test_trusts_real_remote_branch_tip_and_still_blocks_update_leak(self, tmp_path: Path) -> None:
        """When the branch already exists on the remote, the real tip IS the base — and leaks still block.

        Here ``PRE_COMMIT_FROM_REF`` is the genuine current tip of
        ``refs/remotes/origin/feature`` (a branch-update push, not a first
        push), so the fix trusts it as the base. A leak in the update range
        must still block — proving the trust path is preserved, not widened
        away.
        """
        origin = tmp_path / "origin"
        _make_repo(origin)
        _git(origin, "checkout", "-b", "feature")
        tip = _commit_file(origin, "feature.txt", "existing feature line\n", "seed feature branch")
        _git(origin, "checkout", "main")
        work, env = _public_work_clone(tmp_path, origin)
        _git(work, "checkout", "feature")
        head = _commit_file(work, "leak.txt", "token = glpat-XXXXXXXXXXXXXXXX\n", "update feature")

        result = _run_hook_synth(work, env, to_ref=head, from_ref=tip)

        assert result.returncode == 1, (
            "a leak in a branch-update range must still block: " + result.stdout + result.stderr
        )
        assert "privacy" in (result.stdout + result.stderr).lower()

    def test_clean_update_from_real_remote_tip_passes(self, tmp_path: Path) -> None:
        """Anti-vacuity companion: a clean branch-update from the real tip passes."""
        origin = tmp_path / "origin"
        _make_repo(origin)
        _git(origin, "checkout", "-b", "feature")
        tip = _commit_file(origin, "feature.txt", "existing feature line\n", "seed feature branch")
        _git(origin, "checkout", "main")
        work, env = _public_work_clone(tmp_path, origin)
        _git(work, "checkout", "feature")
        head = _commit_file(work, "more.txt", "another clean feature line\n", "extend feature")

        result = _run_hook_synth(work, env, to_ref=head, from_ref=tip)

        assert result.returncode == 0, result.stdout + result.stderr


class TestLeakGateScansOnlyNewlyPublicCommits:
    """A merge-forward push must not re-judge ``main``'s own history (#3523).

    When a feature branch merges ``origin/main`` into itself, the linear range
    ``remote_sha..HEAD`` spans every commit the merge brought in — all of it
    already public. The gate must scan the commits reachable from HEAD but NOT
    from any already-public ref (``origin/main`` AND the branch's own remote
    tip), so main's findings and main's committer identities are never
    re-reported against the branch.
    """

    _FEATURE_STDIN = "refs/heads/feature {head} refs/heads/feature {tip}\n"

    def _merge_forward(
        self,
        tmp_path: Path,
        *,
        main_content: str = _PLANTED_HOME_PATH,
        main_committer: str = _SQUASH_COMMITTER,
    ) -> tuple[Path, dict[str, str], str, str]:
        """Feature branch pushed at F1, then merged with an advanced ``main``.

        ``main`` carries both a content finding and a non-noreply (GitHub
        squash-merge) committer identity — the two false-positive classes the
        stale linear range produced on PR #3523.
        """
        origin = tmp_path / "origin"
        _make_repo(origin)
        _git(origin, "checkout", "-b", "feature")
        feature_tip = _commit_file(origin, "feature.txt", "existing feature line\n", "seed feature branch")
        _git(origin, "checkout", "main")
        _commit_file(origin, "prior.txt", main_content, "prior PR merged on main")
        _commit_file(
            origin,
            "squashed.txt",
            "a line squash-merged via the GitHub web UI\n",
            "prior PR squash-merged on main",
            committer_email=main_committer,
        )

        work, env = _public_work_clone(tmp_path, origin)
        _git(work, "checkout", "feature")
        _git(work, "merge", "origin/main", "-m", "merge origin/main into feature")
        return work, env, _rev(work, "HEAD"), feature_tip

    def test_merge_forward_with_findings_only_on_main_is_allowed(self, tmp_path: Path) -> None:
        work, env, head, tip = self._merge_forward(tmp_path)

        result = _run_hook(work, env, self._FEATURE_STDIN.format(head=head, tip=tip))

        assert result.returncode == 0, (
            "already-public main content must not be re-scanned: " + result.stdout + result.stderr
        )

    def test_merge_forward_does_not_re_judge_main_commit_identities(self, tmp_path: Path) -> None:
        work, env, head, tip = self._merge_forward(tmp_path, main_content="a clean prior line\n")

        result = _run_hook(work, env, self._FEATURE_STDIN.format(head=head, tip=tip))

        assert result.returncode == 0, (
            "already-public main identities must not be re-judged: " + result.stdout + result.stderr
        )
        assert _SQUASH_COMMITTER not in result.stdout

    def test_branch_finding_on_top_of_merge_forward_still_blocks(self, tmp_path: Path) -> None:
        work, env, _head, tip = self._merge_forward(tmp_path)
        head = _commit_file(work, "leak.txt", _PLANTED_SECRET, "add config")

        result = _run_hook(work, env, self._FEATURE_STDIN.format(head=head, tip=tip))

        assert result.returncode == 1, "a genuinely new leak must still block: " + result.stdout + result.stderr
        assert "privacy" in (result.stdout + result.stderr).lower()

    def test_branch_identity_on_top_of_merge_forward_still_blocks(self, tmp_path: Path) -> None:
        work, env, _head, tip = self._merge_forward(tmp_path, main_content="a clean prior line\n")
        head = _commit_file(
            work,
            "extra.txt",
            "another clean feature line\n",
            "extend feature",
            committer_email=_REAL_EMAIL,
        )

        result = _run_hook(work, env, self._FEATURE_STDIN.format(head=head, tip=tip))

        assert result.returncode == 1, "a new non-noreply identity must block: " + result.stdout + result.stderr
        assert _REAL_EMAIL in result.stdout

    def test_amended_history_is_still_scanned(self, tmp_path: Path) -> None:
        """A force-push whose ``remote_sha`` is no longer an ancestor of HEAD.

        Excluding the old remote tip must not exclude the rewritten commit —
        it is unreachable from every public ref, so it is genuinely new.
        """
        origin = tmp_path / "origin"
        _make_repo(origin)
        _git(origin, "checkout", "-b", "feature")
        tip = _commit_file(origin, "feature.txt", "existing feature line\n", "seed feature branch")
        _git(origin, "checkout", "main")
        work, env = _public_work_clone(tmp_path, origin)
        _git(work, "checkout", "feature")
        _git(work, "reset", "--hard", "HEAD~1")
        head = _commit_file(work, "leak.txt", _PLANTED_SECRET, "rewritten feature")

        result = _run_hook(work, env, self._FEATURE_STDIN.format(head=head, tip=tip))

        assert result.returncode == 1, "amended history must still be scanned: " + result.stdout + result.stderr
        assert "privacy" in (result.stdout + result.stderr).lower()

    def test_undeterminable_public_set_fails_closed(self, tmp_path: Path) -> None:
        """A remote that cannot be listed → scan wider, never narrower.

        With no public ref to subtract, the whole history reachable from the
        pushed sha is scanned, so a finding that predates the branch still
        blocks rather than riding out on an unknown base.
        """
        origin = tmp_path / "origin"
        _make_repo(origin)
        _commit_file(origin, "prior.txt", _PLANTED_HOME_PATH, "prior PR merged on main")
        work, env = _public_work_clone(tmp_path, origin)
        _git(work, "checkout", "-b", "feature")
        head = _commit_file(work, "feature.txt", "a clean new feature line\n", "add feature")
        _make_the_remote_unlistable(work)

        result = _run_hook(work, env, self._FEATURE_STDIN.format(head=head, tip=ZERO_SHA))

        assert result.returncode == 1, "an undeterminable public set must fail closed: " + result.stdout + result.stderr
        assert "home_path" in result.stdout, result.stdout


def _make_erroring_gh_shim(bin_dir: Path) -> None:
    """Write a ``gh`` that is PRESENT on PATH but whose ``repo view`` FAILS.

    Distinct from "no gh at all": the tool resolves, but the visibility API call
    ERRORS (a network/auth failure). The bash gate then sees an empty verdict and
    must fail CLOSED (scan anyway) -- the exact analogue of the Python leak gate's
    ``slug_visibility -> None`` probe-error path (#3442).
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "gh"
    shim.write_text(
        '#!/usr/bin/env bash\necho "gh: could not reach api.github.com (probe error)" >&2\nexit 1\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestProbeErrorVisibilityFailsClosed:
    """A visibility-PROBE ERROR fails CLOSED on the bash pre-push gate (#3442).

    Distinct from the gh-ABSENT rows above: here ``gh`` is PRESENT but its
    ``repo view`` errors (a network/auth failure), so the gate sees an empty
    verdict and scans anyway (the ``*)`` undetermined branch). This is the bash
    half of the #3442 reconciliation; the Python PreToolUse leak gate is held to
    the SAME polarity (a resolvable slug whose probe cannot be confirmed is
    scanned, never skipped) by
    ``tests/teatree_hooks/test_public_visibility.py::TestProbeErrorFailsClosed``.
    """

    def test_bash_gate_scans_and_blocks_leak_when_probe_errors(self, tmp_path: Path) -> None:
        # gh PRESENT but `repo view` ERRORS -> visibility undetermined -> fail
        # CLOSED (scan) -> the planted secret blocks. The loud "fail closed"
        # warning shows the undetermined path was taken (not a private skip).
        work, _env = _clone_with_remote(tmp_path, "PUBLIC")
        bin_dir = tmp_path / "errbin"
        _make_erroring_gh_shim(bin_dir)
        env = _hermetic_env()
        env["PATH"] = f"{bin_dir}{os.pathsep}/usr/bin:/bin"
        env["T3_DATA_DIR"] = str(_isolated_state_dir(tmp_path))
        # privacy-scan:allow fixture -- a planted FAKE secret the gate-under-test must
        # scan; annotated so this repo's own pre-push gate does not flag the diff line.
        (work / "leak.txt").write_text("token = glpat-XXXXXXXXXXXXXXXX\n", encoding="utf-8")  # privacy-scan:allow
        _git(work, "add", "leak.txt")
        _git(work, "commit", "-m", "add config")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        combined = (result.stdout + result.stderr).lower()
        assert "privacy" in combined
        assert "fail closed" in combined


_GITLAB_REMOTE_URL = "git@gitlab.com:acme-eng/inner/widget.git"


def _make_glab_shim(bin_dir: Path, visibility: str) -> None:
    """Write a fake ``glab`` answering ``api projects/<url-encoded-path>``."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "glab"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"api"* && "$*" == *"projects/"* ]]; then\n'
        f'  echo \'{{"visibility":"{visibility}"}}\'\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestLeakGateResolvesVisibilityOnTheRemotesOwnForge:
    """A GitLab remote must be classifiable — asking ``gh`` about it never was.

    The gate shelled ``gh repo view`` for EVERY remote. On a ``gitlab.com``
    remote ``gh`` errors on the namespace, so visibility was permanently
    undetermined and the gate fell into its fail-closed branch: it scanned a
    PRIVATE repo on every push, forever. Visibility now routes by the remote's
    host (``glab`` for GitLab, ``gh`` for GitHub).

    The PUBLIC row is the anti-vacuity guard: it proves the private row skips
    because the repo is provably private, not because the gate stopped
    detecting the planted secret on a GitLab remote.
    """

    def _gitlab_clone(self, tmp_path: Path, visibility: str) -> tuple[Path, dict[str, str]]:
        origin = tmp_path / "origin"
        _make_repo(origin)
        work = tmp_path / "work"
        _git(tmp_path, "clone", str(origin), str(work))
        _git(work, "config", "user.email", _NOREPLY_EMAIL)
        _git(work, "config", "user.name", _NOREPLY_NAME)
        _git(work, "remote", "set-url", "origin", _GITLAB_REMOTE_URL)
        bin_dir = tmp_path / "bin"
        _make_glab_shim(bin_dir, visibility)
        env = _hermetic_env()
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["T3_DATA_DIR"] = str(_isolated_state_dir(tmp_path))
        (work / "leak.txt").write_text(_PLANTED_SECRET, encoding="utf-8")
        _git(work, "add", "leak.txt")
        _git(work, "commit", "-m", "add config")
        return work, env

    def test_private_gitlab_remote_skips_the_scan(self, tmp_path: Path) -> None:
        work, env = self._gitlab_clone(tmp_path, "private")

        result = _run_hook(work, env, _push_stdin(work), remote_url=_GITLAB_REMOTE_URL)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "fail closed" not in (result.stdout + result.stderr).lower()

    def test_public_gitlab_remote_still_blocks_the_same_secret(self, tmp_path: Path) -> None:
        work, env = self._gitlab_clone(tmp_path, "public")

        result = _run_hook(work, env, _push_stdin(work), remote_url=_GITLAB_REMOTE_URL)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "privacy" in (result.stdout + result.stderr).lower()


_PROBE_FAILURE_MARKER = "probe-exploded-marker-7f3a"
_PROBE_REMOTE_URL = _FORGE_URL


def _fake_core_package(src_root: Path, *, verdict: str = "", fail_marker: str = "") -> None:
    """Plant a minimal ``teatree.hooks.repo_visibility_cli`` under ``src_root``.

    It either prints a verdict (a probe that resolves) or writes ``fail_marker``
    to stderr and exits non-zero (a probe that breaks). Being first on
    ``PYTHONPATH`` it shadows any real teatree the interpreter may have
    installed, so the outcome is the test's, not the runner's environment's.
    """
    hooks = src_root / "teatree" / "hooks"
    hooks.mkdir(parents=True)
    (src_root / "teatree" / "__init__.py").write_text("", encoding="utf-8")
    (hooks / "__init__.py").write_text("", encoding="utf-8")
    body = (
        f"import sys\nsys.stderr.write({fail_marker!r} + '\\n')\nraise SystemExit(1)\n"
        if fail_marker
        else f"import sys\nprint({verdict!r} if '--live' in sys.argv else 'UNKNOWN')\n"
    )
    (hooks / "repo_visibility_cli.py").write_text(body, encoding="utf-8")


def _relocate_hook(root: Path) -> Path:
    """Place the real hook at ``<root>/scripts/hooks/`` so it derives ``root``.

    A symlink, not a copy: the hook resolves its repo root from ``$0``'s
    directory, so this exercises the shipped script rather than a snapshot of
    it that could drift away from the file under test.
    """
    return place_hook(root, registry=_DEFAULT_REGISTRY, sources=SourceFixture(scanner=None, with_sources=False))


def _plant_always_finding_scanner(root: Path) -> None:
    """Give a relocated checkout a scanner that reports a finding on ANY input.

    These tests are about which VISIBILITY the gate resolves, not about what the
    scanner detects. An always-finding stub makes the two outcomes unambiguous:
    exit 0 can only mean the scan was SKIPPED on a resolved private verdict, and
    a refusal can only mean the undetermined path ran the scan.
    """
    (root / "scripts" / "privacy_scan.py").write_text(_FINDING_SCANNER, encoding="utf-8")


class TestVisibilityProbeResolvesCoreAndReportsItsFailures:
    """The probe must find core in EITHER layout, and never fail silently.

    Two independent defects made a PRIVATE repo scan as public forever. The
    fallback offered only ``<root>/src`` on ``PYTHONPATH``, which is where core
    lives in a standalone checkout but NOT in a fork that vendors it at
    ``<root>/vendor/teatree/src`` — so the probe could not import ``teatree``
    at all. And both probe branches redirected stderr to ``/dev/null``, so the
    reason was invisible: the gate reported only "could not confirm", and the
    operator saw a private repo refused over its own domain vocabulary.

    Fail-closed behaviour is unchanged and pinned by the third test: an
    undetermined verdict still scans and still blocks a real finding.
    """

    def _probe_clone(
        self,
        tmp_path: Path,
        *,
        core_src: str,
        verdict: str = "",
        fail_marker: str = "",
    ) -> tuple[Path, Path, dict[str, str]]:
        root = tmp_path / "checkout"
        root.mkdir()
        hook = _relocate_hook(root)
        _plant_always_finding_scanner(root)
        _fake_core_package(root / core_src, verdict=verdict, fail_marker=fail_marker)
        commit_guarded(root)

        origin = tmp_path / "origin"
        _make_repo(origin)
        work = tmp_path / "work"
        _git(tmp_path, "clone", str(origin), str(work))
        _git(work, "config", "user.email", _NOREPLY_EMAIL)
        _git(work, "config", "user.name", _NOREPLY_NAME)
        _git(work, "remote", "set-url", "origin", _PROBE_REMOTE_URL)

        env = _hermetic_env()
        env["PATH"] = _PATH_WITHOUT_GH
        env["T3_DATA_DIR"] = str(_isolated_state_dir(tmp_path))

        (work / "leak.txt").write_text(_PLANTED_SECRET, encoding="utf-8")
        _git(work, "add", "leak.txt")
        _git(work, "commit", "-m", "add config")
        return hook, work, env

    def _run(self, hook: Path, work: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            # `bash <hook>` is git's real pre-push invocation shape; test-only driver.
            ["/bin/bash", str(hook), "origin", _PROBE_REMOTE_URL],
            cwd=work,
            input=_push_stdin(work),
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_resolves_core_vendored_under_vendor_teatree_src(self, tmp_path: Path) -> None:
        hook, work, env = self._probe_clone(tmp_path, core_src="vendor/teatree/src", verdict="PRIVATE")

        result = self._run(hook, work, env)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "could not confirm" not in (result.stdout + result.stderr).lower()

    def test_resolves_core_in_a_standalone_checkout_under_src(self, tmp_path: Path) -> None:
        hook, work, env = self._probe_clone(tmp_path, core_src="src", verdict="PRIVATE")

        result = self._run(hook, work, env)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "could not confirm" not in (result.stdout + result.stderr).lower()

    def test_failed_probe_surfaces_its_error_and_still_fails_closed(self, tmp_path: Path) -> None:
        hook, work, env = self._probe_clone(tmp_path, core_src="src", fail_marker=_PROBE_FAILURE_MARKER)

        result = self._run(hook, work, env)

        assert _PROBE_FAILURE_MARKER in result.stderr, result.stdout + result.stderr
        assert result.returncode == 1, result.stdout + result.stderr
        assert "privacy" in (result.stdout + result.stderr).lower()


def _make_unusable_bare_pythons(bin_dir: Path) -> None:
    """Version-explicit interpreters absent, and a bare ``python3`` too old to import core (the system stub)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("python3.13", "python3.14", "python3"):
        shim = bin_dir / name
        shim.write_text("#!/usr/bin/env bash\necho 'SyntaxError: python 3.9 stub' >&2\nexit 1\n", encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestResolverNeedsNothingButTheCheckoutsSource:
    """The resolver runs under ``-S`` from the checkout's ``src`` alone, so no project env decides what it can import.

    It used to run through ``uv run --project <root>``, whose env a fresh worktree has empty and whose site
    directory runs any ``.pth`` file in it; one allowlisted remote then resolved PRIVATE in the worktree that had a
    venv and UNKNOWN in the one that did not. Now core is found on ``PYTHONPATH`` in both layouts or not at all.
    """

    def _push(self, tmp_path: Path, *, core_src: str | None) -> subprocess.CompletedProcess[str]:
        root = tmp_path / "checkout"
        root.mkdir()
        hook = _relocate_hook(root)
        _plant_always_finding_scanner(root)
        if core_src is not None:
            _fake_core_package(root / core_src, verdict="PRIVATE")
        commit_guarded(root)
        work, _env = _clone_with_remote(tmp_path, "PUBLIC")
        env = _hermetic_env()
        env["PATH"] = _PATH_WITHOUT_GH
        env["T3_DATA_DIR"] = str(_isolated_state_dir(tmp_path))
        _commit_as(work, "Real Dev", _REAL_EMAIL, "feature.txt", "clean feature line\n")
        return subprocess.run(
            ["bash", str(hook), "origin", _PROBE_REMOTE_URL],  # noqa: S607 — the hook's real invocation shape
            cwd=work,
            input=_push_stdin(work),
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    @pytest.mark.parametrize("core_src", ["src", "vendor/teatree/src"], ids=["standalone", "vendored"])
    def test_core_is_found_on_the_checkouts_own_pythonpath(self, core_src: str, tmp_path: Path) -> None:
        result = self._push(tmp_path, core_src=core_src)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "could not" not in (result.stdout + result.stderr).lower()

    def test_a_checkout_without_core_says_so_and_fails_closed(self, tmp_path: Path) -> None:
        result = self._push(tmp_path, core_src=None)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "could not run the visibility resolver" in result.stdout, result.stdout
        assert "Install Python 3.13+" in result.stdout, result.stdout
        assert "config_setting set private_repos" not in result.stdout, result.stdout


def _commit_as(work: Path, name: str, email: str, filename: str, body: str) -> None:
    (work / filename).write_text(body, encoding="utf-8")
    _git(work, "add", filename)
    _git(work, "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", "add feature")


#: Rewrite verbs a refusal must never put in front of an operator: every one of them
#: rewrites the pushed branch, and a branch under review is exactly what gets pushed.
_REWRITE_VERBS = ("filter-branch", "filter-repo", "rebase", "--force", "-f ", "amend", "reset --hard")


class TestUnconfirmedVisibilityKeepsTheIdentityGuard:
    """Unknown visibility leaves the #730 identity guard in its refusing posture.

    An UNKNOWN verdict is "could not confirm private", so a real email bound for
    a remote nobody could classify is refused exactly as on a confirmed-public
    one. The refusal must not claim the repo IS public, and it must name how to
    confirm the visibility so a genuinely private remote can be unblocked.
    """

    def _unconfirmed(self, tmp_path: Path) -> tuple[Path, dict[str, str]]:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        env["T3_CONFIG_DB"] = str(_seed_private_repos(tmp_path / "config.sqlite3", []))
        return work, env

    def test_unconfirmed_visibility_refuses_an_internal_commit_identity(self, tmp_path: Path) -> None:
        work, env = self._unconfirmed(tmp_path)
        _commit_as(work, "Real Dev", _REAL_EMAIL, "feature.txt", "clean feature line\n")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        assert _REAL_EMAIL in result.stdout
        assert "noreply" in result.stdout

    def test_unconfirmed_refusal_says_how_to_confirm_visibility(self, tmp_path: Path) -> None:
        work, env = self._unconfirmed(tmp_path)
        _commit_as(work, "Real Dev", _REAL_EMAIL, "feature.txt", "clean feature line\n")

        result = _run_hook(work, env, _push_stdin(work))
        text = result.stdout.lower()

        assert result.returncode == 1, result.stdout + result.stderr
        assert "public repo" not in text, result.stdout
        assert "visibility could not be confirmed" in text, result.stdout
        assert "private_repos" in result.stdout, result.stdout
        assert "auth login" in result.stdout, result.stdout

    def test_confirmed_public_still_refuses_the_same_commit_identity(self, tmp_path: Path) -> None:
        """Anti-vacuity: the guard is intact — only its premise now has to be proven."""
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        _commit_as(work, "Real Dev", _REAL_EMAIL, "feature.txt", "clean feature line\n")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        assert "noreply" in result.stdout


def _seed_private_repos(db: Path, entries: list[str]) -> Path:
    """A ``teatree_config_setting`` store holding one global ``private_repos`` row."""
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE teatree_config_setting "
        "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'private_repos', ?)", (json.dumps(entries),)
    )
    conn.commit()
    conn.close()
    return db


def _make_unreachable_forge_shims(bin_dir: Path) -> Path:
    """``gh``/``glab`` that log every call and never answer: the probe is unavailable."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = bin_dir / "forge-calls.log"
    for tool in ("gh", "glab"):
        shim = bin_dir / tool
        shim.write_text(f'#!/usr/bin/env bash\necho "{tool} $*" >> "{log}"\nexit 1\n', encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return log


_PRIVATE_GITLAB_REMOTE = "git@gitlab.com:acme-eng/inner/widget.git"


class TestADeclaredPrivateRemoteIsScannedUnlessTheForgeSaysPrivate:
    """``private_repos`` is a store the pusher can point anywhere, so it never establishes PRIVATE for a push.

    Only the forge's answer, asked now, does. The forge CLIs here log every call and never answer, so the
    declared remote is an undetermined one: scanned, and refused for the internal identity.
    """

    def test_a_declared_remote_is_refused_when_the_forge_does_not_answer(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        _git(work, "remote", "set-url", "origin", _PRIVATE_GITLAB_REMOTE)
        log = _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        env["T3_CONFIG_DB"] = str(_seed_private_repos(tmp_path / "config.sqlite3", ["gitlab.com/acme-eng"]))
        _commit_as(work, "Real Dev", _REAL_EMAIL, "feature.txt", "clean feature line\n")

        result = _run_hook(work, env, _push_stdin(work), remote_url=_PRIVATE_GITLAB_REMOTE)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "visibility could not be confirmed" in result.stdout, result.stdout
        assert log.exists(), "the forge was asked, and its silence left the declaration with no weight"

    def test_the_forges_own_private_answer_passes_from_two_worktrees(self, tmp_path: Path) -> None:
        """The control: the same remote, the forge answering PRIVATE now, passes whichever checkout asks."""
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        _git(work, "remote", "set-url", "origin", _PRIVATE_GITLAB_REMOTE)
        _git(work, "worktree", "add", "-b", "second", str(tmp_path / "work-b"))
        second = tmp_path / "work-b"
        _git(second, "config", "user.email", _NOREPLY_EMAIL)
        _make_glab_shim(tmp_path / "glabbin", "private")
        env["PATH"] = f"{tmp_path / 'glabbin'}{os.pathsep}{env['PATH']}"
        results = []
        for checkout, ref in ((work, "refs/heads/main"), (second, "refs/heads/second")):
            _commit_as(checkout, "Real Dev", _REAL_EMAIL, "feature.txt", "clean feature line\n")
            checkout_env = {**env, "T3_DATA_DIR": str(_isolated_state_dir(tmp_path / checkout.name))}
            results.append(
                _run_hook(checkout, checkout_env, _push_stdin(checkout, ref), remote_url=_PRIVATE_GITLAB_REMOTE)
            )

        assert [r.returncode for r in results] == [0, 0], [r.stdout + r.stderr for r in results]


class TestUrlSpelledPrivateRemoteIsProbedOnItsHost:
    """``ssh://git@host[:port]/...`` is the same private remote as ``git@host:...``.

    The URL spelling kept its userinfo and port in the slug, so the probe could not
    route it and the identity guard refused a push the SCP spelling of the very same
    remote let through. The forge is reachable and answers PRIVATE here; only the
    remote's spelling differs from the passing case.
    """

    @pytest.mark.parametrize(
        "remote",
        ["git@gitlab.com:acme-eng/inner/widget.git", "ssh://git@gitlab.com/acme-eng/inner/widget.git"],
        ids=["scp", "ssh-url"],
    )
    def test_private_gitlab_remote_passes_whatever_its_spelling(self, remote: str, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        _git(work, "remote", "set-url", "origin", remote)
        _make_glab_shim(tmp_path / "glabbin", "private")
        env["PATH"] = f"{tmp_path / 'glabbin'}{os.pathsep}{env['PATH']}"
        env["T3_CONFIG_DB"] = str(_seed_private_repos(tmp_path / "config.sqlite3", []))
        env["XDG_DATA_HOME"] = str(tmp_path / "xdg")
        _commit_as(work, "Real Dev", _REAL_EMAIL, "feature.txt", "clean feature line\n")

        result = _run_hook(work, env, _push_stdin(work), remote_url=remote)

        assert result.returncode == 0, result.stdout + result.stderr


class TestTheRefusalDoesNotAdviseDeclaringTheRepoPrivate:
    """A ``private_repos`` declaration carries no weight on the push path, so the refusal must not point at one."""

    def test_the_hint_says_to_authenticate_the_forge_cli(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        _git(work, "remote", "set-url", "origin", _PRIVATE_GITLAB_REMOTE)
        _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        _commit_as(work, "Real Dev", _REAL_EMAIL, "feature.txt", "clean feature line\n")

        result = _run_hook(work, env, _push_stdin(work), remote_url=_PRIVATE_GITLAB_REMOTE)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "glab auth login" in result.stdout, result.stdout
        assert "config_setting set private_repos" not in result.stdout, result.stdout


class TestRefusalNeverRecommendsRewritingThePushedBranch:
    """Remediation must not name a rewrite: the pushed branch is the reviewed branch.

    A ``git filter-branch`` on a branch backing an open merge request destroys
    every reviewer's line anchors and forces a full re-review. The remedy for a
    finding that has not yet reached the remote is a branch cut clean, and a new
    merge request against it.
    """

    def test_identity_refusal_names_no_rewrite(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        _commit_as(work, "Real Dev", _REAL_EMAIL, "feature.txt", "clean feature line\n")

        result = _run_hook(work, env, _push_stdin(work))
        text = (result.stdout + result.stderr).lower()

        assert result.returncode == 1, result.stdout + result.stderr
        named = [verb for verb in _REWRITE_VERBS if verb in text]
        assert not named, f"refusal recommends rewriting the pushed branch: {named}\n{result.stdout}"
        assert "new branch" in text, result.stdout

    def test_leak_refusal_names_no_rewrite(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "leak.txt").write_text(_PLANTED_SECRET, encoding="utf-8")
        _git(work, "add", "leak.txt")
        _git(work, "commit", "-m", "add config")

        result = _run_hook(work, env, _push_stdin(work))
        text = (result.stdout + result.stderr).lower()

        assert result.returncode == 1, result.stdout + result.stderr
        named = [verb for verb in _REWRITE_VERBS if verb in text]
        assert not named, f"refusal recommends rewriting the pushed branch: {named}\n{result.stdout}"


_EARLIER_LABEL = "(earlier than the pushed tip)"


class TestRefusalTeachesTheWholePushRangeIsScanned:
    """Every newly-public commit is judged on its own, so a fix-up at the tip never clears a finding.

    An operator who scrubs the tip and pushes again is refused again. The refusal must
    say which commit is behind the tip, that a later commit cannot clear it, and how to
    get the value out of every commit without rewriting anything already pushed.
    """

    def test_a_finding_fixed_at_the_tip_still_blocks_and_names_the_earlier_commit(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        leaking = _commit_file(work, "leak.txt", _PLANTED_SECRET, "add config")
        _commit_file(work, "leak.txt", "token = <placeholder>\n", "scrub config")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        assert f"commit {leaking} {_EARLIER_LABEL} file leak.txt:" in result.stdout, result.stdout
        assert "does not clear it" in result.stdout, result.stdout

    def test_a_finding_in_the_tip_commit_is_not_labelled_earlier(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        tip = _commit_file(work, "leak.txt", _PLANTED_SECRET, "add config")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        assert f"commit {tip} file leak.txt:" in result.stdout, result.stdout
        assert _EARLIER_LABEL not in result.stdout, result.stdout

    def test_a_leaking_message_in_an_earlier_commit_is_labelled_earlier(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "feature.txt").write_text("a clean feature line\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature", "-m", _PLANTED_SECRET.strip())
        leaking = _rev(work, "HEAD")
        _commit_file(work, "later.txt", "a clean later line\n", "add later")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        assert f"commit {leaking} {_EARLIER_LABEL} (commit message):" in result.stdout, result.stdout

    def test_an_annotated_tag_push_labels_against_the_tagged_commit(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        tip = _commit_file(work, "leak.txt", _PLANTED_SECRET, "add config")
        _git(work, "tag", "-a", "v1", "-m", "release v1")
        tag_object = _rev(work, "refs/tags/v1")
        assert tag_object != tip

        result = _run_hook(work, env, f"refs/tags/v1 {tag_object} refs/tags/v1 {ZERO_SHA}\n")

        assert result.returncode == 1, result.stdout + result.stderr
        assert f"commit {tip} file leak.txt:" in result.stdout, result.stdout
        assert _EARLIER_LABEL not in result.stdout, result.stdout

    def test_an_allow_marker_added_in_a_later_commit_still_blocks(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        _commit_file(work, "leak.txt", _PLANTED_SECRET, "add config")
        annotated = _PLANTED_SECRET.rstrip("\n") + "  # privacy-scan:allow fixture\n"
        _commit_file(work, "leak.txt", annotated, "annotate config")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr

    def test_refusal_prescribes_the_allow_marker_and_a_new_branch_without_a_rewrite(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        _commit_file(work, "leak.txt", _PLANTED_SECRET, "add config")
        _commit_file(work, "leak.txt", "token = <placeholder>\n", "scrub config")

        result = _run_hook(work, env, _push_stdin(work))
        text = (result.stdout + result.stderr).lower()

        assert result.returncode == 1, result.stdout + result.stderr
        assert "privacy-scan:allow" in text, result.stdout
        assert "new branch" in text, result.stdout
        named = [verb for verb in _REWRITE_VERBS if verb in text]
        assert not named, f"refusal recommends rewriting the pushed branch: {named}\n{result.stdout}"


class TestRefusePublicPushWithLeakingRefName:
    """The pushed ref NAME reaches the remote exactly like the content does.

    A branch or tag name lands in the remote's branch list and every fork's
    view the moment the push completes, and GitHub keeps it in ``refs/pull/*``
    after the branch is deleted — so a name carrying a customer, partner or
    internal project is published permanently, and renaming later cannot take
    it back. The gate scanned commit messages and patches but never the name.
    """

    def _public_clone_with_banned_term(self, tmp_path: Path) -> tuple[Path, dict[str, str]]:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["TEATREE_TERM_REGISTRY"] = '{"leak":[],"prose_collider":["democorp"]}'
        (work / "feature.txt").write_text("a perfectly clean feature line\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")
        return work, env

    def test_blocks_public_push_when_the_branch_name_carries_a_banned_term(self, tmp_path: Path) -> None:
        work, env = self._public_clone_with_banned_term(tmp_path)
        _git(work, "checkout", "-b", "feat/democorp-onboarding")

        result = _run_hook(work, env, _push_stdin(work, "refs/heads/feat/democorp-onboarding"))

        assert result.returncode == 1, result.stdout + result.stderr
        assert "democorp" not in result.stdout, result.stdout
        combined = (result.stdout + result.stderr).lower()
        assert "banned_term: prose_collider#0" in combined, result.stdout
        assert "refs/pull/*" in result.stdout, result.stdout

    def test_blocks_public_push_when_a_tag_name_carries_a_banned_term(self, tmp_path: Path) -> None:
        """A ref outside ``refs/heads/`` publishes its own name too — scan it, never skip it."""
        work, env = self._public_clone_with_banned_term(tmp_path)
        _git(work, "tag", "v1.0-democorp")

        result = _run_hook(work, env, _push_stdin(work, "refs/tags/v1.0-democorp"))

        assert result.returncode == 1, result.stdout + result.stderr
        assert "democorp" not in result.stdout, result.stdout
        assert "banned_term: prose_collider#0" in result.stdout, result.stdout

    def test_blocks_public_push_when_a_hierarchical_tag_name_carries_the_term(self, tmp_path: Path) -> None:
        """Every component of a ref name is published, not only the last one."""
        work, env = self._public_clone_with_banned_term(tmp_path)
        _git(work, "tag", "democorp/v1.0")

        result = _run_hook(work, env, _push_stdin(work, "refs/tags/democorp/v1.0"))

        assert result.returncode == 1, result.stdout + result.stderr
        assert "democorp" not in result.stdout, result.stdout
        assert "banned_term: prose_collider#0" in result.stdout, result.stdout

    def test_allows_a_clean_hierarchical_tag_name(self, tmp_path: Path) -> None:
        work, env = self._public_clone_with_banned_term(tmp_path)
        _git(work, "tag", "release/v1.0")

        result = _run_hook(work, env, _push_stdin(work, "refs/tags/release/v1.0"))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_allows_public_push_with_a_clean_branch_name(self, tmp_path: Path) -> None:
        """Anti-vacuity: same public remote and same banned-terms config, clean name → pass."""
        work, env = self._public_clone_with_banned_term(tmp_path)
        _git(work, "checkout", "-b", "feat/onboarding-pipeline")

        result = _run_hook(work, env, _push_stdin(work, "refs/heads/feat/onboarding-pipeline"))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_blocks_when_only_the_remote_target_name_carries_the_term(self, tmp_path: Path) -> None:
        work, env = self._public_clone_with_banned_term(tmp_path)
        _git(work, "checkout", "-b", "feat/clean-local-name")

        result = _run_hook(
            work,
            env,
            _push_stdin(
                work,
                "refs/heads/feat/clean-local-name",
                "refs/heads/feat/democorp-onboarding",
            ),
        )

        assert result.returncode == 1, result.stdout + result.stderr
        assert "democorp" not in result.stdout
        assert "banned_term: prose_collider#0" in result.stdout

    def test_allows_a_leaking_local_name_pushed_under_a_clean_remote_name(self, tmp_path: Path) -> None:
        work, env = self._public_clone_with_banned_term(tmp_path)
        _git(work, "checkout", "-b", "feat/democorp-local-only")

        result = _run_hook(
            work,
            env,
            _push_stdin(
                work,
                "refs/heads/feat/democorp-local-only",
                "refs/heads/feat/clean-published-name",
            ),
        )

        assert result.returncode == 0, result.stdout + result.stderr

    def test_scanner_crash_on_the_ref_name_fails_closed(self, tmp_path: Path) -> None:
        """A ref-name scan that cannot run refuses the push: the name is published for good."""
        work, env = self._public_clone_with_banned_term(tmp_path)
        hook = _relocated_checkout(tmp_path, scanner=_crashing_scanner(1, when="ref"))

        result = _run_hook(work, env, _push_stdin(work, "refs/heads/main"), hook=hook)

        assert result.returncode == 1, "ref-name scanner crash must fail CLOSED: " + result.stdout + result.stderr
        assert "ref-name privacy scan could not run" in result.stdout, result.stdout


def _commit_verbatim(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "--cleanup=verbatim", "-m", message)
    return _rev(repo, "HEAD")


_QUOTED_HUNK_MESSAGE = f"docs: quote the config diff\n\n@@ -1 +1 @@\n-{_PLANTED_SECRET}+token = <placeholder>\n"


class TestCommitMessagesAreScannedAsText:
    """A commit message is published verbatim, so it is never read as part of a diff.

    ``git log --patch`` puts no separator between one commit's last hunk line and the
    next commit's subject, and a message can quote a hunk header itself. Read as a diff,
    a message line opening with ``-`` or a space passes as a removed or context line.
    """

    @pytest.mark.parametrize("prefix", ["-", " "], ids=["minus", "space"])
    def test_a_subject_right_after_a_hunk_is_still_scanned(self, tmp_path: Path, prefix: str) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        older = _commit_verbatim(work, "a.txt", "a clean line\n", prefix + _PLANTED_SECRET)
        _commit_verbatim(work, "b.txt", "another clean line\n", "add b\n")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        assert f"commit {older} {_EARLIER_LABEL} (commit message):" in result.stdout, result.stdout

    def test_a_clean_subject_right_after_a_hunk_passes(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        _commit_verbatim(work, "a.txt", "a clean line\n", "- tidy wording\n")
        _commit_verbatim(work, "b.txt", "another clean line\n", "add b\n")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_message_quoting_a_hunk_does_not_hide_its_own_lines(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        tip = _commit_verbatim(work, "notes.txt", "a clean note\n", _QUOTED_HUNK_MESSAGE)

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        assert f"commit {tip} (commit message):" in result.stdout, result.stdout

    def test_removing_an_already_public_secret_line_still_passes(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        _make_repo(origin)
        _commit_file(origin, "config.txt", _PLANTED_SECRET, "seed config")
        work, env = _public_work_clone(tmp_path, origin)
        _commit_file(work, "config.txt", "token = <placeholder>\n", "scrub config")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_message_scan_never_consumes_the_remaining_ref_updates(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        clean = _commit_file(work, "a.txt", "a clean line\n", "add a")
        _git(work, "checkout", "-b", "feature", "origin/main")
        leak = _commit_verbatim(work, "notes.txt", "a clean note\n", _QUOTED_HUNK_MESSAGE)
        stdin = (
            f"refs/heads/main {clean} refs/heads/main {ZERO_SHA}\n"
            f"refs/heads/feature {leak} refs/heads/feature {ZERO_SHA}\n"
        )

        result = _run_hook(work, env, stdin)

        assert result.returncode == 1, result.stdout + result.stderr
        assert f"commit {leak} (commit message):" in result.stdout, result.stdout


class TestHostQualifiedPrivateDeclarationNeverSkipsPublicPush:
    def test_gitlab_group_does_not_exempt_same_github_owner(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        _git(work, "remote", "set-url", "origin", "https://github.com/acme-eng/oss-lib.git")
        _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        env["T3_CONFIG_DB"] = str(_seed_private_repos(tmp_path / "config.sqlite3", ["gitlab.com/acme-eng"]))
        _commit_as(work, "Real Dev", _REAL_EMAIL, "feature.txt", "clean feature line\n")

        result = _run_hook(work, env, _push_stdin(work), remote_url="https://github.com/acme-eng/oss-lib.git")

        assert result.returncode == 1, result.stdout + result.stderr
        assert _REAL_EMAIL in result.stdout
        assert "visibility could not be confirmed" in result.stdout


class TestTheHookReadsNoEnvironmentOverride:
    """Nothing in the environment redirects the scanner, the visibility verdict or the findings code.

    Each variable below would let the caller swap a stage of the gate for a command of its own choosing.
    """

    _SEAMS = ("T3_REPO_VISIBILITY_CMD", "T3_PRIVACY_SCAN_CMD", "T3_PRIVACY_FINDINGS_EXIT_CODE")

    def test_hook_has_no_env_seams(self, tmp_path: Path) -> None:
        text = HOOK.read_text(encoding="utf-8")
        assert [name for name in self._SEAMS if name in text] == []

        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        always_clean = tmp_path / "always-clean"
        always_clean.write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n", encoding="utf-8")
        always_private = tmp_path / "always-private"
        always_private.write_text("#!/bin/sh\necho PRIVATE\n", encoding="utf-8")
        for script in (always_clean, always_private):
            script.chmod(0o755)
        env.update(
            T3_PRIVACY_SCAN_CMD=str(always_clean),
            T3_REPO_VISIBILITY_CMD=str(always_private),
            T3_PRIVACY_FINDINGS_EXIT_CODE="99",
        )
        (work / "leak.txt").write_text(_PLANTED_SECRET, encoding="utf-8")
        _git(work, "add", "leak.txt")
        _git(work, "commit", "-m", "add config")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, "an env override must change nothing: " + result.stdout + result.stderr
        assert "carries privacy findings" in result.stdout, result.stdout


class TestRepoLocalGitStateDoesNotChangeWhatGitFeedsTheHook:
    """Repo-local git state that changes what git hands the hook is one class, and each instance with a flag closes.

    Filters, textconv drivers, replace refs and the log output encoding live in ``.git/config``,
    ``.git/info/attributes`` or ``.git/refs/replace``, so a pusher can set them with no tracked file changed and no
    environment variable. The hook reads raw blobs and the unconverted, unreplaced history.
    """

    @staticmethod
    def _stdout(cwd: Path, *args: str) -> str:
        return subprocess.run(
            [str(shutil.which("git")), *args], cwd=cwd, check=True, capture_output=True, env=_hermetic_env()
        ).stdout.decode("utf-8", "replace")

    def test_a_smudge_filter_does_not_change_the_copy_of_the_sources_the_hook_runs(self, tmp_path: Path) -> None:
        work, env, hook = _leaking_hook_push(tmp_path)
        root = hook.parents[2]
        smudge = tmp_path / "smudge.sh"
        smudge.write_text(
            "#!/bin/sh\ncat >/dev/null\nprintf 'print(\"Privacy scan: clean (0 findings)\")\\n'\n", encoding="utf-8"
        )
        smudge.chmod(0o755)
        _git(root, "config", "filter.evil.smudge", str(smudge))
        _git(root, "config", "filter.evil.clean", "cat")
        (root / ".git" / "info").mkdir(exist_ok=True)
        (root / ".git" / "info" / "attributes").write_text("scripts/privacy_scan.py filter=evil\n", encoding="utf-8")
        assert "clean (0 findings)" in self._stdout(root, "archive", "HEAD", "scripts/privacy_scan.py").replace(
            "\0", ""
        ), "the plant must reach what git archive writes, or this test proves nothing"

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "the smudged copy judged the push: " + result.stdout + result.stderr
        assert "privacy findings" in result.stdout, result.stdout

    def test_a_replaced_source_blob_does_not_change_the_copy_of_the_sources_the_hook_runs(self, tmp_path: Path) -> None:
        work, env, hook = _leaking_hook_push(tmp_path)
        root = hook.parents[2]
        real = self._stdout(root, "rev-parse", "HEAD:scripts/privacy_scan.py").strip()
        evil = (
            subprocess.run(
                [str(shutil.which("git")), "hash-object", "-w", "--stdin"],
                cwd=root,
                input=_CLEAN_SCANNER.encode(),
                check=True,
                capture_output=True,
                env=_hermetic_env(),
            )
            .stdout.decode()
            .strip()
        )
        _git(root, "replace", real, evil)
        assert "clean (0 findings)" in self._stdout(root, "cat-file", "blob", "HEAD:scripts/privacy_scan.py")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "the replacement blob judged the push: " + result.stdout + result.stderr
        assert "privacy findings" in result.stdout, result.stdout

    def test_a_textconv_driver_does_not_blank_the_patch_the_scanner_reads(self, tmp_path: Path) -> None:
        work, env, hook = _leaking_hook_push(tmp_path)
        blank = tmp_path / "blank.sh"
        blank.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        blank.chmod(0o755)
        _git(work, "config", "diff.blank.textconv", str(blank))
        (work / ".git" / "info").mkdir(exist_ok=True)
        (work / ".git" / "info" / "attributes").write_text("leak.txt diff=blank\n", encoding="utf-8")
        assert "second-leak-term" not in self._stdout(work, "log", "-p"), "the plant must blank what git log shows"

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "the converted patch judged the push: " + result.stdout + result.stderr
        assert "file leak.txt" in result.stdout, "the refusal attributes the finding to its file: " + result.stdout

    def test_a_replaced_commit_does_not_change_what_the_range_shows(self, tmp_path: Path) -> None:
        work, env, hook = _leaking_hook_push(tmp_path)
        leak = _rev(work, "HEAD")
        clean = self._stdout(
            work, "commit-tree", f"{leak}~1^{{tree}}", "-p", f"{leak}~1", "-m", "a clean stand-in"
        ).strip()
        _git(work, "replace", leak, clean)
        assert "second-leak-term" not in self._stdout(work, "log", "-p", leak), "the plant must hide the leak"

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "the replacement commit judged the push: " + result.stdout + result.stderr
        assert "privacy findings" in result.stdout, result.stdout

    def test_a_log_output_encoding_does_not_blind_the_message_scan(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["TEATREE_TERM_REGISTRY"] = _REAL_REGISTRY
        _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        tip = _commit_verbatim(work, "a.txt", "a clean line\n", "feat: add a\n\nnames second-leak-term\n")
        _git(work, "config", "i18n.logOutputEncoding", "UTF-16")
        hook = place_hook(tmp_path / "checkout", registry=_REAL_REGISTRY)

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "the re-encoded message was not read: " + result.stdout + result.stderr
        assert f"commit {tip} (commit message):" in result.stdout, result.stdout


class TestACommitMessageIsScannedAsStoredAndAsDecoded:
    """A commit's ``encoding`` header travels with the pushed object, so what git decodes is not all the remote holds.

    ``i18n.commitEncoding=UTF-16LE`` on plain bytes makes ``git log`` re-decode them into noise while the raw object on
    the remote still carries the message, and no CI job reads commit messages. The hook scans each message as git
    decodes it (UTF-8, which also reads a message stored in another encoding) and as the object stores it.
    """

    @staticmethod
    def _push(
        tmp_path: Path,
        *,
        registry: str,
        message: bytes,
        commit_config: tuple[str, ...] = (),
        repo_config: tuple[tuple[str, str], ...] = (),
    ) -> tuple[Path, str, str]:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        for key, value in repo_config:
            _git(work, "config", key, value)
        env["TEATREE_TERM_REGISTRY"] = registry
        _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        (work / "a.txt").write_text("a clean line\n", encoding="utf-8")
        _git(work, "add", "a.txt")
        message_file = tmp_path / "message.txt"
        message_file.write_bytes(message)
        _git(work, *commit_config, "commit", "--cleanup=verbatim", "-F", str(message_file))
        tip = _rev(work, "HEAD")
        hook = place_hook(tmp_path / "checkout", registry=registry)
        result = _run_hook(work, env, _push_stdin(work), hook=hook)
        return work, tip, f"{result.returncode}\n{result.stdout}{result.stderr}"

    @pytest.mark.parametrize(
        "commit_config",
        [(), ("-c", "i18n.commitEncoding=UTF-16LE")],
        ids=["no-encoding-header", "wrong-encoding-header"],
    )
    def test_a_leaking_message_is_refused_whatever_its_encoding_header(
        self, tmp_path: Path, commit_config: tuple[str, ...]
    ) -> None:
        _work, tip, report = self._push(
            tmp_path,
            registry=_REAL_REGISTRY,
            message=b"release notes mention second-leak-term here\n",
            commit_config=commit_config,
        )

        assert report.startswith("1\n"), "the message landed: " + report
        assert f"commit {tip} (commit message):" in report, report

    def test_a_message_stored_in_another_encoding_is_scanned_as_git_decodes_it(self, tmp_path: Path) -> None:
        """Shift_JIS stores "ア" as 0x83 0x41: the stored bytes read ``second-leak-A-term``; only UTF-8 matches."""
        _work, tip, report = self._push(
            tmp_path,
            registry=_REAL_REGISTRY,
            message="notes mention second-leak-ア-term here\n".encode("shift_jis"),
            repo_config=(("i18n.commitEncoding", "Shift_JIS"),),
        )

        assert report.startswith("1\n"), "the message landed: " + report
        assert f"commit {tip} (commit message):" in report, report


@dataclass(frozen=True)
class _CraftedCommit:
    """What a hand-made commit object carries beyond ``git commit``'s own output."""

    author_name: str = "Dev"
    committer_name: str = "Dev"
    author_email: str = _NOREPLY_EMAIL
    committer_email: str = _NOREPLY_EMAIL
    extra_header: str = ""
    message: str = "a clean message\n"
    blank_line: bool = True
    second_parent: str = ""


def _craft_commit(repo: Path, plant: _CraftedCommit) -> str:
    """Write a commit object by hand, on ``HEAD``'s tree, so its headers can carry what ``git commit`` would not."""
    lines = [f"tree {_rev(repo, 'HEAD^{tree}')}", f"parent {_rev(repo, 'HEAD')}"]
    lines += [f"parent {plant.second_parent}"] if plant.second_parent else []
    lines += [f"author {plant.author_name} <{plant.author_email}> 1700000000 +0000"]
    lines += [f"committer {plant.committer_name} <{plant.committer_email}> 1700000000 +0000"]
    lines += [plant.extra_header] if plant.extra_header else []
    text = "\n".join(lines) + "\n" + (f"\n{plant.message}" if plant.blank_line else "")
    return (
        subprocess.run(
            [str(shutil.which("git")), "hash-object", "-t", "commit", "-w", "--stdin"],
            cwd=repo,
            input=text.encode(),
            check=True,
            capture_output=True,
            env=_hermetic_env(),
        )
        .stdout.decode()
        .strip()
    )


#: How a commit object can carry a term outside its message.
_HEADER_PLANTS = {
    "extra-header": _CraftedCommit(extra_header="x-note mentions second-leak-term"),
    "author-name": _CraftedCommit(author_name="second-leak-term"),
    "committer-name": _CraftedCommit(committer_name="second-leak-term"),
    "header-without-a-blank-line": _CraftedCommit(extra_header="x-note second-leak-term", blank_line=False),
}


#: A secret and a home path in a header, built from the marked fixtures above so this file never holds the bare value.
_FIXTURE_TOKEN = _PLANTED_SECRET.split("= ", 1)[1].strip()
_FIXTURE_HOME_PATH = _PLANTED_HOME_PATH.split("see ", 1)[1].strip()
_GENERIC_HEADER_PLANTS = {
    "secret-in-an-extra-header": (_CraftedCommit(extra_header=f"x-note token {_FIXTURE_TOKEN}"), "api_key"),
    "home-path-in-an-extra-header": (_CraftedCommit(extra_header=f"x-note see {_FIXTURE_HOME_PATH}"), "home_path"),
    "secret-in-a-header-without-a-blank-line": (
        _CraftedCommit(extra_header=f"x-note token {_FIXTURE_TOKEN}", blank_line=False),
        "api_key",
    ),
    "home-path-in-a-header-without-a-blank-line": (
        _CraftedCommit(extra_header=f"x-note see {_FIXTURE_HOME_PATH}", blank_line=False),
        "home_path",
    ),
}


#: A secret-shaped login: the identity rule accepts any ``[A-Za-z0-9-]+@users.noreply.github.com``, and ``sk-`` fits it.
_LOGIN_TOKEN = "sk" + "-" + "A" * 24
_DISGUISED = f"{_LOGIN_TOKEN}@users.noreply.github.com"
_LOGIN_PLANTS = {
    "author": _CraftedCommit(author_email=_DISGUISED),
    "committer": _CraftedCommit(committer_email=_DISGUISED),
    "duplicate-author-line": _CraftedCommit(extra_header=f"author y <{_DISGUISED}> 1700000000 +0000"),
    "line-mimicking-an-identity-inside-a-signature": _CraftedCommit(
        extra_header="\n".join(
            [
                "gpgsig -----BEGIN PGP SIGNATURE-----",
                f" author y <{_DISGUISED}> 1700000000 +0000",
                " -----END PGP SIGNATURE-----",
            ]
        )
    ),
}


def _craft_tag(repo: Path, *, tagger_email: str) -> str:
    """Write an annotated tag object by hand, on ``HEAD``, so its ``tagger`` line carries what ``git tag`` would not."""
    head = ["object " + _rev(repo, "HEAD"), "type commit", "tag v9", f"tagger Dev <{tagger_email}> 1700000000 +0000"]
    text = "\n".join(head) + "\n\na clean tag\n"
    return (
        subprocess.run(
            [str(shutil.which("git")), "hash-object", "-t", "tag", "-w", "--stdin"],
            cwd=repo,
            input=text.encode(),
            check=True,
            capture_output=True,
            env=_hermetic_env(),
        )
        .stdout.decode()
        .strip()
    )


def _signed_tag_mergetag(tagged: str) -> str:
    """The ``mergetag`` header git writes when it merges a SIGNED tag, with a placeholder signature block."""
    lines = [
        f"mergetag object {tagged}",
        " type commit",
        " tag v1",
        f" tagger Dev <{_NOREPLY_EMAIL}> 1700000000 +0000",
        " ",
        " a clean signed tag",
        " -----BEGIN PGP SIGNATURE-----",
        " ",
        " PLACEHOLDERSIGNATUREBLOCKNOTAREALSIGNATURE",
        " -----END PGP SIGNATURE-----",
    ]
    return "\n".join(lines)


class TestACommitObjectIsScannedWholeNotOnlyItsMessage:
    """A commit object carries more text than its message: extra headers and the author and committer names.

    Those travel with the pushed object and no CI job reads commit objects, so the hook feeds the scanner each whole
    object for registry terms and, with only the noreply domain cut, for every other detector: a noreply address is no
    finding, while a secret or a home path in a header, or in the login before that domain, is refused.
    """

    @staticmethod
    def _push(tmp_path: Path, plant: _CraftedCommit) -> tuple[str, str]:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["TEATREE_TERM_REGISTRY"] = _REAL_REGISTRY
        _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        crafted = _craft_commit(work, plant)
        hook = place_hook(tmp_path / "checkout", registry=_REAL_REGISTRY)
        result = _run_hook(work, env, f"refs/heads/feature {crafted} refs/heads/feature {ZERO_SHA}\n", hook=hook)
        return crafted, f"{result.returncode}\n{result.stdout}{result.stderr}"

    @pytest.mark.parametrize("plant", list(_HEADER_PLANTS.values()), ids=list(_HEADER_PLANTS))
    def test_a_term_in_a_header_is_refused(self, tmp_path: Path, plant: _CraftedCommit) -> None:
        crafted, report = self._push(tmp_path, plant)

        assert report.startswith("1\n"), "the term landed in a commit header: " + report
        assert f"commit {crafted}" in report, report
        assert "leak#" in report, report
        assert "second-leak-term" not in report, "a refusal never prints the term"

    @pytest.mark.parametrize(
        ("plant", "category"), list(_GENERIC_HEADER_PLANTS.values()), ids=list(_GENERIC_HEADER_PLANTS)
    )
    def test_a_secret_or_a_home_path_in_a_header_is_refused(
        self, tmp_path: Path, plant: _CraftedCommit, category: str
    ) -> None:
        """Every detector reads the object, not only the registry-term pass; the same value in a message is refused."""
        crafted, report = self._push(tmp_path, plant)

        assert report.startswith("1\n"), "the value landed in a commit header: " + report
        assert f"commit {crafted}" in report, report
        assert category in report, report

    @pytest.mark.parametrize("plant", list(_LOGIN_PLANTS.values()), ids=list(_LOGIN_PLANTS))
    def test_a_secret_shaped_login_is_refused_though_the_domain_is_noreply(
        self, tmp_path: Path, plant: _CraftedCommit
    ) -> None:
        """Only the exact ``@users.noreply.github.com`` is hidden from the detectors, not the login in front of it."""
        crafted, report = self._push(tmp_path, plant)

        assert report.startswith("1\n"), "the token landed as a noreply login: " + report
        assert f"commit {crafted}" in report, report
        assert "api_key" in report, report

    def test_a_secret_shaped_login_in_a_tagger_line_is_refused(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["TEATREE_TERM_REGISTRY"] = _REAL_REGISTRY
        _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        tag = _craft_tag(work, tagger_email=_DISGUISED)
        hook = place_hook(tmp_path / "checkout", registry=_REAL_REGISTRY)

        result = _run_hook(work, env, f"refs/tags/v9 {tag} refs/tags/v9 {ZERO_SHA}\n", hook=hook)

        assert result.returncode == 1, "the token landed as a tagger login: " + result.stdout + result.stderr
        assert "api_key" in result.stdout, result.stdout

    def test_a_clean_noreply_tagger_passes(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["TEATREE_TERM_REGISTRY"] = _REAL_REGISTRY
        _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        tag = _craft_tag(work, tagger_email=_NOREPLY_EMAIL)
        hook = place_hook(tmp_path / "checkout", registry=_REAL_REGISTRY)

        result = _run_hook(work, env, f"refs/tags/v9 {tag} refs/tags/v9 {ZERO_SHA}\n", hook=hook)

        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_clean_merge_of_a_signed_tag_passes(self, tmp_path: Path) -> None:
        """Git copies the tag's ``tagger`` line, indented, into a ``mergetag`` header: a noreply address, no finding."""
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["TEATREE_TERM_REGISTRY"] = _REAL_REGISTRY
        _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        _git(work, "checkout", "-b", "side")
        side = _commit_file(work, "side.txt", "a clean side line\n", "side work")
        _git(work, "checkout", "-")
        merge = _craft_commit(work, _CraftedCommit(second_parent=side, extra_header=_signed_tag_mergetag(side)))
        hook = place_hook(tmp_path / "checkout", registry=_REAL_REGISTRY)

        result = _run_hook(work, env, f"refs/heads/feature {merge} refs/heads/feature {ZERO_SHA}\n", hook=hook)

        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_clean_merge_commit_passes(self, tmp_path: Path) -> None:
        """Anti-vacuity: a merge has two ``parent`` lines and a noreply committer, and is no finding either."""
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["TEATREE_TERM_REGISTRY"] = _REAL_REGISTRY
        _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        _git(work, "checkout", "-b", "side")
        _commit_file(work, "side.txt", "a clean side line\n", "side work")
        _git(work, "checkout", "-")
        _commit_file(work, "main.txt", "a clean main line\n", "main work")
        _git(work, "merge", "--no-ff", "-m", "merge side", "side")
        hook = place_hook(tmp_path / "checkout", registry=_REAL_REGISTRY)

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 0, result.stdout + result.stderr


class TestATermInAPathIsRefused:
    """A registry term can also ride out as a file path (new, renamed, deleted).

    A path is a line of the diff header (``diff --git``, ``rename to``, ``--- a/``) and the scanner reads the header
    lines of a diff as well as its hunks.
    """

    @staticmethod
    def _push(tmp_path: Path, *, change: str) -> str:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["TEATREE_TERM_REGISTRY"] = _REAL_REGISTRY
        _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        origin = tmp_path / "origin"
        if change == "new":
            (work / "second-leak-term.txt").write_text("a clean line\n", encoding="utf-8")
        elif change == "rename":
            _git(work, "mv", "README.md", "second-leak-term.md")
        elif change == "delete":
            _commit_file(origin, "second-leak-term.txt", "a clean line\n", "add a file")
            _git(work, "pull", "-q", "--ff-only", str(origin), "main")
            _git(work, "rm", "-q", "second-leak-term.txt")
        _git(work, "add", "-A")
        _git(work, "commit", "--allow-empty", "-m", "a clean message")
        sha = _rev(work, "HEAD")
        hook = place_hook(tmp_path / "checkout", registry=_REAL_REGISTRY)
        result = _run_hook(work, env, f"refs/heads/feature {sha} refs/heads/feature {ZERO_SHA}\n", hook=hook)
        return f"{result.returncode}\n{result.stdout}{result.stderr}"

    @pytest.mark.parametrize("change", ["new", "rename", "delete"])
    def test_a_path_with_a_term_is_refused(self, tmp_path: Path, change: str) -> None:
        report = self._push(tmp_path, change=change)

        assert report.startswith("1\n"), f"a {change} path with a term landed: " + report
        assert "leak#" in report, report


class TestCommitMessagesRideTheScannersStdin:
    """A message is handed to the scanner on stdin: the scan writes no file, and the hook leaves nothing in ``TMPDIR``.

    The hook needs one directory of its own, for the copy of the committed sources it runs. Without one it refuses:
    a gate that cannot make it has not looked at the push.
    """

    @staticmethod
    def _usable_tmpdir(tmp_path: Path, env: dict[str, str]) -> Path:
        tmpdir = tmp_path / "tmpdir"
        tmpdir.mkdir()
        env["TMPDIR"] = str(tmpdir)
        return tmpdir

    def test_messages_are_scanned_from_stdin_and_nothing_is_left_in_tmpdir(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        tmpdir = self._usable_tmpdir(tmp_path, env)
        tip = _commit_verbatim(work, "a.txt", "a clean line\n", "feat: add a\n\n" + _PLANTED_SECRET)

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        assert f"commit {tip} (commit message):" in result.stdout, result.stdout
        assert list(tmpdir.iterdir()) == []

    def test_a_clean_message_passes_and_leaves_nothing_in_tmpdir(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        tmpdir = self._usable_tmpdir(tmp_path, env)
        _commit_verbatim(work, "a.txt", "a clean line\n", "feat: add a\n")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr
        assert list(tmpdir.iterdir()) == []

    def test_an_unusable_tmpdir_refuses_instead_of_skipping_the_scan(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["TMPDIR"] = str(tmp_path / "no-such-tmpdir")
        _commit_verbatim(work, "a.txt", "a clean line\n", "feat: add a\n\n" + _PLANTED_SECRET)

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        assert "directory for the committed sources could not be made" in result.stdout, result.stdout


class TestRootForcedAndTagRangesAreCheckedForRegistryTerms:
    """The ranges where "what is new" is least clear still get the registry-term pass.

    A term is generated per test, so the registry holds nothing but a placeholder.
    """

    @staticmethod
    def _public_clone(tmp_path: Path) -> tuple[Path, dict[str, str], str]:
        term = "zq" + secrets.token_hex(4)
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["TEATREE_TERM_REGISTRY"] = json.dumps({"leak": [term]})
        return work, env, term

    @staticmethod
    def _assert_refused_without_the_value(result: subprocess.CompletedProcess[str], term: str) -> None:
        assert result.returncode == 1, result.stdout + result.stderr
        assert term not in result.stdout + result.stderr, "the refusal printed the registry term"
        assert "banned_term: leak#0" in result.stdout, result.stdout

    def test_a_new_root_range_is_scanned_for_the_term(self, tmp_path: Path) -> None:
        work, env, term = self._public_clone(tmp_path)
        _git(work, "update-ref", "-d", "refs/remotes/origin/main")
        _commit_file(work, "notes.txt", f"the {term} sits in a note\n", "add notes")

        self._assert_refused_without_the_value(_run_hook(work, env, _push_stdin(work)), term)

    def test_a_forced_range_is_scanned_for_the_term(self, tmp_path: Path) -> None:
        work, env, term = self._public_clone(tmp_path)
        _git(work, "checkout", "-b", "replaced")
        replaced_remote_tip = _commit_file(work, "old.txt", "the history this push replaces\n", "old tip")
        _git(work, "checkout", "main")
        _commit_file(work, "notes.txt", f"the {term} sits in a note\n", "add notes")
        stdin = f"refs/heads/main {_rev(work, 'HEAD')} refs/heads/main {replaced_remote_tip}\n"

        self._assert_refused_without_the_value(_run_hook(work, env, stdin), term)

    def test_a_forced_range_against_a_remote_tip_the_clone_has_never_seen_is_scanned(self, tmp_path: Path) -> None:
        work, env, term = self._public_clone(tmp_path)
        _commit_file(work, "notes.txt", f"the {term} sits in a note\n", "add notes")
        stdin = f"refs/heads/main {_rev(work, 'HEAD')} refs/heads/main {'ab' * 20}\n"

        self._assert_refused_without_the_value(_run_hook(work, env, stdin), term)

    def test_an_annotated_tag_message_is_scanned_even_when_its_range_is_empty(self, tmp_path: Path) -> None:
        work, env, term = self._public_clone(tmp_path)
        _git(work, "tag", "-a", "v2", "-m", f"release notes mention {term}", "HEAD")
        stdin = f"refs/tags/v2 {_rev(work, 'refs/tags/v2')} refs/tags/v2 {ZERO_SHA}\n"

        result = _run_hook(work, env, stdin)

        self._assert_refused_without_the_value(result, term)
        assert "annotated tag" in result.stdout, result.stdout

    def test_an_annotated_tag_message_with_a_secret_is_refused(self, tmp_path: Path) -> None:
        """A tag object is read whole by every detector, tagger line blanked, like a commit object."""
        work, env, _term = self._public_clone(tmp_path)
        _git(work, "tag", "-a", "v2", "-m", f"release token {_FIXTURE_TOKEN}", "HEAD")
        stdin = f"refs/tags/v2 {_rev(work, 'refs/tags/v2')} refs/tags/v2 {ZERO_SHA}\n"

        result = _run_hook(work, env, stdin)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "annotated tag" in result.stdout, result.stdout
        assert "api_key" in result.stdout, result.stdout

    def test_a_clean_annotated_tag_passes(self, tmp_path: Path) -> None:
        work, env, _term = self._public_clone(tmp_path)
        _git(work, "tag", "-a", "v2", "-m", "release notes", "HEAD")
        stdin = f"refs/tags/v2 {_rev(work, 'refs/tags/v2')} refs/tags/v2 {ZERO_SHA}\n"

        result = _run_hook(work, env, stdin)

        assert result.returncode == 0, result.stdout + result.stderr


class TestRefusalsNameARegistryTermByClassAndIndexOnly:
    """A refusal that has to mention an identity or a ref name never prints it when it is, or holds, a registry term."""

    @staticmethod
    def _public_clone(tmp_path: Path, term: str) -> tuple[Path, dict[str, str]]:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["TEATREE_TERM_REGISTRY"] = json.dumps({"leak": ["first-term", term]})
        return work, env

    def test_an_author_email_that_is_a_registry_term_is_not_printed(self, tmp_path: Path) -> None:
        term = f"zq{secrets.token_hex(4)}@qx{secrets.token_hex(3)}.com"
        work, env = self._public_clone(tmp_path, term)
        _commit_as(work, "Real Dev", term, "feature.txt", "clean feature line\n")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        assert term not in result.stdout + result.stderr
        assert "non-noreply commit identity" in result.stdout
        assert "registry term leak#1" in result.stdout, result.stdout

    def test_an_ordinary_non_noreply_email_is_still_printed(self, tmp_path: Path) -> None:
        work, env = self._public_clone(tmp_path, f"zq{secrets.token_hex(4)}")
        _commit_as(work, "Real Dev", _REAL_EMAIL, "feature.txt", "clean feature line\n")

        result = _run_hook(work, env, _push_stdin(work))

        assert _REAL_EMAIL in result.stdout, result.stdout

    def test_a_local_branch_name_that_holds_a_registry_term_is_not_printed(self, tmp_path: Path) -> None:
        term = f"zq{secrets.token_hex(4)}"
        work, env = self._public_clone(tmp_path, term)
        local_ref = f"refs/heads/feat/{term}-local"
        _git(work, "checkout", "-b", f"feat/{term}-local")
        _commit_file(work, "leak.txt", _PLANTED_SECRET, "add config")

        result = _run_hook(work, env, f"{local_ref} {_rev(work, 'HEAD')} refs/heads/feat/clean-name {ZERO_SHA}\n")

        assert result.returncode == 1, result.stdout + result.stderr
        assert term not in result.stdout + result.stderr
        assert "carries privacy findings" in result.stdout


class TestTheHookHoldsTheLoadedRegistryToTheCommittedFingerprint:
    """A registry of the caller's choosing is not the registry the checkout commits digests for, so it gates nothing.

    The environment can carry a fake ``TEATREE_TERM_REGISTRY`` or ``T3_CONFIG_DB`` that no inline-assignment
    check sees (``export``, ``printf -v``, ``read``, ``source``, a Python ``os.environ``). The hook compares the
    lists it loaded with the digests committed at its own checkout's HEAD and refuses on any difference, on a
    fingerprint it cannot read, and on one that is not committed unchanged. All terms are SYNTHETIC.
    """

    def _push(
        self,
        tmp_path: Path,
        *,
        env_registry: str,
        fingerprint: CommittedFingerprint | None = None,
        leaking: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["TEATREE_TERM_REGISTRY"] = env_registry
        body = "a line naming second-leak-term\n" if leaking else "a clean feature line\n"
        (work / "feature.txt").write_text(body, encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")
        hook = place_hook(tmp_path / "checkout", registry=env_registry, fingerprint=fingerprint)
        return _run_hook(work, env, _push_stdin(work), hook=hook)

    def test_the_registry_the_checkout_commits_is_accepted_and_still_catches_its_terms(self, tmp_path: Path) -> None:
        result = self._push(tmp_path, env_registry=_REAL_REGISTRY, leaking=True)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "leak#1" in result.stdout, "the scan ran with the verified registry"
        assert "does not match" not in result.stdout

    def test_a_clean_push_with_the_committed_registry_passes(self, tmp_path: Path) -> None:
        result = self._push(tmp_path / "a", env_registry=_REAL_REGISTRY)

        assert result.returncode == 0, result.stdout + result.stderr

    def test_an_exported_fake_registry_is_refused_though_the_push_holds_a_real_term(self, tmp_path: Path) -> None:
        result = self._push(
            tmp_path, env_registry=_FAKE_REGISTRY, fingerprint=_COMMITTED_FOR_THE_REAL_ONE, leaking=True
        )

        assert result.returncode == 1, result.stdout + result.stderr
        assert "does not match" in result.stdout
        assert "tree" in result.stdout

    def test_the_refusal_prints_no_digest_and_no_term(self, tmp_path: Path) -> None:
        result = self._push(
            tmp_path, env_registry=_FAKE_REGISTRY, fingerprint=_COMMITTED_FOR_THE_REAL_ONE, leaking=True
        )

        shown = result.stdout + result.stderr
        assert result.returncode == 1, shown
        assert "sha256" not in shown
        assert not any(
            term in shown for term in ("demo-leak-term", "second-leak-term", "demo-overlay-term", "fake-picked")
        )

    @pytest.mark.parametrize("state", ["missing", "untracked", "edited", "garbled"])
    def test_a_fingerprint_that_is_not_committed_unchanged_is_refused(
        self, tmp_path: Path, state: FingerprintState
    ) -> None:
        fingerprint = CommittedFingerprint(state=state, registry=_FAKE_REGISTRY)

        result = self._push(tmp_path, env_registry=_REAL_REGISTRY, fingerprint=fingerprint)

        assert result.returncode == 1, f"{state}: " + result.stdout + result.stderr
        assert "term-source-fingerprint.json" in result.stdout

    def test_a_fake_store_cannot_turn_a_remote_nothing_proves_private_into_a_private_one(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        log = _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        env["T3_CONFIG_DB"] = str(_seed_private_repos(tmp_path / "config.sqlite3", ["github.com/acme/widget"]))
        env["TEATREE_TERM_REGISTRY"] = _FAKE_REGISTRY
        (work / "feature.txt").write_text("a line naming second-leak-term\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")
        hook = place_hook(tmp_path / "checkout", registry=_FAKE_REGISTRY, fingerprint=_COMMITTED_FOR_THE_REAL_ONE)

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert log.exists(), "the forge was asked and could not answer, so only the fake allowlist said private"
        assert result.returncode == 1, result.stdout + result.stderr
        assert "does not match" in result.stdout

    def test_the_same_remote_passes_when_the_forge_says_private_and_the_registry_is_the_committed_one(
        self, tmp_path: Path
    ) -> None:
        """Anti-vacuity: the pass is the forge's, and the refusal above is the registry's alone."""
        work, env = _clone_with_remote(tmp_path, "PRIVATE")
        env["TEATREE_TERM_REGISTRY"] = _REAL_REGISTRY
        (work / "feature.txt").write_text("a line naming second-leak-term\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")
        hook = place_hook(tmp_path / "checkout", registry=_REAL_REGISTRY)

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 0, result.stdout + result.stderr


def _plant_with_update_ref(work: Path, sha: str) -> None:
    subprocess.run(
        [str(shutil.which("git")), "update-ref", "--stdin"],
        input=f"update refs/remotes/origin/feature {sha}\n",
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
        env=_hermetic_env(),
    )


def _plant_in_packed_refs(work: Path, sha: str) -> None:
    _git(work, "pack-refs", "--all")
    packed = work / ".git" / "packed-refs"
    lines = packed.read_text(encoding="utf-8").splitlines()
    header = [line for line in lines if line.startswith("#")]
    entries = [line for line in lines if not line.startswith("#") and not line.endswith(" refs/remotes/origin/feature")]
    entries.append(f"{sha} refs/remotes/origin/feature")
    entries.sort(key=lambda line: line.split(" ", 1)[1])
    packed.write_text("\n".join([*header, *entries]) + "\n", encoding="utf-8")


def _plant_by_fetching_head(work: Path, _sha: str) -> None:
    _git(work, "fetch", ".", "HEAD:refs/remotes/origin/feature")


def _plant_by_fetching_another_url(work: Path, _sha: str) -> None:
    attacker = work.parent / "attacker"
    _git(work.parent, "clone", str(work), str(attacker))
    _git(work, "remote", "set-url", "origin", str(attacker))
    _git(work, "fetch", "origin")
    _git(work, "remote", "set-url", "origin", _FORGE_URL)


_PLANTINGS = {
    "update-ref --stdin": _plant_with_update_ref,
    "packed-refs": _plant_in_packed_refs,
    "fetch . HEAD:remotes/origin/x": _plant_by_fetching_head,
    "set-url + fetch": _plant_by_fetching_another_url,
}


class TestThePushRangeNeverTrustsALocalTrackingRef:
    """A remote-tracking ref is a local file, so a pusher can write one that says the remote holds the leaking commit.

    Four ways to do it need no ``git push`` the PreToolUse guards could see: ``update-ref --stdin``, a line
    appended to ``packed-refs``, ``git fetch . HEAD:refs/remotes/origin/x`` and a ``set-url`` plus ``fetch`` from
    a repository of the pusher's own. The commits a push exposes are the ones the REMOTE does not hold, read from
    the remote itself (``git ls-remote --heads`` at the URL git is pushing to) and from the sha git reports for
    the ref; when the remote cannot be listed the range gets wider, never narrower.
    """

    def _leaking_branch(self, tmp_path: Path) -> tuple[Path, dict[str, str], str]:
        origin = tmp_path / "origin"
        _make_repo(origin)
        work, env = _public_work_clone(tmp_path, origin)
        _git(work, "checkout", "-b", "feature")
        leak = _commit_file(work, "leak.txt", _PLANTED_SECRET, "add config")
        return work, env, leak

    @staticmethod
    def _new_branch_push(leak: str) -> str:
        return f"refs/heads/feature {leak} refs/heads/feature {ZERO_SHA}\n"

    def test_an_honest_clone_refuses_the_leaking_commit(self, tmp_path: Path) -> None:
        work, env, leak = self._leaking_branch(tmp_path)

        result = _run_hook(work, env, self._new_branch_push(leak))

        assert result.returncode == 1, result.stdout + result.stderr
        assert "privacy" in (result.stdout + result.stderr).lower()

    @pytest.mark.parametrize("plant", list(_PLANTINGS.values()), ids=list(_PLANTINGS))
    def test_a_planted_tracking_ref_does_not_empty_the_range(self, tmp_path: Path, plant: Callable[..., None]) -> None:
        work, env, leak = self._leaking_branch(tmp_path)
        plant(work, leak)
        planted = _rev(work, "refs/remotes/origin/feature")
        assert planted == leak, "the planting must have worked, or this test proves nothing"

        result = _run_hook(work, env, self._new_branch_push(leak))

        assert result.returncode == 1, "the planted ref emptied the scanned range: " + result.stdout + result.stderr
        assert "privacy" in (result.stdout + result.stderr).lower()

    @pytest.mark.parametrize("plant", list(_PLANTINGS.values()), ids=list(_PLANTINGS))
    def test_a_planted_tracking_ref_does_not_empty_an_update_range_either(
        self, tmp_path: Path, plant: Callable[..., None]
    ) -> None:
        origin = tmp_path / "origin"
        _make_repo(origin)
        _git(origin, "checkout", "-b", "feature")
        tip = _commit_file(origin, "feature.txt", "existing feature line\n", "seed feature branch")
        _git(origin, "checkout", "main")
        work, env = _public_work_clone(tmp_path, origin)
        _git(work, "checkout", "feature")
        leak = _commit_file(work, "leak.txt", _PLANTED_SECRET, "update feature")
        plant(work, leak)
        assert _rev(work, "refs/remotes/origin/feature") == leak

        result = _run_hook(work, env, f"refs/heads/feature {leak} refs/heads/feature {tip}\n")

        assert result.returncode == 1, "the planted ref emptied the update range: " + result.stdout + result.stderr

    def test_a_url_rewrite_that_splits_the_listing_from_the_push_does_not_empty_the_range(self, tmp_path: Path) -> None:
        work, env, leak = self._leaking_branch(tmp_path)
        fake = tmp_path / "fake-origin"
        _git(tmp_path, "clone", str(work), str(fake))
        _git(work, "config", f"url.{fake}.insteadOf", _FORGE_URL)
        _git(work, "config", f"url.{_FORGE_URL}.pushInsteadOf", _FORGE_URL)
        assert leak in _git_stdout(work, "ls-remote", "--heads", _FORGE_URL), "the listing reads the fake repository"
        assert _git_stdout(work, "remote", "get-url", "--push", "origin").strip() == _FORGE_URL, "the push stays real"

        result = _run_hook(work, env, self._new_branch_push(leak))

        assert result.returncode == 1, "the rewrite emptied the scanned range: " + result.stdout + result.stderr
        assert "could not list" in (result.stdout + result.stderr).lower()

    def test_a_remote_that_cannot_be_listed_widens_the_range_instead_of_emptying_it(self, tmp_path: Path) -> None:
        work, env, leak = self._leaking_branch(tmp_path)
        _plant_with_update_ref(work, leak)
        _make_the_remote_unlistable(work)

        result = _run_hook(work, env, self._new_branch_push(leak))

        assert result.returncode == 1, result.stdout + result.stderr
        assert "could not list" in (result.stdout + result.stderr).lower()

    def test_a_from_ref_prek_derived_from_a_planted_ref_is_not_trusted(self, tmp_path: Path) -> None:
        work, env, leak = self._leaking_branch(tmp_path)
        _plant_with_update_ref(work, leak)

        result = _run_hook_synth(work, env, to_ref=leak, from_ref=leak, remote_branch="refs/heads/feature")

        assert result.returncode == 1, "prek's FROM_REF came from the planted ref: " + result.stdout + result.stderr

    def test_the_reported_remote_sha_bounds_an_update_when_the_remote_cannot_be_listed(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        _make_repo(origin)
        _commit_file(origin, "prior.txt", _PLANTED_HOME_PATH, "a path already public on main")
        _git(origin, "checkout", "-b", "feature")
        tip = _commit_file(origin, "feature.txt", "existing feature line\n", "seed feature branch")
        _git(origin, "checkout", "main")
        work, env = _public_work_clone(tmp_path, origin)
        _git(work, "checkout", "feature")
        head = _commit_file(work, "more.txt", "another clean feature line\n", "extend feature")
        _make_the_remote_unlistable(work)

        result = _run_hook(work, env, f"refs/heads/feature {head} refs/heads/feature {tip}\n")

        assert result.returncode == 0, "git reported the remote's tip, so only the new commit is scanned: " + (
            result.stdout + result.stderr
        )

    def test_a_head_the_remote_holds_but_this_clone_never_fetched_is_skipped(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        _make_repo(origin)
        work, env = _public_work_clone(tmp_path, origin)
        _git(origin, "checkout", "-b", "teammate")
        _commit_file(origin, "theirs.txt", "a teammate's line\n", "teammate work")
        _git(work, "checkout", "-b", "feature")
        head = _commit_file(work, "feature.txt", "a clean feature line\n", "add feature")

        result = _run_hook(work, env, f"refs/heads/feature {head} refs/heads/feature {ZERO_SHA}\n")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_commit_the_remote_already_holds_is_not_scanned_again(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        _make_repo(origin)
        _commit_file(origin, "prior.txt", _PLANTED_SECRET, "a leak already public on main")
        work, env = _public_work_clone(tmp_path, origin)
        _git(work, "checkout", "-b", "feature")
        head = _commit_file(work, "feature.txt", "a clean feature line\n", "add feature")

        result = _run_hook(work, env, f"refs/heads/feature {head} refs/heads/feature {ZERO_SHA}\n")

        assert result.returncode == 0, result.stdout + result.stderr


class TestAnExportedRunnerVariableDoesNotRedirectTheHook:
    """prek exports ``PRE_COMMIT_REMOTE_URL``; a pusher can export one too, so the URL git passes wins."""

    def test_a_private_looking_url_in_the_environment_does_not_stand_in_for_the_pushed_remote(
        self, tmp_path: Path
    ) -> None:
        origin = tmp_path / "origin"
        _make_repo(origin)
        work, env = _public_work_clone(tmp_path, origin)
        _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        env["T3_CONFIG_DB"] = str(_seed_private_repos(tmp_path / "config.sqlite3", ["github.com/acme/inner-widget"]))
        env["PRE_COMMIT_REMOTE_URL"] = "https://github.com/acme/inner-widget.git"
        _git(work, "checkout", "-b", "feature")
        leak = _commit_file(work, "leak.txt", _PLANTED_SECRET, "add config")

        result = _run_hook(work, env, f"refs/heads/feature {leak} refs/heads/feature {ZERO_SHA}\n")

        assert result.returncode == 1, result.stdout + result.stderr
        assert "inner-widget" not in result.stdout, "the pushed remote is the one git named"


class TestThePushPathTakesOnlyTheForgesPrivateAnswer:
    """A local file cannot say a remote is private: the visibility cache and ``private_repos`` skip no scan.

    Both are files a pusher can write: the cache directly (no environment change at all), the allowlist in a
    store ``T3_CONFIG_DB`` or ``T3_DATA_DIR`` points anywhere (a copy of the owner's registry passes the registry
    check). With no forge answering, the remote is undetermined and the leak is scanned and refused.
    """

    def _leaking_push(self, tmp_path: Path) -> tuple[Path, dict[str, str], str]:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        _make_unreachable_forge_shims(tmp_path / "offline-bin")
        env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
        (work / "leak.txt").write_text(_PLANTED_SECRET, encoding="utf-8")
        _git(work, "add", "leak.txt")
        _git(work, "commit", "-m", "add config")
        return work, env, _push_stdin(work)

    def test_a_planted_private_cache_entry_skips_no_scan(self, tmp_path: Path) -> None:
        work, env, stdin = self._leaking_push(tmp_path)
        state = Path(env["T3_DATA_DIR"])
        entry = {"ts": time.time(), "visibility": "PRIVATE"}
        (state / "repo-visibility-cache.json").write_text(
            json.dumps({slug_for_remote_url(_FORGE_URL): entry}), encoding="utf-8"
        )

        result = _run_hook(work, env, stdin)

        assert result.returncode == 1, "a cached PRIVATE skipped the scan: " + result.stdout + result.stderr
        assert "privacy" in (result.stdout + result.stderr).lower()

    def test_a_decoy_store_declaring_the_repo_private_skips_no_scan(self, tmp_path: Path) -> None:
        work, env, stdin = self._leaking_push(tmp_path)
        env["T3_CONFIG_DB"] = str(_seed_private_repos(tmp_path / "decoy.sqlite3", ["github.com/acme/widget"]))

        result = _run_hook(work, env, stdin)

        assert result.returncode == 1, "a decoy allowlist skipped the scan: " + result.stdout + result.stderr
        assert "privacy" in (result.stdout + result.stderr).lower()

    def test_the_forges_own_private_answer_still_skips_the_scan(self, tmp_path: Path) -> None:
        """Anti-vacuity: the cache and the allowlist lost their weight; a gate that never skips would fail this."""
        work, env = _clone_with_remote(tmp_path, "PRIVATE")
        (work / "leak.txt").write_text(_PLANTED_SECRET, encoding="utf-8")
        _git(work, "add", "leak.txt")
        _git(work, "commit", "-m", "add config")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr


def _leaking_hook_push(
    tmp_path: Path, *, env_registry: str = _REAL_REGISTRY, committed: str | None = None
) -> tuple[Path, dict[str, str], Path]:
    """A public push whose only commit names a registry term, and the real hook in a committed scratch checkout."""
    work, env = _clone_with_remote(tmp_path, "PUBLIC")
    env["TEATREE_TERM_REGISTRY"] = env_registry
    _make_unreachable_forge_shims(tmp_path / "offline-bin")
    env["PATH"] = f"{tmp_path / 'offline-bin'}{os.pathsep}{env['PATH']}"
    (work / "leak.txt").write_text("a line naming second-leak-term\n", encoding="utf-8")
    _git(work, "add", "leak.txt")
    _git(work, "commit", "-m", "add config")
    fingerprint = CommittedFingerprint(registry=committed) if committed else None
    hook = place_hook(tmp_path / "checkout", registry=env_registry, fingerprint=fingerprint)
    return work, env, hook


_ARGPARSE_THAT_REPORTS_CLEAN = (
    'import os, sys\nsys.stdout.write("Privacy scan: clean (0 findings)\\n")\nsys.stdout.flush()\nos._exit(0)\n'
)
_DATACLASSES_THAT_ANSWERS_PRIVATE = (
    "import os, sys\n"
    'if "--live" in sys.argv:\n'
    '    sys.stdout.write("PRIVATE\\n")\n'
    "    sys.stdout.flush()\n"
    "    os._exit(0)\n"
)


class TestTheHookRunsOnlyCommittedCode:
    """An edit nobody committed is not code the push gate may run: it is the pusher's word that the push is clean.

    The hook runs the scanner, the registry check and the visibility resolver from the working tree it lives in, so
    an uncommitted edit of any of them (a scanner that reports nothing, a check that always verifies, a resolver that
    always answers private) would judge the push. The hook reads the list of those files from HEAD and refuses when
    a listed file differs from its HEAD blob, naming the file and never its content.
    """

    _EVIL = "# EVIL-MARKER-edited-and-never-committed\n"

    def _leaking_push(
        self, tmp_path: Path, *, env_registry: str = _REAL_REGISTRY, committed: str | None = None
    ) -> tuple[Path, dict[str, str], Path]:
        return _leaking_hook_push(tmp_path, env_registry=env_registry, committed=committed)

    @staticmethod
    def _edit(hook: Path, relative: str, appended: str) -> None:
        target = hook.parents[2] / relative
        target.write_text(target.read_text(encoding="utf-8") + appended, encoding="utf-8")

    def test_a_scanner_edited_to_report_nothing_is_refused(self, tmp_path: Path) -> None:
        work, env, hook = self._leaking_push(tmp_path)
        (hook.parents[2] / "scripts" / "privacy_scan.py").write_text(_CLEAN_SCANNER + self._EVIL, encoding="utf-8")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "the edited scanner judged the push: " + result.stdout + result.stderr
        assert "scripts/privacy_scan.py" in result.stdout, result.stdout
        assert "EVIL-MARKER" not in result.stdout + result.stderr, "the refusal names the file, never its content"

    def test_a_registry_check_edited_to_always_verify_is_refused(self, tmp_path: Path) -> None:
        work, env, hook = self._leaking_push(tmp_path, env_registry=_FAKE_REGISTRY, committed=_REAL_REGISTRY)
        self._edit(hook, "src/teatree/hooks/term_fingerprint.py", "\nunverified_gates = lambda registry, path: []\n")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "the edited check verified a fake registry: " + result.stdout + result.stderr
        assert "src/teatree/hooks/term_fingerprint.py" in result.stdout, result.stdout

    def test_a_visibility_resolver_edited_to_answer_private_is_refused(self, tmp_path: Path) -> None:
        work, env, hook = self._leaking_push(tmp_path)
        resolver = hook.parents[2] / "src" / "teatree" / "hooks" / "repo_visibility_cli.py"
        edited = resolver.read_text(encoding="utf-8").replace(
            "    verdict = probe_visibility(slug)\n", '    verdict = "PRIVATE"\n'
        )
        assert edited != resolver.read_text(encoding="utf-8"), "the edit must change the resolver"
        resolver.write_text(edited, encoding="utf-8")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "the edited resolver said private: " + result.stdout + result.stderr
        assert "src/teatree/hooks/repo_visibility_cli.py" in result.stdout, result.stdout

    def test_marking_the_file_assume_unchanged_does_not_hide_the_edit(self, tmp_path: Path) -> None:
        work, env, hook = self._leaking_push(tmp_path)
        root = hook.parents[2]
        _git(root, "update-index", "--assume-unchanged", "scripts/privacy_scan.py")
        (root / "scripts" / "privacy_scan.py").write_text(_CLEAN_SCANNER, encoding="utf-8")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "git was told to ignore the edit: " + result.stdout + result.stderr
        assert "scripts/privacy_scan.py" in result.stdout, result.stdout

    def test_a_list_that_is_not_committed_is_refused(self, tmp_path: Path) -> None:
        work, env, hook = self._leaking_push(tmp_path)
        root = hook.parents[2]
        _git(root, "rm", "-q", "scripts/hooks/leak-gate-sources.txt")
        _git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "drop the list")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "leak-gate-sources.txt" in result.stdout, result.stdout

    def test_a_planted_pyc_for_a_guarded_module_is_not_loaded(self, tmp_path: Path) -> None:
        """The pyc carries the committed source's mtime and size, so Python takes it for the source's own bytecode."""
        work, env, hook = self._leaking_push(tmp_path)
        source = hook.parents[2] / "src" / "teatree" / "hooks" / "term_match.py"
        text = source.read_text(encoding="utf-8") + "\ndef matched_term(*args, **kwargs):\n    return None\n"
        stat = source.stat()
        header = importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, int(stat.st_mtime), stat.st_size)
        pyc = Path(importlib.util.cache_from_source(str(source)))
        pyc.parent.mkdir(parents=True, exist_ok=True)
        pyc.write_bytes(header + marshal.dumps(compile(text, str(source), "exec")))

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "the planted bytecode judged the push: " + result.stdout + result.stderr
        assert "privacy findings" in result.stdout, result.stdout

    @pytest.mark.parametrize("plant", ["pth in the checkout's venv", "sitecustomize beside the sources"])
    def test_code_that_runs_at_interpreter_startup_does_not_answer_for_the_resolver(
        self, tmp_path: Path, plant: str
    ) -> None:
        work, env, hook = self._leaking_push(tmp_path)
        root = hook.parents[2]
        answer = (
            "import os, sys\n"
            'if "--live" in sys.argv:\n'
            '    sys.stdout.write("PRIVATE\\n")\n'
            "    sys.stdout.flush()\n"
            "    os._exit(0)\n"
        )
        if plant.startswith("pth"):
            (root / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\nrequires-python = ">=3.13"\n')
            subprocess.run([str(shutil.which("uv")), "venv", "--python", "3.13", "-q", str(root / ".venv")], check=True)
            site = next((root / ".venv" / "lib").glob("python3*/site-packages"))
            (site / "evil.pth").write_text(f"import os, sys; exec({answer!r})\n", encoding="utf-8")
        else:
            (root / "src" / "sitecustomize.py").write_text(answer, encoding="utf-8")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "startup code answered private: " + result.stdout + result.stderr
        assert "privacy findings" in result.stdout, result.stdout

    @pytest.mark.parametrize("suffix", [importlib.machinery.EXTENSION_SUFFIXES[0], ".pyd"], ids=["so", "pyd"])
    def test_an_untracked_extension_module_beside_a_guarded_module_is_refused(
        self, tmp_path: Path, suffix: str
    ) -> None:
        """Python imports ``term_match<suffix>`` before ``term_match.py``, and nothing above hashes it."""
        work, env, hook = self._leaking_push(tmp_path)
        shim = hook.parents[2] / "src" / "teatree" / "hooks" / f"term_match{suffix}"
        shim.write_bytes(b"not a real extension module")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "shadowed" in result.stdout, result.stdout
        assert f"src/teatree/hooks/term_match{suffix}" in result.stdout, result.stdout

    def test_an_untracked_package_named_like_a_guarded_module_is_refused(self, tmp_path: Path) -> None:
        """A package directory is imported before a same-named ``.py`` module: here a matcher that finds nothing."""
        work, env, hook = self._leaking_push(tmp_path)
        package = hook.parents[2] / "src" / "teatree" / "hooks" / "term_match"
        package.mkdir()
        (package / "__init__.py").write_text(
            "def __getattr__(name):\n    return lambda *args, **kwargs: None\n", encoding="utf-8"
        )

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "the shadowing package judged the push: " + result.stdout + result.stderr
        assert "shadowed" in result.stdout, result.stdout
        assert "src/teatree/hooks/term_match/" in result.stdout, result.stdout

    @pytest.mark.parametrize("where", ["scripts", "src"])
    def test_an_untracked_module_named_like_a_standard_library_one_is_not_imported(
        self, tmp_path: Path, where: str
    ) -> None:
        """The scanner's own directory and ``PYTHONPATH`` come first on ``sys.path``, ahead of the standard library."""
        work, env, hook = self._leaking_push(tmp_path)
        (hook.parents[2] / where / "argparse.py").write_text(_ARGPARSE_THAT_REPORTS_CLEAN, encoding="utf-8")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, f"{where}/argparse.py judged the push: " + result.stdout + result.stderr
        assert "privacy findings" in result.stdout, result.stdout

    def test_a_standard_library_named_module_in_the_repo_the_hook_runs_in_is_not_imported(self, tmp_path: Path) -> None:
        """``python -m`` puts the working directory first on ``sys.path``, and a hook runs in the repo being pushed."""
        work, env, hook = self._leaking_push(tmp_path)
        (work / "dataclasses.py").write_text(_DATACLASSES_THAT_ANSWERS_PRIVATE, encoding="utf-8")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, "dataclasses.py answered private: " + result.stdout + result.stderr
        assert "privacy findings" in result.stdout, result.stdout

    def test_committed_sources_run_as_before(self, tmp_path: Path) -> None:
        """Anti-vacuity: with nothing edited the same push is judged by the real scanner and refused as a leak."""
        work, env, hook = self._leaking_push(tmp_path)

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "privacy findings" in result.stdout


class TestAVendoringCheckoutIsJudgedByItsVendoredCopy:
    @staticmethod
    def _checkout(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        place_hook(tmp_path / "origin" / "vendor" / "x", registry=_REAL_REGISTRY, repository=tmp_path / "origin")
        _git(work, "pull", "--ff-only", "origin", "main")
        env["TEATREE_TERM_REGISTRY"] = _REAL_REGISTRY
        return work, env, work / "vendor" / "x" / "scripts" / "hooks" / HOOK.name

    def test_an_own_checkout_disguised_by_a_parent_worktree_is_still_refused(self, tmp_path: Path) -> None:
        work, env, hook = _leaking_hook_push(tmp_path, env_registry=_FAKE_REGISTRY, committed=_REAL_REGISTRY)
        _git(hook.parents[2], "config", "core.worktree", str(tmp_path))

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "leak-gate-sources.txt" in result.stdout, result.stdout

    @pytest.mark.parametrize("run_from", [".", "vendor/x"], ids=["git-top", "prek-nested"])
    def test_a_clean_push_passes(self, tmp_path: Path, run_from: str) -> None:
        work, env, hook = self._checkout(tmp_path)
        (work / "feature.txt").write_text("clean feature\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")

        result = _run_hook(work / run_from, env, _push_stdin(work), hook=hook)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "cannot be checked" not in result.stdout

    def test_a_host_registry_that_differs_from_the_vendored_fingerprint_passes_a_clean_push(
        self, tmp_path: Path
    ) -> None:
        work, env, hook = self._checkout(tmp_path)
        env["TEATREE_TERM_REGISTRY"] = _FAKE_REGISTRY
        (work / "feature.txt").write_text("clean feature\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_host_registry_that_differs_from_the_vendored_fingerprint_still_refuses_its_own_term(
        self, tmp_path: Path
    ) -> None:
        work, env, hook = self._checkout(tmp_path)
        env["TEATREE_TERM_REGISTRY"] = _FAKE_REGISTRY
        (work / "feature.txt").write_text("fake-picked-term\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "carries privacy findings" in result.stdout
        assert "leak#0" in result.stdout
        assert "does not match" not in result.stdout

    def test_a_vendoring_host_without_a_registry_is_refused_naming_the_missing_registry(self, tmp_path: Path) -> None:
        work, env, hook = self._checkout(tmp_path)
        env.pop("TEATREE_TERM_REGISTRY")
        env["T3_CONFIG_DB"] = str(_seed_private_repos(tmp_path / "config.sqlite3", []))
        (work / "feature.txt").write_text("clean feature\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "no term registry could be loaded" in result.stdout
        assert "banned_term_registry" in result.stdout
        assert "TEATREE_TERM_REGISTRY" in result.stdout
        assert "term-source-fingerprint" not in result.stdout

    def test_a_vendored_push_to_a_private_remote_needs_no_registry(self, tmp_path: Path) -> None:
        work, env, hook = self._checkout(tmp_path)
        _make_gh_shim(tmp_path / "bin", "PRIVATE")
        env.pop("TEATREE_TERM_REGISTRY")
        env["T3_CONFIG_DB"] = str(_seed_private_repos(tmp_path / "config.sqlite3", []))
        (work / "feature.txt").write_text("clean feature\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("run_from", [".", "vendor/x"], ids=["git-top", "prek-nested"])
    @pytest.mark.parametrize("path", ["vendor/x/leak.txt", "outside.txt"])
    def test_a_leak_is_refused_and_named_wherever_it_sits(self, tmp_path: Path, run_from: str, path: str) -> None:
        work, env, hook = self._checkout(tmp_path)
        target = work / path
        target.write_text("demo-leak-term\n", encoding="utf-8")
        _git(work, "add", path)
        _git(work, "commit", "-m", "add file")

        result = _run_hook(work / run_from, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "carries privacy findings" in result.stdout, result.stdout
        assert f"file {path}:" in result.stdout, result.stdout

    def test_diff_relative_cannot_hide_a_leak_outside_the_vendor_directory(self, tmp_path: Path) -> None:
        work, env, hook = self._checkout(tmp_path)
        (work / "outside.txt").write_text("demo-leak-term\n", encoding="utf-8")
        _git(work, "add", "outside.txt")
        _git(work, "commit", "-m", "add file")
        _git(work, "config", "diff.relative", "true")
        assert "demo-leak-term" not in _git_stdout(work / "vendor" / "x", "log", "-1", "--format=", "--patch")

        result = _run_hook(work / "vendor" / "x", env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "carries privacy findings" in result.stdout, result.stdout
        assert "file outside.txt:" in result.stdout, result.stdout

    def test_an_uncommitted_vendored_scanner_edit_is_named(self, tmp_path: Path) -> None:
        work, env, hook = self._checkout(tmp_path)
        scanner = work / "vendor" / "x" / "scripts" / "privacy_scan.py"
        scanner.write_text(scanner.read_text(encoding="utf-8") + "\nchanged = True\n", encoding="utf-8")
        (work / "feature.txt").write_text("clean feature\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")

        result = _run_hook(work / "vendor" / "x", env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "cannot be checked: 1 file(s)" in result.stdout, result.stdout
        assert "scripts/privacy_scan.py" in result.stdout, result.stdout

    def test_same_named_files_at_the_repository_top_do_not_replace_the_vendored_gate(self, tmp_path: Path) -> None:
        work, env, hook = self._checkout(tmp_path)
        (work / "scripts" / "hooks").mkdir(parents=True)
        (work / "scripts" / "hooks" / "leak-gate-sources.txt").write_text("scripts/privacy_scan.py\n", encoding="utf-8")
        (work / "scripts" / "privacy_scan.py").write_text(_CLEAN_SCANNER, encoding="utf-8")
        (work / "outside.txt").write_text("demo-leak-term\n", encoding="utf-8")
        _git(work, "add", "scripts", "outside.txt")
        _git(work, "commit", "-m", "add files")

        result = _run_hook(work, env, _push_stdin(work), hook=hook)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "carries privacy findings" in result.stdout, result.stdout
        assert "file outside.txt:" in result.stdout, result.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
