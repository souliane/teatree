"""Ownership guard — exclude a colleague's work on a product repo (#2763), real git.

Anti-vacuous: the owner's solo repos (no colleague pattern) are never excluded;
on a colleague-facing product repo the owner's own branch is kept but a
colleague-authored branch is excluded up front.
"""

import subprocess
from pathlib import Path

from teatree.core.cleanup.cleanup_ownership import is_excluded_by_ownership
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


def _repo(tmp: Path, *, author_name: str, author_email: str, owner_name: str = "souliane") -> Path:
    """A repo whose local git identity is the OWNER, with a feature branch authored by ``author``."""
    work = tmp / "product"
    work.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=work)
    _run_git("remote", "add", "origin", "https://github.com/acme-product/backend.git", cwd=work)
    _run_git("config", "user.name", owner_name, cwd=work)
    _run_git("config", "user.email", f"{owner_name}@users.noreply.github.com", cwd=work)
    (work / "base.txt").write_text("base\n", encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    _run_git("commit", "-q", "-m", "initial", cwd=work)
    _run_git("checkout", "-q", "-b", "feature", cwd=work)
    (work / "f.txt").write_text("work\n", encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    env = {**_clean_env(), "GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email}
    subprocess.run([_GIT, "-C", str(work), "commit", "-q", "-m", "feat: work"], check=True, env=env)
    return work


_PRODUCT_PATTERN = r"acme-product/"


def test_solo_repo_with_no_pattern_is_never_excluded(tmp_path: Path) -> None:
    work = _repo(tmp_path, author_name="someone-else", author_email="other@corp.com")
    verdict = is_excluded_by_ownership(str(work), "feature", owner_aliases=[], colleague_pattern="")
    assert verdict.excluded is False


def test_colleague_repo_owner_authored_branch_is_kept(tmp_path: Path) -> None:
    work = _repo(tmp_path, author_name="souliane", author_email="souliane@users.noreply.github.com")
    verdict = is_excluded_by_ownership(
        str(work), "feature", owner_aliases=["souliane"], colleague_pattern=_PRODUCT_PATTERN
    )
    assert verdict.excluded is False


def test_colleague_repo_colleague_authored_branch_is_excluded(tmp_path: Path) -> None:
    work = _repo(tmp_path, author_name="a-colleague", author_email="colleague@acme.com")
    verdict = is_excluded_by_ownership(
        str(work), "feature", owner_aliases=["souliane"], colleague_pattern=_PRODUCT_PATTERN
    )
    assert verdict.excluded is True
    assert "colleague-authored" in verdict.reason


def test_owner_alias_matches_even_when_git_identity_differs(tmp_path: Path) -> None:
    # The branch author is a secondary owner alias, not the repo's configured identity.
    work = _repo(tmp_path, author_name="souliane-alt", author_email="alt@personal.com", owner_name="souliane")
    verdict = is_excluded_by_ownership(
        str(work), "feature", owner_aliases=["souliane-alt"], colleague_pattern=_PRODUCT_PATTERN
    )
    assert verdict.excluded is False
