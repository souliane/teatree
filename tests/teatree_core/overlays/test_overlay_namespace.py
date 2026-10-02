"""Attribution by an overlay's DECLARED forge namespace is host-exact.

``owned_repos`` is forge-host-keyed, and the whole point of that key is that a
namespace declared on one forge says nothing about a same-named namespace on
another. These pin that the matcher honours it — a ``github.com`` declaration
never attributes a GitLab post — plus the three ways it declines to answer (a
tie, a segment-boundary near-miss, a wildcard scope).

The matcher is pure: the declarations are handed in as a literal, so nothing here
patches a registry.
"""

from teatree.core.overlays.overlay_namespace import OverlayScopes, namespace_owner


def _scopes(declarations: dict[str, dict[str, list[str]]]) -> OverlayScopes:
    """``(overlay, owned_repos)`` pairs, spelled as the registry would hand them over."""
    return sorted(declarations.items())


class TestOneOverlayOwnsTheNamespaceOnThatForge:
    def test_a_repo_the_table_never_listed_resolves_to_its_group_owner(self) -> None:
        scopes = _scopes({"acme": {"gitlab.com": ["acme-eng"]}, "other": {"github.com": ["someone"]}})
        assert namespace_owner("acme-eng/repo-created-yesterday", scopes, forge="gitlab") == "acme"

    def test_a_subgroup_repo_resolves_to_the_group_owner(self) -> None:
        scopes = _scopes({"acme": {"gitlab.com": ["acme-eng"]}})
        assert namespace_owner("acme-eng/platform/widget", scopes, forge="gitlab") == "acme"

    def test_the_namespace_may_itself_name_a_subgroup(self) -> None:
        scopes = _scopes({"acme": {"gitlab.com": ["acme-eng/platform"]}})
        assert namespace_owner("acme-eng/platform/widget", scopes, forge="gitlab") == "acme"
        assert namespace_owner("acme-eng/other/widget", scopes, forge="gitlab") == ""

    def test_a_self_hosted_gitlab_host_is_a_gitlab_declaration(self) -> None:
        scopes = _scopes({"acme": {"gitlab.corp.example": ["acme-eng"]}})
        assert namespace_owner("acme-eng/widget", scopes, forge="gitlab") == "acme"

    def test_the_slug_case_does_not_change_the_owner(self) -> None:
        scopes = _scopes({"acme": {"gitlab.com": ["acme-eng"]}})
        assert namespace_owner("ACME-Eng/Widget", scopes, forge="gitlab") == "acme"


class TestTheMatchIsHostExact:
    def test_a_github_namespace_never_attributes_a_gitlab_post(self) -> None:
        # The declaration IS the operator saying where that namespace lives. Read
        # host-blind, the sole `souliane` claim (github.com) would have answered
        # for `gitlab.com/souliane/*` — a forge its owner puts out of scope.
        scopes = _scopes({"gh-only": {"github.com": ["souliane"]}})
        assert namespace_owner("souliane/anything", scopes, forge="gitlab") == ""
        assert namespace_owner("souliane/anything", scopes, forge="github") == "gh-only"

    def test_only_the_declaration_on_the_addressed_forge_answers(self) -> None:
        scopes = _scopes({"gl": {"gitlab.com": ["shared"]}, "gh": {"github.com": ["shared"]}})
        assert namespace_owner("shared/widget", scopes, forge="gitlab") == "gl"
        assert namespace_owner("shared/widget", scopes, forge="github") == "gh"

    def test_an_unclassifiable_host_declaration_attributes_nothing(self) -> None:
        scopes = _scopes({"acme": {"code.corp.example": ["acme-eng"]}})
        assert namespace_owner("acme-eng/widget", scopes, forge="gitlab") == ""

    def test_an_unrecognised_forge_attributes_nothing(self) -> None:
        scopes = _scopes({"acme": {"gitlab.com": ["acme-eng"]}})
        assert namespace_owner("acme-eng/widget", scopes, forge="") == ""
        assert namespace_owner("acme-eng/widget", scopes, forge="bitbucket") == ""


class TestTheTierDeclinesRatherThanGuesses:
    def test_a_namespace_two_overlays_claim_on_one_forge_is_a_tie(self) -> None:
        scopes = _scopes({"one": {"gitlab.com": ["acme-eng"]}, "two": {"gitlab.com": ["acme-eng"]}})
        assert namespace_owner("acme-eng/widget", scopes, forge="gitlab") == ""

    def test_the_match_is_segment_bounded(self) -> None:
        scopes = _scopes({"acme": {"gitlab.com": ["acme-eng"]}})
        assert namespace_owner("acme-eng-fork/widget", scopes, forge="gitlab") == ""
        assert namespace_owner("open-acme-eng/widget", scopes, forge="gitlab") == ""
        assert namespace_owner("elsewhere/acme-eng", scopes, forge="gitlab") == ""

    def test_the_whole_host_wildcard_is_dropped_before_matching(self) -> None:
        # ``["*"]`` means "the whole host" to the SCOPE gate; as an ATTRIBUTION
        # namespace it would claim every slug on the forge, so it never matches
        # and an overlay declaring only it contributes nothing.
        scopes = _scopes({"acme": {"gitlab.com": ["*", "acme-eng"]}, "wild": {"gitlab.com": ["*"]}})
        assert namespace_owner("anyone/anything", scopes, forge="gitlab") == ""
        assert namespace_owner("acme-eng/widget", scopes, forge="gitlab") == "acme"

    def test_an_overlay_declaring_no_scope_at_all_contributes_nothing(self) -> None:
        scopes = _scopes({"acme": {"gitlab.com": ["acme-eng"]}, "silent": {}})
        assert namespace_owner("acme-eng/widget", scopes, forge="gitlab") == "acme"
