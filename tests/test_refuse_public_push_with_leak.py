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
import sys
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


# A GitHub-noreply identity so the shared fixtures exercise only the
# leak-scan dimension of the gate, not the #730 author-identity guard
# (which has its own dedicated test class). The login form keeps these
# commits passing the noreply pattern just like a real loop commit.
_NOREPLY_EMAIL = "21343492+souliane@users.noreply.github.com"
_NOREPLY_NAME = "souliane"


def _make_repo(path: Path, branch: str = "main") -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-b", branch)
    _git(path, "config", "user.email", _NOREPLY_EMAIL)
    _git(path, "config", "user.name", _NOREPLY_NAME)
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
    _git(work, "config", "user.email", _NOREPLY_EMAIL)
    _git(work, "config", "user.name", _NOREPLY_NAME)
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


def _run_hook(
    cwd: Path,
    env: dict[str, str],
    stdin: str,
    remote_url: str = "https://github.com/acme/widget.git",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        # `bash <hook>` on PATH is git's real pre-push invocation shape; test-only driver.
        ["bash", str(HOOK), "origin", remote_url],  # noqa: S607 — bash resolved via PATH is the hook's real invocation
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
        # Keep system bins (bash/git) but drop the gh shim dir so `gh`
        # is genuinely unavailable. Pin the scan command to this venv's
        # interpreter by absolute path so the scanner still runs (a bare
        # `python3` on the stripped PATH would miss the test deps and
        # crash the scanner, masking the leak behind its fail-open path).
        env["PATH"] = "/usr/bin:/bin"
        env["T3_PRIVACY_SCAN_CMD"] = f"{sys.executable} {SCAN}"
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
        env["PATH"] = "/usr/bin:/bin"
        env["T3_PRIVACY_SCAN_CMD"] = f"{sys.executable} {SCAN}"
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
        env["T3_BANNED_TERMS"] = "democorp"
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

        Same public remote and the SAME ``T3_BANNED_TERMS`` config, but a clean
        message with no configured term still passes — proving the block above
        measures the term, not the mere presence of the banned-terms config.
        """
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        env["T3_BANNED_TERMS"] = "democorp"
        (work / "feature.txt").write_text("a perfectly clean feature line\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "feat: onboard the new customer pipeline")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_hook_is_executable(self) -> None:
        assert os.access(HOOK, os.X_OK), f"{HOOK} must be chmod +x"


class TestLeakGateFailsOpenOnScannerCrash:
    """The gate blocks on a genuine FINDING, never on a scanner CRASH (#126 gap 3).

    The pre-push gate previously treated ANY non-zero scan exit as a finding
    and BLOCKED. So a scanner crash (missing script, import error, argparse
    usage error) wedged every push closed with no recourse — an over-deny
    lockout. The fix reserves a dedicated findings exit code and makes the
    gate block ONLY on that code, failing OPEN (allow) on any other non-zero.
    """

    def _crashing_scan_shim(self, bin_dir: Path, exit_code: int) -> str:
        """Write a fake scan command that always exits ``exit_code`` (never the findings code)."""
        bin_dir.mkdir(parents=True, exist_ok=True)
        shim = bin_dir / "fake-scan"
        shim.write_text(
            f'#!/usr/bin/env bash\necho "scanner blew up" >&2\nexit {exit_code}\n',
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(shim)

    def test_scanner_crash_fails_open(self, tmp_path: Path) -> None:
        """A non-findings, non-zero scan exit (crash) must ALLOW the push."""
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        # Point the gate at a scan command that crashes with code 1 (generic
        # exception) — distinct from the dedicated findings code.
        shim = self._crashing_scan_shim(tmp_path / "scanbin", exit_code=1)
        env["T3_PRIVACY_SCAN_CMD"] = shim
        (work / "feature.txt").write_text("a clean feature line\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, "scanner crash must fail OPEN: " + result.stdout + result.stderr

    def test_scanner_usage_error_fails_open(self, tmp_path: Path) -> None:
        """An argparse/usage-error exit (2) is also a crash, not a finding → ALLOW."""
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        shim = self._crashing_scan_shim(tmp_path / "scanbin", exit_code=2)
        env["T3_PRIVACY_SCAN_CMD"] = shim
        (work / "feature.txt").write_text("a clean feature line\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add feature")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, "scanner usage error must fail OPEN: " + result.stdout + result.stderr

    def test_genuine_finding_still_blocks(self, tmp_path: Path) -> None:
        """The real scanner reports a finding on the dedicated code → still BLOCK.

        Uses the real privacy_scan.py (not a shim) so the dedicated findings
        exit code path is exercised end-to-end — the gate must block on it.
        """
        work, env = _clone_with_remote(tmp_path, "PUBLIC")  # env already points at real privacy_scan.py
        (work / "leak.txt").write_text(
            "token = glpat-XXXXXXXXXXXXXXXX\n",
            encoding="utf-8",
        )
        _git(work, "add", "leak.txt")
        _git(work, "commit", "-m", "add config")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, "genuine finding must still block: " + result.stdout + result.stderr


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
    _git(work, "remote", "set-url", "origin", "https://github.com/acme/widget.git")
    bin_dir = tmp_path / "bin"
    _make_gh_shim(bin_dir, "PUBLIC")
    env = _hermetic_env()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["T3_PRIVACY_SCAN_CMD"] = f"{sys.executable} {SCAN}"
    return work, env


_REMOTE_URL = "https://github.com/acme/widget.git"


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
        ["bash", str(HOOK), "origin", _REMOTE_URL],  # noqa: S607 — bash on PATH is the hook's real invocation
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
