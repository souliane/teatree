"""#762 — scoped public-souliane noreply identity (source-fix + merge guard).

Single source of truth for: the GitHub-noreply regex (#730's pattern),
public-`souliane/*` detection from a remote slug, and the canonical
noreply identity. Reused by the worktree provisioner (source-fix:
per-repo local git identity) and the merge-path guard (defense-in-depth
against the server-side squash author mismatch). Strictly scoped — non-souliane /
private remotes are NOT treated as public-souliane, so their legitimate
real-identity attribution is untouched.
"""

import pytest

from teatree.core.public_identity import (
    NOREPLY_RE,
    canonical_noreply_identity,
    is_noreply_email,
    is_public_souliane_remote,
)


class TestNoreplyPattern:
    @pytest.mark.parametrize(
        "email",
        [
            "21343492+souliane@users.noreply.github.com",
            "258769440+adrien-acme@users.noreply.github.com",
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


class TestPublicSoulianeDetection:
    @pytest.mark.parametrize(
        "slug",
        ["souliane/teatree", "souliane/skills"],
    )
    def test_public_souliane_slugs(self, slug: str) -> None:
        assert is_public_souliane_remote(slug)

    @pytest.mark.parametrize(
        "slug",
        [
            "acme-private/internal-svc",  # private overlay — must NOT be scoped
            "acme-private/internal-svc-e2e",
            "acme-eng/internal-product",
            "someoneelse/teatree",  # not the souliane org
            "",
        ],
    )
    def test_non_souliane_or_private_excluded(self, slug: str) -> None:
        assert not is_public_souliane_remote(slug)

    def test_detects_from_full_remote_urls(self) -> None:
        assert is_public_souliane_remote("https://github.com/souliane/teatree.git")
        assert is_public_souliane_remote("git@github.com:souliane/skills.git")
        assert not is_public_souliane_remote("git@github.com:acme-private/internal-svc.git")


class TestCanonicalIdentity:
    def test_canonical_identity_is_noreply(self) -> None:
        name, email = canonical_noreply_identity()
        assert is_noreply_email(email), email
        assert name
