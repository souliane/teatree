"""Owner-aware overlay-repo matching for NULL-ticket ceremony scoping.

``repo_in_overlay`` scopes a resolved ``owner/repo`` slug to an overlay using
its declared repos (``get_followup_repos()`` qualified + ``get_repos()`` often
bare). A bare declared name historically matched on repo name alone, so a
same-named repo under a foreign owner was wrongly claimed. ``repo_entry_matches``
makes the comparison owner-aware: a bare name is honoured only when the resolved
slug's owner is one the overlay declares elsewhere as ``owner/repo``.
"""

import pytest

from teatree.core._checking_gather import repo_entry_matches, repo_in_overlay


@pytest.mark.parametrize(
    ("declared", "resolved_slug", "overlay_owner", "expected"),
    [
        ("acme-product", "attacker-org/acme-product", "acme-org", False),
        ("acme-product", "acme-org/acme-product", "acme-org", True),
        ("acme-product", "acme-org/acme-product", None, True),
        ("acme-org/acme-product", "acme-org/acme-product", None, True),
        ("acme-org/acme-product", "attacker-org/acme-product", "acme-org", False),
        ("acme-product", "acme-org/other", "acme-org", False),
    ],
)
def test_repo_entry_matches_is_owner_aware(
    declared: str,
    resolved_slug: str,
    overlay_owner: str | None,
    *,
    expected: bool,
) -> None:
    assert repo_entry_matches(declared, resolved_slug, overlay_owner=overlay_owner) is expected


def test_bare_declared_does_not_match_foreign_owner() -> None:
    overlay_repos = ["acme-org/svc", "acme-product"]
    assert repo_in_overlay("attacker-org/acme-product", overlay_repos) is False


def test_bare_declared_matches_when_owner_declared_elsewhere() -> None:
    overlay_repos = ["acme-org/svc", "acme-product"]
    assert repo_in_overlay("acme-org/acme-product", overlay_repos) is True


def test_qualified_declared_matches_exact_slug() -> None:
    assert repo_in_overlay("acme-org/acme-product", ["acme-org/acme-product"]) is True


def test_bare_only_overlay_keeps_lenient_match() -> None:
    # An overlay that declares no owner anywhere keeps the lenient bare-name
    # scope: a resolved owner/repo whose repo segment matches still belongs.
    assert repo_in_overlay("acme-org/widgets", ["widgets"]) is True


def test_followup_plus_get_repos_shape_scopes_bare_name() -> None:
    overlay_repos = ["souliane/teatree", "teatree"]
    assert repo_in_overlay("souliane/teatree", overlay_repos) is True
    assert repo_in_overlay("attacker-org/teatree", overlay_repos) is False


def test_empty_resolved_slug_never_matches() -> None:
    assert repo_in_overlay("", ["souliane/teatree"]) is False
