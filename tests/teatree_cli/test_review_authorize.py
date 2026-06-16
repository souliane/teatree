r"""One-step ``t3 review authorize`` collapse for the live-post gate (#126).

Before this command, getting one live, colleague-visible comment out under
the user's identity required TWO separate user actions plus the post:

1. ``t3 review approve-on-behalf <repo>!<mr> post_comment --approver <id>``
    (records the durable :class:`OnBehalfApproval`);
2. ``t3 review approve-live-post <mr-url> --from-on-behalf``
    (mints the single-use :class:`LivePostApproval` from it);
3. ``t3 review post-comment <mr-url> ... --live``
    (consumes BOTH tokens).

The two-token dance is the friction the user is blocked on: one logical
"yes, post this" demands two ceremony commands in the right order. This
collapses steps 1+2 into a single ``t3 review authorize <repo>!<mr>
--approver <id>`` that records exactly one durable authorization which
satisfies the live-post chokepoint, and a consolidated
:func:`teatree.cli.review.authorize.resolve_live_authorization` helper the
post path consults.

The matrix asserts:

* ``authorize`` writes the durable authorization in ONE command and a
    following ``post-comment --live`` succeeds with no second command;
* ``resolve_live_authorization`` returns OK from (a) a recorded
    authorization, (b) IMMEDIATE on-behalf mode, (c) an auto-minted
    Slack-DM approval; and an actionable refusal otherwise;
* the genuine guard survives: no authorization of any kind → blocked.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.review import ReviewService
from teatree.cli.review.authorize import resolve_live_authorization
from teatree.config import OnBehalfPostMode
from teatree.core.models.live_post_approval import LivePostApproval
from teatree.core.models.on_behalf_approval import OnBehalfApproval

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_runner = CliRunner()


def _write_cfg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: OnBehalfPostMode = OnBehalfPostMode.DRAFT_OR_ASK,
    user_id: str = "U-OPERATOR",
) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f'[teatree]\nslack_user_id = "{user_id}"\non_behalf_post_mode = "{mode.value}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


class TestAuthorizeCommand:
    """``t3 review authorize`` records one durable authorization."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch)
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_authorize_writes_one_on_behalf_approval(self) -> None:
        result = _runner.invoke(
            app,
            ["review", "authorize", "org/repo!7", "--approver", "U-OPERATOR"],
        )

        assert result.exit_code == 0, result.output
        assert "OK" in result.output
        # Exactly one durable on-behalf authorization scoped to the MR + post_comment.
        approvals = OnBehalfApproval.objects.filter(target="org/repo!7", action="post_comment")
        assert approvals.count() == 1

    def test_authorize_accepts_gitlab_url(self) -> None:
        result = _runner.invoke(
            app,
            [
                "review",
                "authorize",
                "https://gitlab.com/org/repo/-/merge_requests/7",
                "--approver",
                "U-OPERATOR",
            ],
        )

        assert result.exit_code == 0, result.output
        assert OnBehalfApproval.objects.filter(target="org/repo!7", action="post_comment").count() == 1

    def test_authorize_refuses_self_authorizing_agent(self) -> None:
        # The executing coding-agent role can never self-authorize (maker!=checker).
        result = _runner.invoke(
            app,
            ["review", "authorize", "org/repo!7", "--approver", "coding-agent"],
        )

        assert result.exit_code == 1
        assert "Refused" in result.output
        assert OnBehalfApproval.objects.count() == 0

    def test_authorize_enables_live_post_in_one_step(self) -> None:
        # The whole point: ONE authorize command, then post-comment --live
        # succeeds — no separate approve-live-post step.
        result = _runner.invoke(
            app,
            ["review", "authorize", "org/repo!7", "--approver", "U-OPERATOR"],
        )
        assert result.exit_code == 0, result.output

        error = resolve_live_authorization(scope="org/repo!7", action="post_comment")
        assert error == "", error


class TestResolveLiveAuthorization:
    """The consolidated helper the live-post path consults."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_recorded_authorization_returns_ok(self) -> None:
        _write_cfg(self.tmp_path, self.monkeypatch)
        OnBehalfApproval.record(target="org/repo!7", action="post_comment", approver_id="U-OPERATOR")

        assert resolve_live_authorization(scope="org/repo!7", action="post_comment") == ""

    def test_immediate_mode_returns_ok_without_any_row(self) -> None:
        # Under IMMEDIATE on-behalf mode no token is needed at all — the
        # user has globally opted into autonomous posting.
        _write_cfg(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.IMMEDIATE)

        assert resolve_live_authorization(scope="org/repo!7", action="post_comment") == ""
        assert LivePostApproval.objects.count() == 0
        assert OnBehalfApproval.objects.count() == 0

    def test_no_authorization_returns_actionable_refusal(self) -> None:
        _write_cfg(self.tmp_path, self.monkeypatch)

        error = resolve_live_authorization(scope="org/repo!7", action="post_comment")
        assert error != ""
        # Refusal names the single one-step command, not the old two-step dance.
        assert "authorize" in error

    def test_authorization_for_other_mr_does_not_satisfy(self) -> None:
        _write_cfg(self.tmp_path, self.monkeypatch)
        OnBehalfApproval.record(target="org/repo!1", action="post_comment", approver_id="U-OPERATOR")

        assert resolve_live_authorization(scope="org/repo!2", action="post_comment") != ""


class TestPostCommentLiveOneStep:
    """A single ``authorize`` lets ``post-comment --live`` publish (no second command)."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch)
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_post_comment_live_succeeds_after_one_authorize(self) -> None:
        published: list[tuple[str, int, str]] = []

        def _fake_impl(_self: object, repo: str, mr: int, note: str, **_kwargs: object) -> tuple[str, int]:
            published.append((repo, mr, note))
            return "OK posted note id=1", 0

        # Neutralize the colleague-MR shape + evidence sibling gates so the
        # test isolates the on-behalf/live-post collapse.
        self.monkeypatch.setattr(ReviewService, "_post_comment_impl", _fake_impl)
        self.monkeypatch.setattr("teatree.cli.review.service.check_review_shape", lambda **kwargs: "")
        self.monkeypatch.setattr("teatree.cli.review.service.check_todo_anchor", lambda **kwargs: "")
        self.monkeypatch.setattr("teatree.cli.review.service.check_finding_evidence", lambda **kwargs: "")
        self.monkeypatch.setattr(ReviewService, "_get_api", lambda self: object())

        authorize = _runner.invoke(
            app,
            ["review", "authorize", "org/repo!7", "--approver", "U-OPERATOR"],
        )
        assert authorize.exit_code == 0, authorize.output

        service = ReviewService(token="t")
        msg, code = service.post_comment("org/repo", 7, "LGTM, nice work", live=True)

        assert code == 0, msg
        assert published == [("org/repo", 7, "LGTM, nice work")]

    def test_post_comment_live_blocked_without_authorize(self) -> None:
        self.monkeypatch.setattr("teatree.cli.review.service.check_review_shape", lambda **kwargs: "")
        self.monkeypatch.setattr("teatree.cli.review.service.check_todo_anchor", lambda **kwargs: "")
        self.monkeypatch.setattr("teatree.cli.review.service.check_finding_evidence", lambda **kwargs: "")
        self.monkeypatch.setattr(ReviewService, "_get_api", lambda self: object())

        service = ReviewService(token="t")
        msg, code = service.post_comment("org/repo", 7, "LGTM", live=True)

        assert code == 1
        assert "authorize" in msg
