# test-path: cross-cutting — drives deploy/entrypoint.sh + teatree.core.gates.gh_token_preflight (no src mirror).
"""Integration tests for the deploy entrypoint's GitHub token-permission preflight (#3405/#3477).

`deploy/entrypoint.sh`'s ``assert_gh_token_permissions`` verifies TEATREE_GH_TOKEN
carries the permissions the loop uses — a REQUIRED subset (metadata read, issues/
pull_requests/contents write) that ``exit 1``s the deploy when missing, and a
RECOMMENDED subset (workflows write, actions read/write, checks/statuses read,
projects read) that only WARNs — otherwise a read-only token fails LATE, mid-run,
with "Resource not accessible by personal access token", or an optional feature
(CI trigger/status, auto-merge, board sync) fails silently later.

Per the Test-Writing Doctrine these run the REAL shell functions (extracted
verbatim from the entrypoint) in a bash subprocess with a stub `gh` on PATH that
models GitHub's route-level 403 for a denied permission and 404/200 for a
permitted one. The stub's per-endpoint verdict is env-driven so one shim covers
every case. A companion assertion pins the shell's permission labels (both
tiers) to the Python mirror (`teatree.core.gates.gh_token_preflight`) so the two
implementations of the same contract cannot drift, and a second pins that the
entrypoint's `exit 1` paths reference ONLY required-tier labels — the
never-lockout invariant.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from teatree.core.gates.gh_token_preflight import RECOMMENDED_PERMISSION_LABELS, REQUIRED_PERMISSION_LABELS

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="needs bash (present in the deploy image and CI)",
)

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"
_BASH = shutil.which("bash") or "bash"
_NOT_ACCESSIBLE = "Resource not accessible by personal access token"

_SHELL_FUNCTIONS: tuple[str, ...] = (
    "gh_repo_slug",
    "_gh_metadata_denied",
    "_gh_probe_denied",
    "_gh_default_branch",
    "assert_gh_token_permissions",
)
_VAR_ASSIGNMENTS: tuple[str, ...] = ("_GH_CLASSIC_TOKEN_URL", "_GH_FINE_GRAINED_TOKENS_URL")


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


def _extract_var_line(name: str) -> str:
    """Return the verbatim ``NAME="..."`` top-level assignment line for *name*."""
    for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line
    not_found = f"variable {name!r} not found in {ENTRYPOINT}"
    raise AssertionError(not_found)


def _write_gh_stub(bin_dir: Path) -> None:
    """A `gh` shim modelling GitHub's permission-probe responses (write and read alike).

    ``GH_META_FAIL`` makes the metadata read (``gh api -i repos/<slug>``) fail
    with the given text. ``GH_OAUTH_SCOPES`` (when SET, even empty) makes the
    metadata read emit an ``X-OAuth-Scopes`` header with that value — modelling a
    CLASSIC PAT; leaving it unset models a fine-grained PAT (no such header). The
    metadata body always carries ``default_branch`` (``GH_DEFAULT_BRANCH``,
    default ``main``) after a blank line, mirroring the real ``-i`` response shape
    the ``checks: read`` / ``statuses: read`` probes parse it from.

    ``GH_DENY`` is a space-separated set of endpoint substrings for which ANY
    probe (write or read) returns the 403 "Resource not accessible" body; every
    other probe returns a 404 (permitted-but-nonexistent/harmless). The metadata
    call is identified by its ``-i`` flag (second positional arg), never by
    absence of ``--method`` — several RECOMMENDED probes are plain reads with no
    ``--method`` too and must not be misread as the metadata call.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "gh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'args="$*"\n'
        # metadata read: `gh api -i repos/<slug>` — identified by -i as $2 ($1=api)
        'if [ "$2" = "-i" ]; then\n'
        '  if [ -n "${GH_META_FAIL:-}" ]; then echo "$GH_META_FAIL" >&2; exit 1; fi\n'
        '  echo "HTTP/2.0 200 OK"\n'
        '  if [ -n "${GH_OAUTH_SCOPES+x}" ]; then echo "X-OAuth-Scopes: $GH_OAUTH_SCOPES"; fi\n'
        '  echo ""\n'
        '  echo "{\\"default_branch\\":\\"${GH_DEFAULT_BRANCH:-main}\\"}"\n'
        "  exit 0\n"
        "fi\n"
        # every other probe (write or read): deny listed endpoints, else 404
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

    var_lines = "\n".join(_extract_var_line(name) for name in _VAR_ASSIGNMENTS)
    func = "\n\n".join(_extract_shell_function(name) for name in _SHELL_FUNCTIONS)
    harness = tmp_path / "harness.sh"
    harness.write_text(
        f'set -euo pipefail\nREPO_URL="${{REPO_URL:-}}"\n{var_lines}\n{func}\nassert_gh_token_permissions\n',
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.setdefault("TEATREE_REPO_URL", "https://github.com/souliane/teatree.git")
    # Retries must not sleep in the transient-failure test.
    env.setdefault("TEATREE_GH_PREFLIGHT_BACKOFF_SECONDS", "0")
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


class TestRecommendedPermissionsWarnOnly:
    """A missing RECOMMENDED permission WARNs with remediation but never exits (#3477)."""

    def test_single_recommended_gap_warns_and_exits_zero(self, tmp_path: Path) -> None:
        out = _run(tmp_path, GH_DENY="dispatches")
        assert out.returncode == 0, out.stderr
        assert "WARN" in out.stderr
        assert "actions: write" in out.stderr
        assert "permissions verified" in out.stdout

    def test_every_probed_recommended_gap_warns_each(self, tmp_path: Path) -> None:
        out = _run(tmp_path, GH_DENY="dispatches artifacts check-runs main/status")
        assert out.returncode == 0, out.stderr
        for label in ("actions: write", "actions: read", "checks: read", "statuses: read"):
            assert label in out.stderr

    def test_workflows_write_always_warned_unprobed_for_fine_grained(self, tmp_path: Path) -> None:
        # Never actively probed (ambiguous 403-vs-404 ordering) — always listed
        # so the operator verifies it manually, even when every OTHER probe passes.
        out = _run(tmp_path, GH_DENY="")
        assert out.returncode == 0, out.stderr
        assert "workflows: write" in out.stderr
        assert "verify" in out.stderr.lower() or "manually" in out.stderr.lower() or "recreate" in out.stderr.lower()

    def test_required_missing_still_fails_even_with_recommended_gaps(self, tmp_path: Path) -> None:
        out = _run(tmp_path, GH_DENY="issues/0 dispatches")
        assert out.returncode != 0
        assert "issues: write" in out.stderr

    def test_fine_grained_remediation_names_fine_grained_tokens_url(self, tmp_path: Path) -> None:
        out = _run(tmp_path, GH_DENY="dispatches")
        assert "https://github.com/settings/personal-access-tokens" in out.stderr


class TestClassicPatScope:
    """A classic PAT is judged by its ``X-OAuth-Scopes`` header (#3436/#3477).

    The per-route 403 probe fails OPEN for a classic token, so the header's
    scope membership — not any probe — decides every verdict, both tiers.
    """

    def test_classic_pat_with_repo_scope_passes(self, tmp_path: Path) -> None:
        # A classic token WITHOUT `repo` would 404 every write probe (fail open);
        # the passing verdict must come from the scope header, and DENY is ignored.
        out = _run(
            tmp_path,
            GH_OAUTH_SCOPES="repo, workflow, read:project",
            GH_DENY="issues/0 pulls/0 refs/heads dispatches artifacts",
        )
        assert out.returncode == 0, out.stderr
        assert "classic PAT with 'repo' scope" in out.stdout
        assert "WARN" not in out.stderr

    def test_classic_pat_without_repo_scope_fails_loud(self, tmp_path: Path) -> None:
        out = _run(tmp_path, GH_OAUTH_SCOPES="public_repo, read:org", GH_DENY="")
        assert out.returncode != 0
        assert "classic PAT WITHOUT the 'repo' scope" in out.stderr

    def test_classic_repo_status_scope_is_not_repo_write(self, tmp_path: Path) -> None:
        out = _run(tmp_path, GH_OAUTH_SCOPES="repo:status, gist", GH_DENY="")
        assert out.returncode != 0
        assert "classic PAT WITHOUT the 'repo' scope" in out.stderr

    def test_classic_pat_with_repo_but_no_workflow_or_project_scope_warns(self, tmp_path: Path) -> None:
        out = _run(tmp_path, GH_OAUTH_SCOPES="repo, read:org", GH_DENY="")
        assert out.returncode == 0, out.stderr
        assert "WARN" in out.stderr
        assert "workflows: write" in out.stderr
        assert "projects: read" in out.stderr
        assert "https://github.com/settings/tokens/new" in out.stderr


class TestTransientProbeFailure:
    """A transient metadata-read fault must NOT crash-loop init (#3436)."""

    def test_transient_metadata_failure_warns_and_continues(self, tmp_path: Path) -> None:
        # A network-shaped failure carries no denial signal — retry, then warn and
        # skip the write preflight rather than exit 1 (which would crash-loop init).
        out = _run(tmp_path, GH_META_FAIL="could not resolve host: api.github.com")
        assert out.returncode == 0, out.stderr
        assert "SKIPPING the write-permission preflight" in out.stderr
        assert "indeterminate" in out.stderr


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
    """The shell probe and the Python mirror must name the same permissions, both tiers."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    for label in (*REQUIRED_PERMISSION_LABELS, *RECOMMENDED_PERMISSION_LABELS):
        assert label in text, f"entrypoint.sh must reference {label!r} (pinned to the Python label tuples)"


def test_entrypoint_exit1_paths_reference_only_required_labels() -> None:
    """Every ``exit 1`` in the token preflight must name ONLY required-tier labels.

    The never-lockout invariant: a RECOMMENDED-tier gap must never be able to
    fail the deploy, even by accidentally appearing in an exit-1 error message.
    """
    func_lines = _extract_shell_function("assert_gh_token_permissions").splitlines()
    for i, line in enumerate(func_lines):
        if "exit 1" not in line:
            continue
        window = "\n".join(func_lines[max(0, i - 4) : i + 1])
        for label in RECOMMENDED_PERMISSION_LABELS:
            assert label not in window, f"exit 1 path must not reference recommended label {label!r}:\n{window}"
