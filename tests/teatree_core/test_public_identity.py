"""#762 — scoped public-souliane noreply identity (source-fix + merge guard).

Single source of truth for: the GitHub-noreply regex (#730's pattern),
public-`souliane/*` detection from a remote slug, and the canonical
noreply identity. Reused by the worktree provisioner (source-fix:
per-repo local git identity) and the merge-path guard (defense-in-depth
against the server-side squash author mismatch). Strictly scoped — non-souliane /
private remotes are NOT treated as public-souliane, so their legitimate
real-identity attribution is untouched.
"""

from unittest.mock import patch

import pytest

from teatree.core.public_identity import (
    NOREPLY_RE,
    canonical_noreply_identity,
    is_github_host,
    is_noreply_email,
    is_public_github_remote,
)


class TestNoreplyPattern:
    @pytest.mark.parametrize(
        "email",
        [
            "21343492+souliane@users.noreply.github.com",
            "258769440+octo-contrib@users.noreply.github.com",
            "octocat@users.noreply.github.com",
        ],
    )
    def test_noreply_addresses_match(self, email: str) -> None:
        assert is_noreply_email(email)
        assert NOREPLY_RE.match(email)

    @pytest.mark.parametrize(
        "email",
        [
            "real.dev@internal.example",
            "someone@users.noreply.github.com.evil.com",
            "",
            "noreply@github.com",  # the web-flow committer — NOT an author noreply
        ],
    )
    def test_non_noreply_rejected(self, email: str) -> None:
        assert not is_noreply_email(email)


class TestPublicGithubVisibility:
    """#785 — the proactive identity gate is visibility-based, not owner-hardcoded.

    Mirrors the reactive hook (`gh repo view <slug> --json visibility`)
    so every PUBLIC GitHub repo is covered — not just `souliane/*`. The
    `gh` subprocess is the only unstoppable external and is mocked.
    """

    @staticmethod
    def _gh(visibility: str, *, rc: int = 0):
        from teatree.utils.run import CommandFailedError  # noqa: PLC0415

        def _fake(cmd: list[str], **_kw: object) -> object:
            if rc != 0:
                raise CommandFailedError(cmd, rc, "", "not found")
            return type("R", (), {"stdout": visibility + "\n", "returncode": 0})()

        return _fake

    def test_public_non_souliane_repo_is_public(self) -> None:
        # The exact bug #785: a PUBLIC repo owned by a non-souliane
        # account must now be detected (owner-hardcoded gate missed it).
        with patch("teatree.core.public_identity.run_allowed_to_fail", side_effect=self._gh("PUBLIC")):
            assert is_public_github_remote("git@github.com:octo-contrib/sample-repo.git") is True

    def test_public_souliane_repo_still_public(self) -> None:
        with patch("teatree.core.public_identity.run_allowed_to_fail", side_effect=self._gh("PUBLIC")):
            assert is_public_github_remote("https://github.com/souliane/teatree.git") is True

    def test_private_repo_is_not_public(self) -> None:
        with patch("teatree.core.public_identity.run_allowed_to_fail", side_effect=self._gh("PRIVATE")):
            assert is_public_github_remote("git@github.com:acme-private/internal-svc.git") is False

    def test_internal_visibility_is_not_public(self) -> None:
        with patch("teatree.core.public_identity.run_allowed_to_fail", side_effect=self._gh("INTERNAL")):
            assert is_public_github_remote("git@github.com:acme-eng/internal-product.git") is False

    def test_gh_unavailable_fails_safe_to_not_public(self) -> None:
        # Visibility unconfirmable → do NOT proactively set noreply
        # (leave inherited identity); the reactive hook also passes on
        # unknown, so no hard-fail asymmetry is introduced.
        with patch("teatree.core.public_identity.run_allowed_to_fail", side_effect=self._gh("", rc=1)):
            assert is_public_github_remote("git@github.com:souliane/teatree.git") is False

    def test_empty_remote_is_not_public_without_calling_gh(self) -> None:
        with patch("teatree.core.public_identity.run_allowed_to_fail") as gh:
            assert is_public_github_remote("") is False
            gh.assert_not_called()

    @pytest.mark.parametrize("remote", ["just-a-name", "https://github.com/onlyowner", "/leading-slash-only/"])
    def test_malformed_slug_is_not_public_without_calling_gh(self, remote: str) -> None:
        # A non-empty remote that does not yield exactly owner/repo must
        # short-circuit to False before any gh call (the malformed-slug
        # guard) — never query visibility for an unparsable remote.
        with patch("teatree.core.public_identity.run_allowed_to_fail") as gh:
            assert is_public_github_remote(remote) is False
            gh.assert_not_called()


class TestIsGithubHost:
    """The host guard (#2655) parses the host out of every git remote shape."""

    @pytest.mark.parametrize(
        "remote",
        [
            "git@github.com:souliane/teatree.git",
            "https://github.com/souliane/teatree.git",
            "ssh://git@github.com/souliane/teatree.git",
            "https://user@github.com:443/souliane/teatree.git",
            "git@github.com-work:souliane/teatree.git",  # ssh-alias host
            "git@github.com-adrien-oper:souliane/teatree.git",
        ],
    )
    def test_github_hosts(self, remote: str) -> None:
        assert is_github_host(remote) is True

    @pytest.mark.parametrize(
        "remote",
        [
            "git@gitlab.com:acme-eng/widget.git",
            "https://gitlab.com/acme-eng/sidecar.git",
            "git@bitbucket.org:team/repo.git",
            "https://gitlab.example.com/group/proj.git",
            "ssh://git@gitlab.example.com:2222/group/proj.git",
            "souliane/teatree",  # bare slug — no host at all
            "",
            "git@notgithub.com:souliane/teatree.git",  # host that merely contains github.com? no
        ],
    )
    def test_non_github_hosts(self, remote: str) -> None:
        assert is_github_host(remote) is False


class TestNonGithubHostNeverPublic:
    """A non-github.com remote is NEVER treated as a public GitHub repo (#2655).

    The slug parser strips the host, collapsing a non-github URL such as
    ``git@gitlab.com:acme-eng/widget.git`` to the bare ``acme-eng/widget``
    — which ``gh repo view`` would then resolve against **github.com**. If
    a public github.com repo happened to exist at that owner/repo, a
    non-github clone would be stamped with the public GitHub noreply
    identity instead of its inherited identity. The host guard must
    short-circuit a non-github host to ``False`` BEFORE any ``gh`` call,
    so a gitlab/bitbucket remote can never be queried — or stamped — as
    github.
    """

    @staticmethod
    def _gh_public(_cmd: list[str], **_kw: object) -> object:
        return type("R", (), {"stdout": "PUBLIC\n", "returncode": 0})()

    @pytest.mark.parametrize(
        "remote",
        [
            "git@gitlab.com:acme-eng/widget.git",
            "https://gitlab.com/acme-eng/sidecar.git",
            "git@bitbucket.org:team/repo.git",
            "https://gitlab.example.com/group/proj.git",
        ],
    )
    def test_non_github_host_is_not_public_without_calling_gh(self, remote: str) -> None:
        # Even if gh WOULD answer PUBLIC for the host-stripped slug, a
        # non-github host must never reach gh — it is not a GitHub repo.
        with patch(
            "teatree.core.public_identity.run_allowed_to_fail",
            side_effect=self._gh_public,
        ) as gh:
            assert is_public_github_remote(remote) is False
            gh.assert_not_called()

    @pytest.mark.parametrize(
        "remote",
        [
            "git@github.com:souliane/teatree.git",
            "git@github.com-work:souliane/teatree.git",  # ssh-alias host
            "https://github.com/souliane/teatree.git",
            "ssh://git@github.com/souliane/teatree.git",
        ],
    )
    def test_github_host_variants_still_reach_gh(self, remote: str) -> None:
        # github.com (and a github.com-<alias> ssh host) must still be
        # recognised so public github repos keep getting the source-fix.
        with patch(
            "teatree.core.public_identity.run_allowed_to_fail",
            side_effect=self._gh_public,
        ) as gh:
            assert is_public_github_remote(remote) is True
            gh.assert_called_once()


class TestCanonicalIdentity:
    def test_canonical_identity_is_noreply(self) -> None:
        name, email = canonical_noreply_identity()
        assert is_noreply_email(email), email
        assert name
