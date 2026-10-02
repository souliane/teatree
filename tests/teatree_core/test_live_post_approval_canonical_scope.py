"""Direct coverage for ``canonical_mr_scope`` URL parsing (#1207).

The ``LivePostApproval`` row is scoped on the canonical ``<repo>!<iid>``
token derived from whatever the user typed into ``approve-live-post``.
Higher-level tests only exercise the single-level ``org/repo`` GitLab
URL; this file pins the nested-group and GitHub PR paths so an approval
recorded for the URL form matches a ``--live`` invocation made with
``<repo>`` plus ``<iid>``.

Distinct canonical scopes must compare equal across input forms (URL vs
already-canonical), and must NOT collide across distinct projects (a
deeper-nested project must yield a different scope than a shallower
one).
"""

from teatree.core.models.live_post_approval import canonical_mr_scope


class TestCanonicalMrScopeNestedGroups:
    """Nested-group GitLab URLs canonicalize through every depth."""

    def test_single_level_gitlab_group(self) -> None:
        assert canonical_mr_scope("https://gitlab.com/org/proj/-/merge_requests/42") == "org/proj!42"

    def test_nested_group_one_level(self) -> None:
        """``org/sub/proj`` → ``org/sub/proj!123`` (preserves group nesting)."""
        assert canonical_mr_scope("https://gitlab.com/org/sub/proj/-/merge_requests/123") == "org/sub/proj!123"

    def test_nested_group_two_levels(self) -> None:
        """Deep nesting: ``org/sub/deep/proj`` → ``org/sub/deep/proj!456``."""
        assert (
            canonical_mr_scope("https://gitlab.com/org/sub/deep/proj/-/merge_requests/456") == "org/sub/deep/proj!456"
        )

    def test_nested_groups_yield_distinct_scopes(self) -> None:
        """A nested-group project's scope must NOT collide with the shallower form."""
        shallow = canonical_mr_scope("https://gitlab.com/org/proj/-/merge_requests/1")
        nested = canonical_mr_scope("https://gitlab.com/org/sub/proj/-/merge_requests/1")
        deeper = canonical_mr_scope("https://gitlab.com/org/sub/deep/proj/-/merge_requests/1")

        assert shallow != nested
        assert nested != deeper
        assert shallow != deeper


class TestCanonicalMrScopeGitHub:
    """GitHub PR URLs canonicalize through the ``/pull/<iid>`` segment."""

    def test_github_pr(self) -> None:
        assert canonical_mr_scope("https://github.com/owner/repo/pull/789") == "owner/repo!789"

    def test_github_pr_distinct_from_gitlab_same_iid(self) -> None:
        """Same ``<owner/repo>!<iid>`` token across platforms is allowed by design.

        The canonical form is platform-neutral — what matters is that the
        same MR yields the same token regardless of input form (URL vs
        canonical-typed). Two distinct projects on different platforms
        that happen to share ``<owner/repo>`` would collide; that's the
        operator's responsibility to disambiguate at name-choice time.
        """
        gitlab_token = canonical_mr_scope("https://gitlab.com/owner/repo/-/merge_requests/789")
        github_token = canonical_mr_scope("https://github.com/owner/repo/pull/789")

        assert gitlab_token == github_token == "owner/repo!789"


class TestCanonicalMrScopePassthrough:
    """Already-canonical form is preserved verbatim — that's the matching contract."""

    def test_simple_canonical_passthrough(self) -> None:
        assert canonical_mr_scope("org/proj!42") == "org/proj!42"

    def test_nested_canonical_passthrough(self) -> None:
        assert canonical_mr_scope("org/sub/proj!123") == "org/sub/proj!123"

    def test_deep_nested_canonical_passthrough(self) -> None:
        assert canonical_mr_scope("org/sub/deep/proj!456") == "org/sub/deep/proj!456"


class TestCanonicalMrScopeRoundTrip:
    """A URL and its canonical form must yield the same scope (the equality contract)."""

    def test_gitlab_nested_roundtrip(self) -> None:
        """URL and pre-canonicalised input compare equal under the canonical function."""
        from_url = canonical_mr_scope("https://gitlab.com/org/sub/proj/-/merge_requests/123")
        from_canonical = canonical_mr_scope("org/sub/proj!123")

        assert from_url == from_canonical

    def test_github_roundtrip(self) -> None:
        from_url = canonical_mr_scope("https://github.com/owner/repo/pull/789")
        from_canonical = canonical_mr_scope("owner/repo!789")

        assert from_url == from_canonical
