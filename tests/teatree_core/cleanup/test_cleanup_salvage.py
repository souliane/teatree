"""Salvage primitive — capture-then-delete ordering against real git (#2763).

The load-bearing invariant: the source item is deleted ONLY after the forge
confirms the PR landed. A failed push / open / verify, or a banned-terms hit,
leaves the source intact — salvage never destroys the only copy on its own
failure.
"""

import subprocess
from pathlib import Path

from teatree.core.cleanup.cleanup_salvage import SalvageHooks, SalvageRequest, salvage_item
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


def _repo_with_feature(tmp: Path, *, body: str = "feature work\n", subject: str = "feat: ship") -> Path:
    remote = tmp / "remote.git"
    subprocess.run(
        [_GIT, "init", "-q", "--bare", "-b", "main", str(remote)], check=True, capture_output=True, env=_clean_env()
    )
    work = tmp / "work"
    work.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=work)
    _run_git("config", "user.email", "t@t", cwd=work)
    _run_git("config", "user.name", "t", cwd=work)
    _run_git("remote", "add", "origin", str(remote), cwd=work)
    (work / "base.txt").write_text("base\n", encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    _run_git("commit", "-q", "-m", "initial", cwd=work)
    _run_git("push", "-q", "origin", "main", cwd=work)
    _run_git("fetch", "-q", "origin", cwd=work)
    _run_git("checkout", "-q", "-b", "feature", "main", cwd=work)
    (work / "f.txt").write_text(body, encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    _run_git("commit", "-q", "-m", subject, cwd=work)
    _run_git("checkout", "-q", "main", cwd=work)
    return work


class _Recorder:
    """Records the order of side-effecting hook calls so capture-vs-delete order is asserted."""

    def __init__(self, *, push_ok: bool = True, pr_url: str = "https://x/pr/1", verified: bool = True) -> None:
        self.push_ok = push_ok
        self.pr_url = pr_url
        self.verified = verified
        self.calls: list[str] = []

    def hooks(self) -> SalvageHooks:
        def push(_repo: str, _branch: str) -> bool:
            self.calls.append("push")
            return self.push_ok

        def open_pr(_repo: str, _branch: str, _target: str) -> str:
            self.calls.append("open_pr")
            return self.pr_url

        def verify(_repo: str, _branch: str) -> bool:
            self.calls.append("verify")
            return self.verified

        def delete() -> list[str]:
            self.calls.append("delete")
            return []

        return SalvageHooks(push=push, open_pr=open_pr, verify_landed=verify, delete_source=delete)


def test_captures_then_deletes_when_pr_verified(tmp_path: Path) -> None:
    work = _repo_with_feature(tmp_path)
    rec = _Recorder(verified=True)

    result = salvage_item(
        SalvageRequest(repo=str(work), source_ref="feature", salvage_branch="salvage/feature"),
        rec.hooks(),
    )

    assert result.salvaged is True
    assert result.deleted is True
    assert rec.calls == ["push", "open_pr", "verify", "delete"], "delete must come LAST, after verify"
    branches = subprocess.run(
        [_GIT, "-C", str(work), "branch", "--format=%(refname:short)"],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    ).stdout
    assert "salvage/feature" in branches.split()


def test_does_not_delete_when_verification_fails(tmp_path: Path) -> None:
    work = _repo_with_feature(tmp_path)
    rec = _Recorder(verified=False)

    result = salvage_item(
        SalvageRequest(repo=str(work), source_ref="feature", salvage_branch="salvage/feature"),
        rec.hooks(),
    )

    assert result.salvaged is True
    assert result.deleted is False, "an unverified landing must NOT delete the source"
    assert "delete" not in rec.calls


def test_does_not_delete_when_push_fails(tmp_path: Path) -> None:
    work = _repo_with_feature(tmp_path)
    rec = _Recorder(push_ok=False)

    result = salvage_item(
        SalvageRequest(repo=str(work), source_ref="feature", salvage_branch="salvage/feature"),
        rec.hooks(),
    )

    assert result.salvaged is False
    assert result.deleted is False
    assert rec.calls == ["push"], "a failed push must short-circuit before open_pr/verify/delete"


def test_refuses_when_banned_terms_present(tmp_path: Path) -> None:
    work = _repo_with_feature(tmp_path, body="password=hunter2\n", subject="chore: add the secret credential")
    rec = _Recorder()

    result = salvage_item(
        SalvageRequest(repo=str(work), source_ref="feature", salvage_branch="salvage/feature"),
        rec.hooks(),
    )

    assert result.salvaged is False
    assert result.deleted is False
    assert rec.calls == [], "banned-terms refusal must happen before any push/PR/delete"
    assert any("banned terms" in e for e in result.errors)


def test_refuses_when_source_content_is_unreadable(tmp_path: Path) -> None:
    # #F4.6 leak: an UNREADABLE source (the ref does not resolve, so the strict
    # content probe raises) must REFUSE — the old lenient probe degraded to
    # ``["", ""]`` which the scanner read as "clean" and pushed the branch to a
    # public PR completely unscanned. No push/PR/delete may happen.
    work = _repo_with_feature(tmp_path)
    rec = _Recorder(verified=True)

    result = salvage_item(
        SalvageRequest(repo=str(work), source_ref="no-such-ref", salvage_branch="salvage/x"),
        rec.hooks(),
    )

    assert result.salvaged is False
    assert result.deleted is False
    assert rec.calls == [], "an unscannable source must refuse before any push/PR/delete"
    assert any("could not read the source content" in e for e in result.errors)


def test_refuses_when_there_is_no_unique_content_to_scan(tmp_path: Path) -> None:
    # A source ref with NO unique content vs target yields an empty scan corpus
    # ("unknown"). Refuse rather than push an unscanned (albeit empty) branch —
    # the final safety gate fails CLOSED on an inconclusive scan (#F4.6).
    work = _repo_with_feature(tmp_path)
    rec = _Recorder(verified=True)

    result = salvage_item(
        SalvageRequest(repo=str(work), source_ref="main", salvage_branch="salvage/main", target="origin/main"),
        rec.hooks(),
    )

    assert result.salvaged is False
    assert result.deleted is False
    assert rec.calls == []
    assert any("could not read the source content" in e for e in result.errors)


def test_scan_can_be_bypassed_when_banned_clean_not_required(tmp_path: Path) -> None:
    # ``require_banned_clean=False`` skips the scan-gate entirely, so an
    # unreadable/empty content corpus does NOT block the salvage (the caller has
    # opted out of the final safety gate).
    work = _repo_with_feature(tmp_path)
    rec = _Recorder(verified=True)

    result = salvage_item(
        SalvageRequest(
            repo=str(work),
            source_ref="feature",
            salvage_branch="salvage/feature",
            require_banned_clean=False,
        ),
        rec.hooks(),
    )

    assert result.salvaged is True
    assert result.deleted is True
    assert rec.calls == ["push", "open_pr", "verify", "delete"]
