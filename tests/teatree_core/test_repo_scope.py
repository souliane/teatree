"""Forge-host-keyed repo-SCOPE classifier (real git remotes under tmp_path).

SCOPE = owned vs unknown, the axis orthogonal to VISIBILITY (public/private
leak-prevention, fails OPEN) and COLLABORATION (the author/review gate). The
headline guard is host-awareness: a ``gitlab.com/souliane/x`` remote must NOT
match a ``{"github.com": ["souliane"]}`` scope — the EXACT host-key lookup is
the structural fix.
"""

import os
import subprocess
from pathlib import Path

from teatree.core.repo_scope import (
    RepoIdentity,
    host_aware_owns,
    identity_from_host_and_slug,
    normalize_host,
    repo_identity_for_cwd,
    repo_scope,
)

_TEATREE_SCOPE = {"github.com": ["souliane", "acme-eng/widget-overlay"]}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def _repo_with_remote(path: Path, remote_url: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "remote", "add", "origin", remote_url)
    return path


class TestNormalizeHost:
    def test_lowercases_and_strips_scheme_user_port_www_git(self) -> None:
        assert normalize_host("https://www.GitHub.com:443/owner/repo.git") == "github.com"

    def test_ssh_scp_form(self) -> None:
        assert normalize_host("git@gitlab.com") == "gitlab.com"

    def test_bare_host_is_unchanged(self) -> None:
        assert normalize_host("gitlab.acme.internal") == "gitlab.acme.internal"


class TestRepoIdentityForCwd:
    def test_https_github(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "a", "https://github.com/souliane/teatree.git")
        identity = repo_identity_for_cwd(repo)
        assert identity == RepoIdentity(host="github.com", namespace="souliane/teatree")

    def test_ssh_scp_gitlab(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "b", "git@gitlab.com:acme-eng/widget-overlay-e2e.git")
        identity = repo_identity_for_cwd(repo)
        assert identity == RepoIdentity(host="gitlab.com", namespace="acme-eng/widget-overlay-e2e")

    def test_ssh_url_form(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "c", "ssh://git@github.com/souliane/blog.git")
        identity = repo_identity_for_cwd(repo)
        assert identity == RepoIdentity(host="github.com", namespace="souliane/blog")

    def test_dotless_ssh_alias_yields_no_host(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "d", "git@gh-personal:souliane/teatree.git")
        identity = repo_identity_for_cwd(repo)
        assert identity.host == ""

    def test_no_remote_yields_empty_identity(self, tmp_path: Path) -> None:
        path = tmp_path / "e"
        path.mkdir()
        _git(path, "init", "-b", "main")
        assert repo_identity_for_cwd(path) == RepoIdentity(host="", namespace="")


class TestRepoScopeOwnedUnknown:
    def test_owned_github_repo_is_owned(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "own", "https://github.com/souliane/teatree.git")
        assert repo_scope(repo, _TEATREE_SCOPE) == "owned"

    def test_owned_exact_namespace_entry(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "exact", "git@github.com:acme-eng/widget-overlay.git")
        assert repo_scope(repo, _TEATREE_SCOPE) == "owned"

    def test_unknown_github_repo(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "unk", "https://github.com/randomuser/randomrepo.git")
        assert repo_scope(repo, _TEATREE_SCOPE) == "unknown"

    def test_dotless_alias_is_unknown_fail_safe(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "alias", "git@gh-personal:souliane/teatree.git")
        assert repo_scope(repo, _TEATREE_SCOPE) == "unknown"


class TestHostGateIsSymmetric:
    """The headline bug: host-symmetric matching let a gitlab repo match a github scope."""

    def test_gitlab_repo_does_not_match_github_scope(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "gl", "git@gitlab.com:souliane/x.git")
        assert repo_scope(repo, {"github.com": ["souliane"]}) == "unknown"

    def test_github_repo_matches_github_scope(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "gh", "git@github.com:souliane/teatree.git")
        assert repo_scope(repo, {"github.com": ["souliane"]}) == "owned"

    def test_same_namespace_different_host_is_unknown(self) -> None:
        gh = RepoIdentity(host="github.com", namespace="souliane/x")
        gl = RepoIdentity(host="gitlab.com", namespace="souliane/x")
        scope = {"github.com": ["souliane"]}
        assert host_aware_owns(scope, gh) is True
        assert host_aware_owns(scope, gl) is False


class TestWholeHostWildcard:
    def test_star_owns_every_namespace_on_that_host(self) -> None:
        scope = {"gitlab.acme.internal": ["*"]}
        assert host_aware_owns(scope, RepoIdentity(host="gitlab.acme.internal", namespace="team/svc")) is True

    def test_star_does_not_leak_to_other_hosts(self) -> None:
        scope = {"gitlab.acme.internal": ["*"]}
        assert host_aware_owns(scope, RepoIdentity(host="github.com", namespace="team/svc")) is False


class TestPolarityFailsClosed:
    """Ownership fails CLOSED: an unresolvable / empty identity is ``unknown``.

    This is the OPPOSITE polarity to the VISIBILITY gate, which fails OPEN
    (an unknown repo there is treated as not-private / scanned-as-public).
    The two verdicts are never shared — see ``repo_scope`` and
    ``teatree.hooks.publish_destination`` docstrings.
    """

    def test_empty_identity_is_unknown(self) -> None:
        assert host_aware_owns(_TEATREE_SCOPE, RepoIdentity(host="", namespace="")) is False

    def test_host_present_but_no_namespace_is_unknown(self) -> None:
        assert host_aware_owns(_TEATREE_SCOPE, RepoIdentity(host="github.com", namespace="")) is False

    def test_substring_owner_does_not_falsely_match(self) -> None:
        fork = RepoIdentity(host="github.com", namespace="souliane-fork/x")
        assert host_aware_owns({"github.com": ["souliane"]}, fork) is False


class TestBothIdentityHelpersAgreeOnUnresolvableHost:
    """Both identity helpers apply the SAME dotted-host requirement.

    The push path (``repo_identity_for_cwd``) and the merge path
    (``identity_from_host_and_slug``) classify a dotless / alias host
    identically — an unresolvable host is the uncertainty axis (empty identity →
    fails open downstream), never a known-but-unowned host (which would fail
    closed).
    """

    def test_dotless_host_yields_empty_identity_on_the_merge_path(self) -> None:
        assert identity_from_host_and_slug("gh-personal", "souliane/teatree") == RepoIdentity(host="", namespace="")

    def test_dotted_host_yields_a_resolved_identity_on_the_merge_path(self) -> None:
        identity = identity_from_host_and_slug("https://gitlab.com/x/-/issues/1", "x/overlay-repo")
        assert identity == RepoIdentity(host="gitlab.com", namespace="x/overlay-repo")

    def test_dotless_host_resolves_the_same_on_both_paths(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "alias", "git@gh-personal:souliane/teatree.git")
        cwd_identity = repo_identity_for_cwd(repo)
        merge_identity = identity_from_host_and_slug("gh-personal", "souliane/teatree")
        assert cwd_identity.host == merge_identity.host == ""
