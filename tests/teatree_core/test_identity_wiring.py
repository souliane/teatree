"""The deployment's own identity wiring — the two facts that fail silently (#4241 follow-up).

Both defects here reached a human before they reached a check. The reviewer allowlist shipped
empty, so the merge keystone refused the owner's own CLEAR and the merge was done by hand. The
authoring predicate read an unresolvable bot credential as a resolved one, so a venue that could
not reach its bot claimed it was writing under it.
"""

from teatree.core.identity_wiring import (
    AuthoringIdentity,
    authoring_identity_fault,
    classify_authoring_identity,
    derivable_owner_identities,
    owner_identity_fault,
)
from teatree.core.overlay import OverlayConfig


class TestOwnerIdentityFault:
    """An empty reviewer allowlist is a FAULT, not a configuration the operator may have meant."""

    def test_empty_allowlist_is_a_fault(self) -> None:
        fault = owner_identity_fault([])
        assert fault is not None
        assert "merge keystone" in fault.summary

    def test_blank_only_allowlist_is_a_fault(self) -> None:
        assert owner_identity_fault(["", "   "]) is not None

    def test_a_configured_identity_clears_it(self) -> None:
        assert owner_identity_fault(["owner-handle"]) is None

    def test_the_remedy_names_the_command_that_fixes_it(self) -> None:
        fault = owner_identity_fault([])
        assert fault is not None
        assert "t3 identities bootstrap" in fault.remedy


class TestClassifyAuthoringIdentity:
    """An unresolvable scoped credential is its own answer, never 'distinct'."""

    def test_a_resolved_different_credential_is_distinct(self) -> None:
        assert classify_authoring_identity(owner_token="owner", scoped_token="bot") is AuthoringIdentity.DISTINCT

    def test_the_same_credential_is_the_owner(self) -> None:
        assert classify_authoring_identity(owner_token="owner", scoped_token="owner") is AuthoringIdentity.OWNER

    def test_an_empty_scoped_credential_is_unresolvable(self) -> None:
        assert classify_authoring_identity(owner_token="owner", scoped_token="") is AuthoringIdentity.UNRESOLVABLE

    def test_nothing_configured_anywhere_is_the_owner(self) -> None:
        assert classify_authoring_identity(owner_token="", scoped_token="") is AuthoringIdentity.OWNER

    def test_only_unresolvable_raises_a_fault(self) -> None:
        assert authoring_identity_fault(remote="git@host:x/y.git", identity=AuthoringIdentity.OWNER) is None
        assert authoring_identity_fault(remote="git@host:x/y.git", identity=AuthoringIdentity.DISTINCT) is None
        fault = authoring_identity_fault(remote="git@host:x/y.git", identity=AuthoringIdentity.UNRESOLVABLE)
        assert fault is not None
        assert "git@host:x/y.git" in fault.summary


class _ScopedConfig(OverlayConfig):
    """An overlay routing ONE remote to a credential that may or may not resolve."""

    def __init__(self, *, owner: str, scoped: str) -> None:
        super().__init__()
        self._owner = owner
        self._scoped = scoped

    def get_gitlab_token(self) -> str:
        return self._owner

    def get_gitlab_token_for_remote(self, remote: str) -> str:
        return self._scoped if "factory" in remote else self._owner


class TestActsAsDistinctIdentityIsConservative:
    """The regression: an UNRESOLVABLE bot credential used to answer ``True`` (merely unequal).

    Everything that makes the owner a CHECKER is scoped by this predicate, so a ``True`` here
    assigned the owner as reviewer of an MR no credential on the box could author.
    """

    FACTORY = "git@gitlab.com:org/group/factory.git"
    PRODUCT = "git@gitlab.com:org/product.git"

    def test_an_unresolvable_scoped_credential_is_not_distinct(self) -> None:
        config = _ScopedConfig(owner="owner-token", scoped="")
        assert config.authoring_identity_on(self.FACTORY) is AuthoringIdentity.UNRESOLVABLE
        assert config.acts_as_distinct_identity_on(self.FACTORY) is False

    def test_a_resolved_bot_credential_is_distinct(self) -> None:
        config = _ScopedConfig(owner="owner-token", scoped="bot-token")
        assert config.acts_as_distinct_identity_on(self.FACTORY) is True

    def test_an_unscoped_remote_stays_the_owners(self) -> None:
        config = _ScopedConfig(owner="owner-token", scoped="bot-token")
        assert config.authoring_identity_on(self.PRODUCT) is AuthoringIdentity.OWNER
        assert config.acts_as_distinct_identity_on(self.PRODUCT) is False

    def test_an_empty_remote_is_the_owners(self) -> None:
        assert _ScopedConfig(owner="owner-token", scoped="").authoring_identity_on("") is AuthoringIdentity.OWNER


class TestDerivableOwnerIdentities:
    """The bootstrap may never derive an identity the deployment ACTS as (§17.8 clause 3)."""

    def test_a_login_we_also_act_as_is_never_derived(self) -> None:
        assert derivable_owner_identities(forge_logins=["release-bot"], self_identities=["release-bot"]) == ()

    def test_the_exclusion_ignores_case_and_padding(self) -> None:
        derived = derivable_owner_identities(forge_logins=[" Release-Bot "], self_identities=["release-bot"])
        assert derived == ()

    def test_the_owners_own_login_is_derived(self) -> None:
        derived = derivable_owner_identities(forge_logins=["owner-handle"], self_identities=["release-bot"])
        assert derived == ("owner-handle",)

    def test_a_bot_is_dropped_from_a_mixed_set_without_dropping_the_owner(self) -> None:
        derived = derivable_owner_identities(
            forge_logins=["owner-handle", "release-bot", "owner-alt-handle"], self_identities=["release-bot"]
        )
        assert derived == ("owner-handle", "owner-alt-handle")

    def test_blanks_and_duplicates_collapse(self) -> None:
        derived = derivable_owner_identities(forge_logins=["a", "", "  ", "a"], self_identities=[])
        assert derived == ("a",)
