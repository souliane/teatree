"""Predicate that enforces the 4 skip-conditions for review candidates (#1321).

The auto-sweep / discover surfaces previously relied on agent-side BINDING
memory to apply these rules; this module is the canonical structural fix.
"""

from teatree.config import cold_reader
from teatree.core.review.review_candidate import (
    _is_self_authored,
    author_is_self,
    broadcast_claimed_by_other,
    should_review_candidate,
    should_review_candidate_reasons,
)


class _Host:
    def __init__(self, *, current: str = "owner", author: str = "owner", error: Exception | None = None) -> None:
        self.current = current
        self.author = author
        self.error = error
        self.current_user_calls = 0

    def current_user(self) -> str:
        self.current_user_calls += 1
        return self.current

    def get_pr_author(self, *, pr_url: str) -> str:
        _ = pr_url
        if self.error is not None:
            raise self.error
        return self.author


class TestForgeAuthorshipProof:
    def test_declared_bot_author_is_self_on_its_configured_host(self, monkeypatch) -> None:
        monkeypatch.setattr(
            cold_reader,
            "mapping_setting",
            lambda _key: {"gitlab.com": ["factory-bot"]},
        )

        assert _is_self_authored("https://gitlab.com/group/repo/-/merge_requests/1", _Host(author="factory-bot"), ())

    def test_owner_alias_is_self_without_reading_the_credential(self, monkeypatch) -> None:
        host = _Host(current="undeclared-credential", author="owner-alias")
        monkeypatch.setattr(cold_reader, "mapping_setting", lambda _key: {})

        assert _is_self_authored("https://gitlab.com/group/repo/-/merge_requests/1", host, ("owner-alias",))
        assert host.current_user_calls == 0

    def test_undeclared_current_credential_is_not_self(self, monkeypatch) -> None:
        monkeypatch.setattr(cold_reader, "mapping_setting", lambda _key: {})
        host = _Host(current="credential-owner", author="credential-owner")

        assert _is_self_authored("https://gitlab.com/group/repo/-/merge_requests/1", host, ()) is False

    def test_empty_aliases_and_no_configured_bot_is_not_self(self, monkeypatch) -> None:
        monkeypatch.setattr(cold_reader, "mapping_setting", lambda _key: {})
        host = _Host(current="credential-owner", author="credential-owner")

        assert _is_self_authored("https://gitlab.com/group/repo/-/merge_requests/1", host, ()) is False

    def test_missing_alias_key_still_honours_the_declared_bot(self, monkeypatch) -> None:
        monkeypatch.setattr(
            cold_reader,
            "mapping_setting",
            lambda _key: {"gitlab.com": ["factory-bot"]},
        )

        assert _is_self_authored("https://gitlab.com/group/repo/-/merge_requests/1", _Host(author="factory-bot"), ())

    def test_unknown_host_does_not_inherit_another_hosts_bot(self, monkeypatch) -> None:
        monkeypatch.setattr(
            cold_reader,
            "mapping_setting",
            lambda _key: {"gitlab.com": ["factory-bot"]},
        )

        host = _Host(current="factory-bot", author="factory-bot")

        assert _is_self_authored("https://forge.example/group/repo/pull/1", host, ()) is False

    def test_unreadable_author_or_host_stays_unresolved(self, monkeypatch) -> None:
        monkeypatch.setattr(cold_reader, "mapping_setting", lambda _key: {})

        assert (
            _is_self_authored("https://gitlab.com/group/repo/-/merge_requests/1", _Host(author=""), ("owner",)) is None
        )
        assert (
            _is_self_authored(
                "https://gitlab.com/group/repo/-/merge_requests/1",
                _Host(error=RuntimeError("down")),
                ("owner",),
            )
            is None
        )
        assert _is_self_authored("https://gitlab.com/group/repo/-/merge_requests/1", None, ("owner",)) is None

        credential_host = _Host(current="credential-owner", author="credential-owner")
        assert _is_self_authored("https://[unreadable", credential_host, ()) is False


class TestAuthorIsSelf:
    def test_matches_primary_identity(self) -> None:
        assert author_is_self("alice", current_user="alice") is True

    def test_matches_secondary_alias(self) -> None:
        assert author_is_self("alice-gh", current_user="alice", self_identities=("alice-gh",)) is True

    def test_does_not_match_colleague(self) -> None:
        assert author_is_self("bob", current_user="alice", self_identities=("alice-gh",)) is False

    def test_empty_author_never_matches(self) -> None:
        assert author_is_self("", current_user="alice", self_identities=("alice-gh",)) is False

    def test_no_identities_never_matches(self) -> None:
        assert author_is_self("alice", current_user="") is False


class TestSkipSelfAuthor:
    def test_skip_when_gitlab_author_is_current_user(self) -> None:
        mr = {"author": {"username": "alice"}, "state": "opened", "notes": []}
        assert should_review_candidate(mr, current_user="alice") is False
        assert "author_is_self" in should_review_candidate_reasons(mr, current_user="alice")

    def test_skip_when_github_user_is_current_user(self) -> None:
        mr = {"user": {"login": "alice"}, "state": "open"}
        assert should_review_candidate(mr, current_user="alice") is False

    def test_keeps_mr_authored_by_another_user(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened", "notes": []}
        assert should_review_candidate(mr, current_user="alice") is True


class TestSkipAlreadyApproved:
    def test_skip_when_mr_approved_flag_set_and_user_in_approvers(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "approved": True,
            "approvers": [{"username": "alice"}],
        }
        assert should_review_candidate(mr, current_user="alice") is False
        assert "already_approved_by_self" in should_review_candidate_reasons(mr, current_user="alice")

    def test_skip_when_current_user_in_approvers_even_without_approved_flag(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "approvers": [{"username": "alice"}, {"username": "carol"}],
        }
        assert should_review_candidate(mr, current_user="alice") is False

    def test_keeps_mr_approved_only_by_others(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "approved": True,
            "approvers": [{"username": "carol"}],
        }
        assert should_review_candidate(mr, current_user="alice") is True


class TestSkipMyNoteExists:
    def test_skip_when_current_user_has_non_system_note(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "notes": [
                {"author": {"username": "alice"}, "system": False, "body": "looks good"},
            ],
        }
        assert should_review_candidate(mr, current_user="alice") is False
        assert "has_self_note" in should_review_candidate_reasons(mr, current_user="alice")

    def test_keeps_mr_when_only_system_notes_from_user(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "notes": [
                {"author": {"username": "alice"}, "system": True, "body": "assigned"},
            ],
        }
        assert should_review_candidate(mr, current_user="alice") is True

    def test_keeps_mr_when_only_other_users_commented(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "notes": [
                {"author": {"username": "carol"}, "system": False, "body": "nit"},
            ],
        }
        assert should_review_candidate(mr, current_user="alice") is True


class TestSkipMergedOrClosed:
    def test_skip_merged(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "merged"}
        assert should_review_candidate(mr, current_user="alice") is False
        assert "state_merged" in should_review_candidate_reasons(mr, current_user="alice")

    def test_skip_closed(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "closed"}
        assert should_review_candidate(mr, current_user="alice") is False
        assert "state_closed" in should_review_candidate_reasons(mr, current_user="alice")

    def test_keeps_open_mr(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        assert should_review_candidate(mr, current_user="alice") is True


class TestSkipBroadcastReactedByOthers:
    def test_skip_when_broadcast_has_eyes_from_other_user(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": [{"name": "eyes", "users": ["U_BOB"]}]}
        assert should_review_candidate(mr, current_user="U_ALICE", broadcast=broadcast) is False
        assert "broadcast_reacted_by_other" in should_review_candidate_reasons(
            mr, current_user="U_ALICE", broadcast=broadcast
        )

    def test_skip_when_broadcast_has_white_check_mark_from_other(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": [{"name": "white_check_mark", "users": ["U_OTHER"]}]}
        assert should_review_candidate(mr, current_user="U_ALICE", broadcast=broadcast) is False

    def test_keeps_mr_when_broadcast_reaction_only_from_self(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": [{"name": "eyes", "users": ["U_ALICE"]}]}
        assert should_review_candidate(mr, current_user="U_ALICE", broadcast=broadcast) is True

    def test_keeps_mr_when_broadcast_has_no_reactions(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": []}
        assert should_review_candidate(mr, current_user="U_ALICE", broadcast=broadcast) is True

    def test_keeps_mr_when_broadcast_none(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        assert should_review_candidate(mr, current_user="U_ALICE", broadcast=None) is True


class TestShapeTolerance:
    """Heterogeneous and malformed inputs should not crash the predicate."""

    def test_mr_without_author_or_user_keys(self) -> None:
        mr = {"state": "opened"}
        assert should_review_candidate(mr, current_user="alice") is True

    def test_mr_with_bare_string_approvers(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "approvers": ["alice", "carol"],
        }
        assert should_review_candidate(mr, current_user="alice") is False

    def test_mr_with_login_keyed_approver(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "approvers": [{"login": "alice"}],
        }
        assert should_review_candidate(mr, current_user="alice") is False

    def test_mr_with_name_keyed_approver(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "approvers": [{"name": "alice"}],
        }
        assert should_review_candidate(mr, current_user="alice") is False

    def test_mr_with_non_sequence_approvers(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened", "approvers": "alice"}
        assert should_review_candidate(mr, current_user="alice") is True

    def test_mr_with_non_dict_note_element(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened", "notes": ["bad-shape"]}
        assert should_review_candidate(mr, current_user="alice") is True

    def test_mr_with_non_sequence_notes(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened", "notes": "not-a-list"}
        assert should_review_candidate(mr, current_user="alice") is True

    def test_mr_with_unknown_state_string_is_open(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "draft"}
        assert should_review_candidate(mr, current_user="alice") is True

    def test_mr_with_non_string_state(self) -> None:
        mr = {"author": {"username": "bob"}, "state": 42}
        assert should_review_candidate(mr, current_user="alice") is True

    def test_broadcast_with_non_sequence_reactions(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": "not-a-list"}
        assert should_review_candidate(mr, current_user="alice", broadcast=broadcast) is True

    def test_broadcast_with_non_dict_reaction_entry(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": ["malformed"]}
        assert should_review_candidate(mr, current_user="alice", broadcast=broadcast) is True

    def test_broadcast_with_non_sequence_users(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": [{"name": "eyes", "users": "U_OTHER"}]}
        assert should_review_candidate(mr, current_user="alice", broadcast=broadcast) is True

    def test_broadcast_user_is_empty_string(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reactions": [{"name": "eyes", "users": ["", "U_OTHER"]}]}
        assert should_review_candidate(mr, current_user="alice", broadcast=broadcast) is False

    def test_note_with_non_dict_author(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "notes": [{"author": "alice-as-string", "system": False, "body": "x"}],
        }
        assert should_review_candidate(mr, current_user="alice") is True


_IDENTITIES = ("user-gl", "user-gh-a", "user-gh-b")


class TestSkipSelfAuthorAcrossIdentities:
    """The self-author skip must match ANY of the user's configured identities (#1321).

    A user owns more than one identity (a gitlab username plus one or more
    github logins). A primary-identity-only check lets an MR authored under
    a SECONDARY self-identity slip through as a colleague MR and dispatch
    ``t3:reviewer`` on the user's own work.
    """

    def test_skip_when_author_is_secondary_self_identity(self) -> None:
        mr = {"author": {"username": "user-gh-b"}, "state": "opened", "notes": []}
        reasons = should_review_candidate_reasons(mr, current_user="user-gl", self_identities=_IDENTITIES)
        assert "author_is_self" in reasons
        assert should_review_candidate(mr, current_user="user-gl", self_identities=_IDENTITIES) is False

    def test_skip_when_github_secondary_identity_logins(self) -> None:
        mr = {"user": {"login": "user-gh-a"}, "state": "open"}
        assert should_review_candidate(mr, current_user="user-gl", self_identities=("user-gl", "user-gh-a")) is False

    def test_already_approved_matches_any_self_identity(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "approvers": [{"username": "user-gh-a"}],
        }
        assert should_review_candidate(mr, current_user="user-gl", self_identities=("user-gl", "user-gh-a")) is False

    def test_self_note_matches_any_self_identity(self) -> None:
        mr = {
            "author": {"username": "bob"},
            "state": "opened",
            "notes": [{"author": {"username": "user-gh-a"}, "system": False, "body": "present"}],
        }
        assert should_review_candidate(mr, current_user="user-gl", self_identities=("user-gl", "user-gh-a")) is False

    def test_colleague_mr_still_candidate_with_multiple_identities(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened", "notes": []}
        assert should_review_candidate(mr, current_user="user-gl", self_identities=_IDENTITIES) is True

    def test_current_user_still_honoured_without_explicit_identities(self) -> None:
        mr = {"author": {"username": "user-gl"}, "state": "opened", "notes": []}
        assert should_review_candidate(mr, current_user="user-gl") is False


class TestReasonsAccumulate:
    def test_multiple_skip_reasons_returned(self) -> None:
        mr = {
            "author": {"username": "alice"},
            "state": "merged",
            "approved": True,
            "approvers": [{"username": "alice"}],
            "notes": [{"author": {"username": "alice"}, "system": False, "body": "x"}],
        }
        reasons = should_review_candidate_reasons(mr, current_user="alice")
        assert "author_is_self" in reasons
        assert "already_approved_by_self" in reasons
        assert "has_self_note" in reasons
        assert "state_merged" in reasons

    def test_clean_candidate_returns_empty_reasons(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened", "notes": []}
        assert should_review_candidate_reasons(mr, current_user="alice") == []


class TestClassificationIsAuthorNotNamespace:
    """Own-vs-colleague keys on AUTHOR identity, never on the repo namespace.

    A solo-owned overlay repo (the user's own ``acme-eng/widget-overlay`` /
    ``acme-eng/widget-overlay-e2e``) is mergeable exactly like ``souliane/*``:
    a PR authored by the user there is their OWN work, not a colleague's,
    so the review-sweep skips it with ``author_is_self`` — the same verdict
    it returns for a ``souliane/teatree`` self-authored PR. The only
    namespace axis is VISIBILITY (leak-prevention), which lives in a
    different module and never reaches this predicate.

    These cases pin the distinction so a future change cannot reintroduce
    namespace/org-based colleague classification: the verdict is identical
    whether the carried repo slug is public or a private overlay, and flips
    only when the AUTHOR changes.
    """

    def test_self_authored_overlay_pr_is_own_not_colleague(self) -> None:
        pr = {
            "user": {"login": "souliane"},
            "state": "open",
            "base": {"repo": {"full_name": "acme-eng/widget-overlay", "private": True}},
        }
        reasons = should_review_candidate_reasons(pr, current_user="souliane")
        assert "author_is_self" in reasons
        assert should_review_candidate(pr, current_user="souliane") is False

    def test_self_authored_overlay_e2e_pr_is_own_not_colleague(self) -> None:
        pr = {
            "author": {"username": "souliane"},
            "state": "opened",
            "notes": [],
            "web_url": "https://gitlab.com/acme-eng/widget-overlay-e2e/-/merge_requests/7",
        }
        assert should_review_candidate(pr, current_user="souliane") is False

    def test_verdict_identical_for_public_and_private_namespace_when_self_authored(self) -> None:
        public = {"user": {"login": "souliane"}, "state": "open", "base": {"repo": {"full_name": "souliane/teatree"}}}
        private = {
            "user": {"login": "souliane"},
            "state": "open",
            "base": {"repo": {"full_name": "acme-eng/widget-overlay", "private": True}},
        }
        assert should_review_candidate(public, current_user="souliane") == should_review_candidate(
            private, current_user="souliane"
        )

    def test_colleague_authored_overlay_pr_stays_a_candidate(self) -> None:
        pr = {
            "user": {"login": "a-teammate"},
            "state": "open",
            "base": {"repo": {"full_name": "acme-eng/widget-overlay", "private": True}},
        }
        assert should_review_candidate(pr, current_user="souliane") is True

    def test_private_namespace_does_not_force_a_skip_for_a_colleague_pr(self) -> None:
        pr = {
            "user": {"login": "a-teammate"},
            "state": "open",
            "base": {"repo": {"full_name": "acme-product", "private": True}},
        }
        assert should_review_candidate_reasons(pr, current_user="souliane") == []


class TestBroadcastClaimedByOther:
    """Any reaction or thread reply from outside the self-set is a colleague's claim (#159)."""

    SELF = ("UME", "UBOT")

    def test_eyes_from_colleague_is_a_claim(self) -> None:
        message = {"reactions": [{"name": "eyes", "users": ["UC0LLEAGUE"], "count": 1}]}
        assert broadcast_claimed_by_other(message, self_ids=self.SELF) is True

    def test_any_emoji_from_colleague_is_a_claim(self) -> None:
        message = {"reactions": [{"name": "thumbsup", "users": ["UC0LLEAGUE"], "count": 1}]}
        assert broadcast_claimed_by_other(message, self_ids=self.SELF) is True

    def test_reactions_only_from_the_owner_or_the_bot_are_not_a_claim(self) -> None:
        message = {"reactions": [{"name": "eyes", "users": ["UME"], "count": 1}, {"name": "rocket", "users": ["UBOT"]}]}
        assert broadcast_claimed_by_other(message, self_ids=self.SELF) is False

    def test_colleague_thread_reply_is_a_claim(self) -> None:
        message = {"reply_count": 1, "reply_users": ["UC0LLEAGUE"]}
        assert broadcast_claimed_by_other(message, self_ids=self.SELF) is True

    def test_own_thread_replies_are_not_a_claim(self) -> None:
        message = {"reply_count": 2, "reply_users": ["UME", "UBOT"]}
        assert broadcast_claimed_by_other(message, self_ids=self.SELF) is False

    def test_reply_count_without_reply_users_fails_closed_as_a_claim(self) -> None:
        assert broadcast_claimed_by_other({"reply_count": 1}, self_ids=self.SELF) is True

    def test_no_reactions_and_no_replies_is_not_a_claim(self) -> None:
        assert broadcast_claimed_by_other({}, self_ids=self.SELF) is False

    def test_malformed_blocks_are_not_a_claim(self) -> None:
        assert broadcast_claimed_by_other({"reactions": "nope", "reply_users": "nope"}, self_ids=self.SELF) is False

    def test_malformed_reaction_entry_and_users_are_skipped(self) -> None:
        message = {
            "reactions": [
                "not-a-dict",
                {"name": "eyes", "users": "not-a-list"},
                {"name": "eyes", "users": [42, "", "UME", "UC0LLEAGUE"], "count": 1},
            ],
        }
        assert broadcast_claimed_by_other(message, self_ids=self.SELF) is True

    def test_empty_self_set_treats_every_reactor_as_other(self) -> None:
        message = {"reactions": [{"name": "eyes", "users": ["UC0LLEAGUE"], "count": 1}]}
        assert broadcast_claimed_by_other(message, self_ids=()) is True

    def test_candidate_reasons_read_thread_replies_too(self) -> None:
        mr = {"author": {"username": "bob"}, "state": "opened"}
        broadcast = {"reply_count": 1, "reply_users": ["U_BOB"]}
        assert "broadcast_reacted_by_other" in should_review_candidate_reasons(
            mr, current_user="U_ALICE", broadcast=broadcast
        )
        own_reply = {"reply_count": 1, "reply_users": ["U_ALICE"]}
        assert should_review_candidate(mr, current_user="U_ALICE", broadcast=own_reply) is True


class TestBotReplyIsNotAColleagueClaim:
    """A B…-prefixed Slack bot/app-integration id in reply_users is not a colleague claim.

    Modelled on a live incident: a forge integration's own status reply in the
    broadcast thread (a ``B…``-prefixed bot id, never a human ``U…`` id)
    suppressed dispatch even though no human had claimed the review.
    """

    SELF = ("UOWNER", "UBOTOWN")

    def test_bot_reply_alone_does_not_suppress(self) -> None:
        message = {"reply_count": 1, "reply_users": ["BINTEGRATION"]}
        assert broadcast_claimed_by_other(message, self_ids=self.SELF) is False

    def test_human_colleague_reply_still_suppresses(self) -> None:
        message = {"reply_count": 1, "reply_users": ["UC0LLEAGUE"]}
        assert broadcast_claimed_by_other(message, self_ids=self.SELF) is True

    def test_mixed_bot_and_human_reply_still_suppresses(self) -> None:
        message = {"reply_count": 2, "reply_users": ["BINTEGRATION", "UC0LLEAGUE"]}
        assert broadcast_claimed_by_other(message, self_ids=self.SELF) is True
