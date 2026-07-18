# test-path: cross-cutting — drives deploy/entrypoint.sh + teatree.core.gates.gh_token_preflight (no src mirror).
"""Integration tests for the deploy entrypoint's GitHub token-permission preflight (#3405).

`deploy/entrypoint.sh`'s ``assert_gh_token_permissions`` verifies TEATREE_GH_TOKEN
carries the WRITE permissions the loop mutates (issues / pull_requests / contents)
rather than only that it authenticates — otherwise a read-only token fails LATE,
mid-run, with "Resource not accessible by personal access token".

Per the Test-Writing Doctrine these run the REAL shell functions (extracted
verbatim from the entrypoint) in a bash subprocess with a stub `gh` on PATH that
models GitHub's route-level 403 for a denied write permission and 404 for a
permitted one. The stub's per-endpoint verdict is env-driven so one shim covers
every case. A companion assertion pins the shell's permission labels to the
Python mirror (`teatree.core.gates.gh_token_preflight`) so the two implementations of
the same contract cannot drift.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from teatree.core.gates.gh_token_preflight import REQUIRED_PERMISSION_LABELS

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="needs bash (present in the deploy image and CI)",
)

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"
_BASH = shutil.which("bash") or "bash"
_NOT_ACCESSIBLE = "Resource not accessible by personal access token"


def _extract_shell_function(name: str) -> str:
    """Return the verbatim source of shell function *name* from the entrypoint."""
    body: list[str] = []
    capturing = False
    for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}() {{"):
            capturing = True
        if capturing:
            body.append(line)
            if line == "}":
                return "\n".join(body)
    not_found = f"function {name!r} not found in {ENTRYPOINT}"
    raise AssertionError(not_found)


def _write_gh_stub(bin_dir: Path) -> None:
    """A `gh` shim modelling GitHub's write-permission responses.

    ``GH_META_FAIL`` makes the metadata read (``gh api repos/<slug>``) fail with
    the given text. ``GH_DENY`` is a space-separated set of endpoint substrings
    for which a write probe returns the 403 "Resource not accessible" body;
    every other write probe returns a 404 (permitted-but-nonexistent). Exit code
    is non-zero for any 4xx, exactly as real `gh api` behaves.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "gh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'args="$*"\n'
        # metadata read: `gh api repos/<slug>` with no --method
        'if [[ "$args" != *"--method"* && "$args" == *"api repos/"* ]]; then\n'
        '  if [ -n "${GH_META_FAIL:-}" ]; then echo "$GH_META_FAIL" >&2; exit 1; fi\n'
        '  echo "{}"; exit 0\n'
        "fi\n"
        # write probes: deny listed endpoints, else 404
        "for needle in ${GH_DENY:-}; do\n"
        '  if [[ "$args" == *"$needle"* ]]; then echo "' + _NOT_ACCESSIBLE + '" >&2; exit 1; fi\n'
        "done\n"
        'echo "{\\"message\\":\\"Not Found\\"}" >&2; exit 1\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(tmp_path: Path, **stub_env: str) -> subprocess.CompletedProcess[str]:
    """Run the extracted preflight once with the stub `gh` on PATH."""
    bin_dir = tmp_path / "bin"
    _write_gh_stub(bin_dir)

    func = "\n\n".join(
        _extract_shell_function(name) for name in ("gh_repo_slug", "_gh_perm_denied", "assert_gh_token_permissions")
    )
    harness = tmp_path / "harness.sh"
    harness.write_text(
        f'set -euo pipefail\nREPO_URL="${{REPO_URL:-}}"\n{func}\nassert_gh_token_permissions\n',
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.setdefault("TEATREE_REPO_URL", "https://github.com/souliane/teatree.git")
    env.update(stub_env)
    return subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=False, env=env)


class TestAssertGhTokenPermissions:
    def test_all_permissions_present_passes(self, tmp_path: Path) -> None:
        out = _run(tmp_path, GH_DENY="")
        assert out.returncode == 0, out.stderr
        assert "permissions verified" in out.stdout

    def test_missing_issues_write_fails_loud(self, tmp_path: Path) -> None:
        out = _run(tmp_path, GH_DENY="issues/0")
        assert out.returncode != 0
        assert "issues: write" in out.stderr
        assert "Resource not accessible" in out.stderr

    def test_all_writes_missing_lists_each(self, tmp_path: Path) -> None:
        out = _run(tmp_path, GH_DENY="issues/0 pulls/0 refs/heads")
        assert out.returncode != 0
        for label in ("issues: write", "pull_requests: write", "contents: write"):
            assert label in out.stderr

    def test_metadata_unreadable_fails_loud(self, tmp_path: Path) -> None:
        out = _run(tmp_path, GH_META_FAIL="Not Found")
        assert out.returncode != 0
        assert "metadata: read" in out.stderr

    def test_unparseable_slug_skips_gracefully(self, tmp_path: Path) -> None:
        out = _run(tmp_path, TEATREE_REPO_URL="file:///local/mirror", GH_DENY="issues/0")
        # No GitHub slug → skip the probe, do not fail the deploy.
        assert out.returncode == 0, out.stderr
        assert "could not resolve the GitHub repo slug" in out.stderr


class TestGhRepoSlug:
    def _slug(self, tmp_path: Path, url: str) -> str:
        func = _extract_shell_function("gh_repo_slug")
        harness = tmp_path / "slug.sh"
        harness.write_text(f'set -euo pipefail\nREPO_URL=""\n{func}\ngh_repo_slug\n', encoding="utf-8")
        env = dict(os.environ)
        env["TEATREE_REPO_URL"] = url
        return subprocess.run(
            [_BASH, str(harness)], capture_output=True, text=True, check=False, env=env
        ).stdout.strip()

    def test_https_url(self, tmp_path: Path) -> None:
        assert self._slug(tmp_path, "https://github.com/souliane/teatree.git") == "souliane/teatree"

    def test_ssh_url(self, tmp_path: Path) -> None:
        assert self._slug(tmp_path, "git@github.com:souliane/teatree.git") == "souliane/teatree"

    def test_non_github_url_is_empty(self, tmp_path: Path) -> None:
        assert self._slug(tmp_path, "https://gitlab.com/x/y.git") == ""


def test_shell_labels_match_python_mirror() -> None:
    """The shell probe and the Python mirror must name the same four permissions."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    for label in REQUIRED_PERMISSION_LABELS:
        assert label in text, f"entrypoint.sh must probe {label!r} (pinned to REQUIRED_PERMISSION_LABELS)"
