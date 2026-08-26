"""The author-side carve-out: an owner-authored MR reply posts; nothing else widens (#960).

The owner's recorded decision (2026-07-23) is narrow: replying to a reviewer
on an MR the OWNER AUTHORED posts autonomously — no draft-first, no per-reply
approval. Every other on-behalf post stays gated, including on that same MR.

The four directions this pins, because proving only the first would have
REMOVED a control rather than encoded a carve-out:

1.  author-side reply + owner-authored MR → publishes with NO approval;
2.  the same reply on a COLLEAGUE-authored MR → still refused;
3.  every other on-behalf action on the OWNER'S OWN MR → still refused;
4.  the receipt DM still fires on the carved-out post.

Plus the fail-CLOSED direction: an unreadable author, an unresolvable
posting identity, or a raising forge all keep the reply gated.
"""

from pathlib import Path

import pytest

from teatree.cli.review import ReviewService
from teatree.cli.review.own_mr import owner_authored_mr
from teatree.config import OnBehalfPostMode
from teatree.core.models import BotPing, ConfigSetting, OnBehalfApproval
from teatree.on_behalf_gate import OnBehalfContext, OnBehalfVerdict, resolve_on_behalf_verdict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_OWNER = "repo-owner"
_COLLEAGUE = "someone.else"
_REPO = "org/product"
_MR = 42
_TARGET = f"{_REPO}!{_MR}"

_BLOCKING_MODES = [OnBehalfPostMode.ASK, OnBehalfPostMode.DRAFT_OR_ASK]


class _AuthoredStubAPI:
    """GitLab API stand-in whose MR carries an explicit author, recording every call."""

    def __init__(self, *, author: str, identity: str = _OWNER) -> None:
        self._author = author
        self._identity = identity
        self.calls: list[tuple[str, str]] = []

    def current_username(self) -> str:
        return self._identity

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint))
        if endpoint.endswith(f"/merge_requests/{_MR}"):
            return {"id": _MR, "iid": _MR, "author": {"username": self._author}} if self._author else {"id": _MR}
        if endpoint.endswith("/draft_notes"):
            return []
        if endpoint.rstrip("/").rsplit("/", 1)[-1].isdigit():
            return {"id": 1, "resolvable": True, "resolved": True}
        if endpoint.endswith("/approvals"):
            return {"approved_by": []}
        return []

    def get_json_paginated(self, endpoint: str) -> list[object]:
        self.calls.append(("get_json_paginated", endpoint))
        return []

    def post_json(self, endpoint: str, payload: object) -> dict[str, object]:
        self.calls.append(("post_json", endpoint))
        del payload
        return {"id": 1, "web_url": f"https://gitlab.example.com/{_REPO}/-/merge_requests/{_MR}#note_1"}

    def post_status(self, endpoint: str) -> int:
        self.calls.append(("post_status", endpoint))
        return 200

    def put_status(self, endpoint: str, payload: object | None = None) -> int:
        self.calls.append(("put_status", endpoint))
        del payload
        return 200

    def delete(self, endpoint: str) -> int:
        self.calls.append(("delete", endpoint))
        return 204

    @property
    def published(self) -> list[str]:
        return [endpoint for kind, endpoint in self.calls if kind in {"post_json", "put_status", "delete"}]


def _service(api: object) -> ReviewService:
    service = ReviewService(token="t", repo=_REPO)
    service._get_api = lambda: api  # type: ignore[method-assign, return-value]
    return service


def _mode(mode: OnBehalfPostMode) -> None:
    ConfigSetting.objects.set_value("on_behalf_post_mode", mode.value)


class TestAuthorSideReplyOnOwnMrPosts:
    """Direction 1 — the carve-out the owner granted."""

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_reply_on_owner_authored_mr_publishes_without_any_approval(self, mode: OnBehalfPostMode) -> None:
        """RED before the carve-out: this BLOCKed with `on_behalf_post_mode (#960)`."""
        _mode(mode)
        api = _AuthoredStubAPI(author=_OWNER)

        msg, code = _service(api).reply_to_discussion(_REPO, _MR, "d1", "Deleted, with both comment references.")

        assert code == 0, msg
        assert any("discussions/d1/notes" in endpoint for endpoint in api.published), api.published
        assert not OnBehalfApproval.objects.exists(), "the carve-out must consume no approval"

    def test_reply_recognises_an_mr_authored_under_a_configured_alias(self) -> None:
        """A secondary forge handle in ``user_identity_aliases`` is still the owner's own work."""
        _mode(OnBehalfPostMode.DRAFT_OR_ASK)
        ConfigSetting.objects.set_value("user_identity_aliases", ["owner-secondary-handle"])
        api = _AuthoredStubAPI(author="owner-secondary-handle", identity="some-other-login")

        _, code = _service(api).reply_to_discussion(_REPO, _MR, "d1", "Fixed.")

        assert code == 0


class TestColleagueAuthoredMrStillRefused:
    """Direction 2 — the control the carve-out must NOT remove."""

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_reply_on_colleague_authored_mr_is_still_blocked(self, mode: OnBehalfPostMode) -> None:
        _mode(mode)
        api = _AuthoredStubAPI(author=_COLLEAGUE)

        msg, code = _service(api).reply_to_discussion(_REPO, _MR, "d1", "Fixed.")

        assert code == 1
        assert "approve-on-behalf" in msg
        assert api.published == [], "no GitLab write may be attempted on a colleague's MR"

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_colleague_mr_reply_still_publishes_with_a_recorded_approval(self, mode: OnBehalfPostMode) -> None:
        """The pre-existing satisfier is untouched — the carve-out adds a path, it replaces none."""
        _mode(mode)
        OnBehalfApproval.record(target=_TARGET, action="reply_to_discussion", approver_id="souliane")
        api = _AuthoredStubAPI(author=_COLLEAGUE)

        _, code = _service(api).reply_to_discussion(_REPO, _MR, "d1", "Fixed.")

        assert code == 0


class TestOtherOnBehalfSurfacesStillRefusedOnOwnMr:
    """Direction 3 — owner authorship exempts the REPLY, and nothing else."""

    @pytest.fixture(autouse=True)
    def _own_mr(self) -> None:
        _mode(OnBehalfPostMode.DRAFT_OR_ASK)
        self.api = _AuthoredStubAPI(author=_OWNER)
        self.service = _service(self.api)

    def test_approve_on_own_mr_is_still_blocked(self) -> None:
        msg, code = self.service.approve(_REPO, _MR)

        assert code == 1
        assert "approve-on-behalf" in msg
        assert self.api.published == []

    def test_unapprove_on_own_mr_is_still_blocked(self) -> None:
        msg, code = self.service.unapprove(_REPO, _MR)

        assert code == 1
        assert "approve-on-behalf" in msg
        assert self.api.published == []

    def test_publish_draft_notes_on_own_mr_is_still_blocked(self) -> None:
        msg, code = self.service.publish_draft_notes(_REPO, _MR)

        assert code == 1
        assert "approve-on-behalf" in msg
        assert self.api.published == []

    def test_resolve_discussion_on_own_mr_is_still_blocked(self) -> None:
        msg, code = self.service.resolve_discussion(_REPO, _MR, "d1")

        assert code == 1
        assert "approve-on-behalf" in msg
        assert self.api.published == []

    def test_update_note_on_own_mr_is_still_blocked(self) -> None:
        msg, code = self.service.update_note(_REPO, _MR, 99, "edited")

        assert code == 1
        assert "approve-on-behalf" in msg
        assert self.api.published == []

    def test_delete_discussion_on_own_mr_is_still_blocked(self) -> None:
        msg, code = self.service.delete_discussion(_REPO, _MR, 99)

        assert code == 1
        assert "approve-on-behalf" in msg
        assert self.api.published == []

    def test_live_comment_on_own_mr_is_still_blocked(self) -> None:
        _, code = self.service.post_comment(_REPO, _MR, "note", live=True)

        assert code == 1
        assert self.api.published == []


class TestReceiptDmStillFires:
    """Direction 4 — autonomy removes the pre-ask, never the owner's visibility."""

    def test_carved_out_reply_still_dms_the_owner(self) -> None:
        _mode(OnBehalfPostMode.DRAFT_OR_ASK)
        api = _AuthoredStubAPI(author=_OWNER)

        _, code = _service(api).reply_to_discussion(_REPO, _MR, "d1", "Fixed.")

        assert code == 0
        assert BotPing.objects.filter(idempotency_key__startswith=f"on_behalf_post:{_TARGET}:reply_to_discussion")


class TestAuthorshipProofFailsClosed:
    """An unproven owner is not an owner — every unreadable input keeps the reply gated."""

    @pytest.mark.parametrize(
        ("author", "identity"),
        [
            pytest.param("", _OWNER, id="mr-payload-carries-no-author"),
            pytest.param(_OWNER, "", id="posting-identity-unresolvable"),
        ],
    )
    def test_unresolvable_authorship_blocks_the_reply(self, author: str, identity: str) -> None:
        _mode(OnBehalfPostMode.DRAFT_OR_ASK)
        api = _AuthoredStubAPI(author=author, identity=identity)

        msg, code = _service(api).reply_to_discussion(_REPO, _MR, "d1", "Fixed.")

        assert code == 1
        assert "approve-on-behalf" in msg
        assert api.published == []

    def test_a_raising_forge_read_blocks_the_reply(self, tmp_path: Path) -> None:
        del tmp_path

        class _RaisingAPI(_AuthoredStubAPI):
            def get_json(self, endpoint: str) -> object:
                if endpoint.endswith(f"/merge_requests/{_MR}"):
                    msg = "gitlab is down"
                    raise RuntimeError(msg)
                return super().get_json(endpoint)

        _mode(OnBehalfPostMode.DRAFT_OR_ASK)
        api = _RaisingAPI(author=_OWNER)

        msg, code = _service(api).reply_to_discussion(_REPO, _MR, "d1", "Fixed.")

        assert code == 1
        assert "approve-on-behalf" in msg
        assert api.published == []

    def test_owner_authored_mr_is_false_when_the_identity_lookup_raises(self) -> None:
        class _NoIdentityAPI(_AuthoredStubAPI):
            def current_username(self) -> str:
                msg = "401"
                raise RuntimeError(msg)

        assert owner_authored_mr(_NoIdentityAPI(author=_OWNER), _REPO, _MR) is False


class TestVerdictResolverCarveOutIsActionScoped:
    """The pure resolver's half of the lock: ``own_mr=True`` is inert outside the action set."""

    @pytest.mark.parametrize("action", ["approve", "unapprove", "post_comment", "publish_draft_notes", "update_note"])
    def test_own_mr_does_not_exempt_any_other_action(self, action: str) -> None:
        _mode(OnBehalfPostMode.DRAFT_OR_ASK)

        assert resolve_on_behalf_verdict(action, OnBehalfContext(own_mr=True)) is OnBehalfVerdict.BLOCK

    def test_reply_without_proved_authorship_still_blocks(self) -> None:
        _mode(OnBehalfPostMode.DRAFT_OR_ASK)

        assert resolve_on_behalf_verdict("reply_to_discussion") is OnBehalfVerdict.BLOCK
        assert resolve_on_behalf_verdict("reply_to_discussion", OnBehalfContext(own_mr=True)) is OnBehalfVerdict.PROCEED
