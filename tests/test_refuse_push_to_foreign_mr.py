"""Integration tests for the foreign-MR pre-push guard (#2211).

The gate refuses ``git push`` when the branch being pushed backs an
**open** MR/PR whose author is NOT the configured user identity — a
teammate's open MR. Pushing to it silently modifies their MR; the gate
blocks that and names the author. An explicit override token
(``[push-to-foreign-mr-ok: <reason>]``) in the push range's commit
messages lets a genuine co-authoring push through.

ALLOW cases: our own branch / our own open MR, a branch with no MR, a
branch whose only MR is closed or merged (only OPEN foreign MRs are
protected), the override token present, and a forge-API failure
(fail-open — a transient ``gh`` error must never brick a legitimate
push).

These are integration tests in the spirit of the Test-Writing Doctrine:
a real ``git init`` repo under ``tmp_path``, a real ``gh`` shim on
``PATH`` returning a fixed login + PR payload, and a real hook
invocation. Only ``gh`` (the unstoppable forge network) is faked.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "refuse-push-to-foreign-mr.sh"

_OUR_LOGIN = "souliane"
_NOREPLY_EMAIL = "21343492+souliane@users.noreply.github.com"
_NOREPLY_NAME = "souliane"


def _hermetic_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607 — git from PATH is what the hook under test itself runs
        cwd=cwd,
        check=True,
        capture_output=True,
        env=_hermetic_env(),
    )


def _make_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", _NOREPLY_EMAIL)
    _git(path, "config", "user.name", _NOREPLY_NAME)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "init")


def _make_gh_shim(bin_dir: Path, *, login: str, pr_payload: list[dict[str, object]]) -> None:
    r"""Write a fake ``gh`` answering ``api user`` and ``pr list``.

    ``api user --jq .login`` → the configured login.
    ``pr list --head <branch> --state open --json number,author --jq
    '.[] | "\(.number)\t\(.author.login)"'`` → the hook formats the
    payload via ``gh``'s built-in jq into ``<number>\t<login>`` rows. The
    shim reproduces exactly that: it filters ``pr_payload`` to the
    requested ``--head`` branch and emits the tab-separated rows, faithful
    to real ``gh`` output for that command.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    payload_json = json.dumps(pr_payload)
    shim = bin_dir / "gh"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"login = {login!r}\n"
        f"payload = json.loads({payload_json!r})\n"
        "args = sys.argv[1:]\n"
        'if "api" in args and "user" in args:\n'
        "    print(login)\n"
        "    sys.exit(0)\n"
        'if "pr" in args and "list" in args:\n'
        '    head = args[args.index("--head") + 1] if "--head" in args else None\n'
        "    rows = [pr for pr in payload if head is None or pr.get('headRefName') == head]\n"
        "    for pr in rows:\n"
        "        print(f\"{pr['number']}\\t{pr['author']['login']}\")\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _failing_gh_shim(bin_dir: Path) -> None:
    """Write a ``gh`` whose ``pr list`` always fails (transient API error)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "gh"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"login = {_OUR_LOGIN!r}\n"
        "args = sys.argv[1:]\n"
        'if "api" in args and "user" in args:\n'
        "    print(login)\n"
        "    sys.exit(0)\n"
        'if "pr" in args and "list" in args:\n'
        '    sys.stderr.write("gh: API error\\n")\n'
        "    sys.exit(1)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _setup(
    tmp_path: Path,
    *,
    branch: str = "feature-x",
    login: str = _OUR_LOGIN,
    pr_payload: list[dict[str, object]] | None = None,
    failing_gh: bool = False,
) -> tuple[Path, dict[str, str]]:
    origin = tmp_path / "origin"
    _make_repo(origin)
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.email", _NOREPLY_EMAIL)
    _git(work, "config", "user.name", _NOREPLY_NAME)
    _git(work, "checkout", "-b", branch)
    _git(work, "remote", "set-url", "origin", "https://github.com/acme/widget.git")

    bin_dir = tmp_path / "bin"
    if failing_gh:
        _failing_gh_shim(bin_dir)
    else:
        _make_gh_shim(bin_dir, login=login, pr_payload=pr_payload or [])
    env = _hermetic_env()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return work, env


def _commit(work: Path, filename: str, body: str, message: str = "add feature") -> None:
    (work / filename).write_text(body, encoding="utf-8")
    _git(work, "add", filename)
    _git(work, "commit", "-m", message)


def _commit_with_large_body(work: Path, filename: str, ordinal: int, body_bytes: int) -> None:
    """Commit a file with a multi-hundred-KB commit-message body.

    The cumulative ``git log --format='%B'`` output across these commits is
    the producer that must outrun ``grep -q``'s early close — large bodies
    let a handful of commits fill the OS pipe buffer.
    """
    (work / filename).write_text(f"line {ordinal}\n", encoding="utf-8")
    _git(work, "add", filename)
    filler = ("lorem ipsum dolor sit amet " * 40 + "\n") * (body_bytes // 1080 + 1)
    # Pass the large body via a message FILE, not a -m argument: a
    # hundreds-of-KB commit message on the command line blows past Linux's
    # ARG_MAX (E2BIG / "Argument list too long").
    msg_file = work / f".commit-msg-{ordinal}"
    msg_file.write_text(f"bulk commit {ordinal}\n\n{filler}", encoding="utf-8")
    _git(work, "commit", "-F", str(msg_file))
    msg_file.unlink()


def _git_log_body_bytes(work: Path) -> int:
    """Byte length of ``git log --format='%B' HEAD`` (the producer stream).

    Its size determines whether ``grep -q``'s early close triggers SIGPIPE.
    """
    out = subprocess.run(
        ["git", "log", "--format=%B", "HEAD"],  # noqa: S607 — git from PATH is what the hook under test itself runs
        cwd=work,
        capture_output=True,
        check=True,
        env=_hermetic_env(),
    ).stdout
    return len(out)


def _rev_parse(work: Path, rev: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", rev],  # noqa: S607 — git from PATH is what the hook under test itself runs
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
        env=_hermetic_env(),
    ).stdout.strip()


def _run_hook(
    work: Path,
    env: dict[str, str],
    branch: str,
    remote_sha: str = "0000000000000000000000000000000000000000",
) -> subprocess.CompletedProcess[str]:
    sha = _rev_parse(work, "HEAD")
    stdin = f"refs/heads/{branch} {sha} refs/heads/{branch} {remote_sha}\n"
    return subprocess.run(
        ["bash", str(HOOK), "origin", "https://github.com/acme/widget.git"],  # noqa: S607 — git from PATH is what the hook under test itself runs
        cwd=work,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _run_hook_via_prek(work: Path, env: dict[str, str], branch: str) -> subprocess.CompletedProcess[str]:
    """Drive the hook the way prek does: prek eats pre-push stdin and passes the range in ``PRE_COMMIT_*``."""
    synth = dict(env)
    synth["PRE_COMMIT_REMOTE_NAME"] = "origin"
    synth["PRE_COMMIT_TO_REF"] = _rev_parse(work, "HEAD")
    synth["PRE_COMMIT_FROM_REF"] = "0000000000000000000000000000000000000000"
    synth["PRE_COMMIT_LOCAL_BRANCH"] = f"refs/heads/{branch}"
    synth["PRE_COMMIT_REMOTE_BRANCH"] = f"refs/heads/{branch}"
    return subprocess.run(
        ["bash", str(HOOK), "origin", "https://github.com/acme/widget.git"],  # noqa: S607 — git from PATH is what the hook under test itself runs
        cwd=work,
        input="",
        capture_output=True,
        text=True,
        check=False,
        env=synth,
    )


def _foreign_open_pr(branch: str = "feature-x", author: str = "teammate") -> list[dict[str, object]]:
    return [
        {
            "number": 42,
            "url": "https://github.com/acme/widget/pull/42",
            "headRefName": branch,
            "author": {"login": author},
            "state": "OPEN",
        }
    ]


class TestRefusePushToForeignOpenMr:
    def test_blocks_push_to_foreign_open_mr_branch(self, tmp_path: Path) -> None:
        """An OPEN MR authored by a teammate → BLOCK, naming the author."""
        work, env = _setup(tmp_path, pr_payload=_foreign_open_pr())
        _commit(work, "feature.txt", "a clean feature line\n")

        result = _run_hook(work, env, "feature-x")

        assert result.returncode == 1, result.stdout + result.stderr
        combined = result.stdout + result.stderr
        assert "teammate" in combined, combined
        assert "42" in combined, combined

    def test_allows_push_to_our_own_open_mr_branch(self, tmp_path: Path) -> None:
        """An OPEN MR authored by us → ALLOW (our own work)."""
        payload = _foreign_open_pr(author=_OUR_LOGIN)
        work, env = _setup(tmp_path, pr_payload=payload)
        _commit(work, "feature.txt", "a clean feature line\n")

        result = _run_hook(work, env, "feature-x")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_author_match_is_case_insensitive(self, tmp_path: Path) -> None:
        """Our own MR with a differently-cased login → still ALLOW."""
        payload = _foreign_open_pr(author="Souliane")
        work, env = _setup(tmp_path, login="souliane", pr_payload=payload)
        _commit(work, "feature.txt", "a clean feature line\n")

        result = _run_hook(work, env, "feature-x")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_allows_push_when_no_mr_backs_the_branch(self, tmp_path: Path) -> None:
        """No open MR for the branch → ALLOW."""
        work, env = _setup(tmp_path, pr_payload=[])
        _commit(work, "feature.txt", "a clean feature line\n")

        result = _run_hook(work, env, "feature-x")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_allows_push_when_foreign_mr_is_closed(self, tmp_path: Path) -> None:
        """A closed/merged foreign MR is NOT protected — only OPEN ones.

        ``gh pr list --state open`` returns nothing for a closed/merged MR,
        so the payload is empty and the gate allows the push.
        """
        work, env = _setup(tmp_path, pr_payload=[])
        _commit(work, "feature.txt", "a clean feature line\n")

        result = _run_hook(work, env, "feature-x")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_override_token_allows_push_to_foreign_open_mr(self, tmp_path: Path) -> None:
        """The ``[push-to-foreign-mr-ok: <reason>]`` token lets a co-author push."""
        work, env = _setup(tmp_path, pr_payload=_foreign_open_pr())
        _commit(
            work,
            "feature.txt",
            "a clean feature line\n",
            message="add feature\n\n[push-to-foreign-mr-ok: pair-programming with teammate]",
        )

        result = _run_hook(work, env, "feature-x")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_override_token_allows_push_with_large_log_body_no_sigpipe(self, tmp_path: Path) -> None:
        """Honour the override token under a LARGE push-range log body (#2502).

        The override token lives in the NEWEST commit, which ``git log
        --format='%B'`` emits FIRST, so ``grep -q`` matches and closes the
        pipe almost immediately while ``git log`` still has hundreds of KB of
        older-commit bodies to write. Under the old ``git log | grep -q``
        pipeline (``set -o pipefail``) ``git log`` then dies with SIGPIPE
        (141), ``pipefail`` propagates 141, and the ``if`` is FALSE even
        though the token IS present — the gate wrongly BLOCKS a legitimate
        co-authoring push. The fixed here-string form has no producer process
        to receive SIGPIPE, so the match is deterministic and the push is
        allowed.

        This is the anti-vacuous companion to
        ``test_override_token_allows_push_to_foreign_open_mr`` — that test's
        single tiny commit never fills the pipe buffer, so it passes on BOTH
        the buggy pipe form and the fixed form and guards nothing against this
        regression.
        """
        work, env = _setup(tmp_path, pr_payload=_foreign_open_pr())
        # Older commits with large bodies: the bulk that git log must still
        # write AFTER grep -q matches the (newer) token commit and closes the
        # pipe. ~1MB total comfortably exceeds the OS pipe buffer (64KB Linux,
        # 16-64KB macOS) on every platform CI runs on.
        for ordinal in range(1, 5):
            _commit_with_large_body(work, "bulk.txt", ordinal, body_bytes=250_000)
        # NEWEST commit carries the override token — emitted FIRST by git log.
        _commit(
            work,
            "feature.txt",
            "a clean feature line\n",
            message="add feature\n\n[push-to-foreign-mr-ok: pair-programming with teammate]",
        )

        assert _git_log_body_bytes(work) > 512_000, "log body too small to exercise the SIGPIPE race"

        result = _run_hook(work, env, "feature-x")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_override_token_in_an_already_pushed_commit_does_not_authorise_this_push(self, tmp_path: Path) -> None:
        """The override is scoped to the commits the push INTRODUCES, not to all history.

        The token sits in a commit the remote already has, so it authorised its
        own push and nothing else. Searching the pushed sha's whole ancestry
        makes that one token a permanent blanket waiver for every later push to
        the teammate's branch.
        """
        work, env = _setup(tmp_path, pr_payload=_foreign_open_pr())
        _commit(
            work,
            "feature.txt",
            "a clean feature line\n",
            message="add feature\n\n[push-to-foreign-mr-ok: pair-programming with teammate]",
        )
        already_pushed = _rev_parse(work, "HEAD")
        _git(work, "update-ref", "refs/remotes/origin/feature-x", already_pushed)
        _commit(work, "later.txt", "an unrelated later change\n", message="unrelated follow-up")

        result = _run_hook(work, env, "feature-x", remote_sha=already_pushed)

        assert result.returncode == 1, result.stdout + result.stderr

    def test_override_token_in_a_newly_pushed_commit_still_allows_the_push(self, tmp_path: Path) -> None:
        """Narrowing the search to the push range keeps the genuine override working."""
        work, env = _setup(tmp_path, pr_payload=_foreign_open_pr())
        _commit(work, "base.txt", "already on the remote\n", message="base")
        already_pushed = _rev_parse(work, "HEAD")
        _git(work, "update-ref", "refs/remotes/origin/feature-x", already_pushed)
        _commit(
            work,
            "feature.txt",
            "a clean feature line\n",
            message="add feature\n\n[push-to-foreign-mr-ok: pair-programming with teammate]",
        )

        result = _run_hook(work, env, "feature-x", remote_sha=already_pushed)

        assert result.returncode == 0, result.stdout + result.stderr

    def test_override_scope_uses_tracking_refs_when_the_protocol_reports_no_remote_sha(
        self,
        tmp_path: Path,
    ) -> None:
        """A first push of the branch still subtracts what the remote already has."""
        work, env = _setup(tmp_path, pr_payload=_foreign_open_pr())
        _commit(
            work,
            "feature.txt",
            "a clean feature line\n",
            message="add feature\n\n[push-to-foreign-mr-ok: pair-programming with teammate]",
        )
        _git(work, "update-ref", "refs/remotes/origin/feature-x", _rev_parse(work, "HEAD"))
        _commit(work, "later.txt", "an unrelated later change\n", message="unrelated follow-up")

        result = _run_hook(work, env, "feature-x")

        assert result.returncode == 1, result.stdout + result.stderr

    def test_fails_open_when_gh_pr_list_errors(self, tmp_path: Path) -> None:
        """A transient ``gh`` API failure must ALLOW the push (fail open)."""
        work, env = _setup(tmp_path, failing_gh=True)
        _commit(work, "feature.txt", "a clean feature line\n")

        result = _run_hook(work, env, "feature-x")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_fails_open_when_gh_unavailable(self, tmp_path: Path) -> None:
        """No ``gh`` on PATH → cannot resolve MR → fail open (allow)."""
        work, env = _setup(tmp_path, pr_payload=_foreign_open_pr())
        env["PATH"] = "/usr/bin:/bin"
        _commit(work, "feature.txt", "a clean feature line\n")

        result = _run_hook(work, env, "feature-x")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_branch_deletion_push_is_skipped(self, tmp_path: Path) -> None:
        """A branch-deletion ref (local-sha all-zeros) is skipped → ALLOW."""
        work, env = _setup(tmp_path, pr_payload=_foreign_open_pr())
        _commit(work, "feature.txt", "a clean feature line\n")
        zero = "0000000000000000000000000000000000000000"
        stdin = f"refs/heads/feature-x {zero} refs/heads/feature-x {zero}\n"
        result = subprocess.run(
            ["bash", str(HOOK), "origin", "https://github.com/acme/widget.git"],  # noqa: S607 — git from PATH is what the hook under test itself runs
            cwd=work,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        assert result.returncode == 0, result.stdout + result.stderr

    def test_only_blocks_the_ref_whose_branch_backs_a_foreign_mr(self, tmp_path: Path) -> None:
        """The head-branch match is per-ref: a non-matching head MR does not block.

        The open MR's ``headRefName`` is a different branch than the one
        being pushed, so the gate must NOT block — proving the branch-name
        match is load-bearing, not a blanket "any open foreign MR exists".
        """
        payload = _foreign_open_pr(branch="some-other-branch", author="teammate")
        work, env = _setup(tmp_path, pr_payload=payload)
        _commit(work, "feature.txt", "a clean feature line\n")

        result = _run_hook(work, env, "feature-x")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_prek_invocation_blocks_on_an_empty_stdin_push(self, tmp_path: Path) -> None:
        """The prek wiring is the production path, and a stdin-only gate is inert on it."""
        work, env = _setup(tmp_path, pr_payload=_foreign_open_pr())
        _commit(work, "feature.txt", "a clean feature line\n")

        result = _run_hook_via_prek(work, env, "feature-x")

        combined = result.stdout + result.stderr
        assert result.returncode == 1, combined
        assert "teammate" in combined, combined

    def test_prek_invocation_allows_our_own_open_mr(self, tmp_path: Path) -> None:
        """The synthesized range resolves the real branch, not a blanket block."""
        work, env = _setup(tmp_path, pr_payload=_foreign_open_pr(author=_OUR_LOGIN))
        _commit(work, "feature.txt", "a clean feature line\n")

        result = _run_hook_via_prek(work, env, "feature-x")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_hook_is_executable(self) -> None:
        assert os.access(HOOK, os.X_OK), f"{HOOK} must be chmod +x"


def _make_glab_shim(bin_dir: Path, *, username: str, merge_requests: list[dict[str, object]]) -> None:
    """Write a fake ``glab`` answering ``api user`` and the open-MR query.

    ``glab api`` has no ``--jq`` flag, so both answers are plain JSON — the
    shape the resolver parses in Python.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "glab"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        f"rows = json.loads({json.dumps(merge_requests)!r})\n"
        "if args[:2] == ['api', 'user']:\n"
        f"    print(json.dumps({{'username': {username!r}}}))\n"
        "    sys.exit(0)\n"
        "if args[0] == 'api' and 'merge_requests' in args[1]:\n"
        "    branch = args[1].split('source_branch=')[1].split('&')[0]\n"
        "    print(json.dumps([r for r in rows if r['source_branch'] == branch]))\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestGitLabRemotesAreGatedToo:
    """A GitLab remote reaches the guard at all.

    The hook resolved the backing MR with ``gh`` and exited early when ``gh`` was
    absent, so on a GitLab remote — the remote a fork of this project pushes to —
    it could never fire. Nothing reported that; a guard that cannot ask reads
    exactly like a guard that found nothing.
    """

    _REMOTE = "https://gitlab.com/acme-eng/widget.git"

    def _setup_gitlab(self, tmp_path: Path, *, author: str) -> tuple[Path, dict[str, str]]:
        origin = tmp_path / "origin"
        _make_repo(origin)
        work = tmp_path / "work"
        _git(tmp_path, "clone", str(origin), str(work))
        _git(work, "config", "user.email", _NOREPLY_EMAIL)
        _git(work, "config", "user.name", _NOREPLY_NAME)
        _git(work, "checkout", "-b", "feature-x")
        _git(work, "remote", "set-url", "origin", self._REMOTE)

        bin_dir = tmp_path / "bin"
        _make_glab_shim(
            bin_dir,
            username=_OUR_LOGIN,
            merge_requests=[{"iid": 77, "source_branch": "feature-x", "author": {"username": author}}],
        )
        env = _hermetic_env()
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        return work, env

    def _run(self, work: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 — git from PATH is what the hook itself runs
            cwd=work,
            capture_output=True,
            text=True,
            check=True,
            env=_hermetic_env(),
        ).stdout.strip()
        stdin = f"refs/heads/feature-x {sha} refs/heads/feature-x {'0' * 40}\n"
        return subprocess.run(
            ["bash", str(HOOK), "origin", self._REMOTE],  # noqa: S607 — bash via PATH is the real invocation
            cwd=work,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_blocks_push_to_a_foreign_open_gitlab_mr(self, tmp_path: Path) -> None:
        work, env = self._setup_gitlab(tmp_path, author="teammate")
        _commit(work, "feature.txt", "a clean feature line\n")

        result = self._run(work, env)

        combined = result.stdout + result.stderr
        assert result.returncode == 1, combined
        assert "teammate" in combined, combined
        assert "77" in combined, combined

    def test_allows_push_to_our_own_open_gitlab_mr(self, tmp_path: Path) -> None:
        work, env = self._setup_gitlab(tmp_path, author=_OUR_LOGIN)
        _commit(work, "feature.txt", "a clean feature line\n")

        result = self._run(work, env)

        assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
