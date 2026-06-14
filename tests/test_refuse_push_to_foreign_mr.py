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
        ["git", *args],  # noqa: S607
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


def _run_hook(work: Path, env: dict[str, str], branch: str) -> subprocess.CompletedProcess[str]:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
        env=_hermetic_env(),
    ).stdout.strip()
    stdin = f"refs/heads/{branch} {sha} refs/heads/{branch} 0000000000000000000000000000000000000000\n"
    return subprocess.run(
        ["bash", str(HOOK), "origin", "https://github.com/acme/widget.git"],  # noqa: S607
        cwd=work,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
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
            ["bash", str(HOOK), "origin", "https://github.com/acme/widget.git"],  # noqa: S607
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

    def test_hook_is_executable(self) -> None:
        assert os.access(HOOK, os.X_OK), f"{HOOK} must be chmod +x"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
