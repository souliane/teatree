"""``t3 <overlay> review apply-reviewer-policy`` — the owner-invoked catch-up pass.

The command is the only sanctioned way to put the configured reviewer on an MR
that was already open. It carries no username and no author option, so it cannot
be aimed anywhere the standing policy does not already point.
"""

import inspect
from functools import partial
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from teatree.core.management.commands import _reviewer_policy_commands as policy_commands
from teatree.core.overlay import OverlayConfig
from teatree.core.review.reviewer_policy import ReviewerPolicyError, ReviewerPolicyRow

BOT = "acme-factory-bot"
OWNER = "octocat"
REMOTE = "https://gitlab.com/acme-eng/factory.git"


def _mr(iid: int, *, author: str, reviewers: tuple[str, ...] = ()) -> dict:
    return {
        "iid": iid,
        "web_url": f"https://gitlab.com/acme-eng/factory/-/merge_requests/{iid}",
        "author": {"username": author},
        "reviewers": [{"username": name} for name in reviewers],
    }


def _overlay() -> MagicMock:
    overlay = MagicMock()
    overlay.config.pr_auto_reviewers = [OWNER]
    overlay.config.get_gitlab_token.return_value = "owner-token"
    overlay.config.get_gitlab_token_for_remote.return_value = "bot-token"
    overlay.config.acts_as_distinct_identity_on.side_effect = partial(
        OverlayConfig.acts_as_distinct_identity_on,
        overlay.config,
    )
    return overlay


class FakeHost:
    """A real class, not a mock: the command's capability check is a static-attribute probe."""

    def __init__(self, mrs: list[dict], *, assign_ok: bool = True) -> None:
        self._mrs = mrs
        self._assign_ok = assign_ok
        self.assigned: list[tuple[str, str]] = []

    def current_user(self) -> str:
        return BOT

    def list_prs(self, *, repo: str, state: str = "", author: str = "") -> list[dict]:
        del repo, state, author
        return self._mrs

    def assign_reviewer(self, *, pr_url: str, username: str) -> bool:
        self.assigned.append((pr_url, username))
        return self._assign_ok


def _host(mrs: list[dict], *, assign_ok: bool = True) -> FakeHost:
    return FakeHost(mrs, assign_ok=assign_ok)


def _run(
    mrs: list[dict],
    *args: str,
    assign_ok: bool = True,
    overlay: MagicMock | None = None,
) -> list[ReviewerPolicyRow]:
    host = _host(mrs, assign_ok=assign_ok)
    with (
        patch.object(policy_commands, "get_overlay", return_value=overlay or _overlay()),
        patch.object(policy_commands.git, "remote_url", return_value=REMOTE),
        patch.object(policy_commands, "get_code_host_for_repo", return_value=host),
    ):
        return cast("list[ReviewerPolicyRow]", call_command("review", "apply-reviewer-policy", *args))


class TestReporting:
    def test_reports_a_row_per_open_mr(self) -> None:
        rows = _run([_mr(76, author=BOT), _mr(87, author=OWNER)])

        assert [row.outcome for row in rows] == ["assigned", "refused"]

    def test_an_already_reviewed_mr_is_unchanged_not_an_error(self) -> None:
        rows = _run([_mr(76, author=BOT, reviewers=(OWNER,))])

        assert rows[0].outcome == "unchanged"


class TestExitCode:
    def test_a_failed_assignment_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _run([_mr(76, author=BOT)], assign_ok=False)

        assert exc_info.value.code == 1

    def test_a_whole_run_refusal_exits_nonzero(self) -> None:
        overlay = _overlay()
        overlay.config.get_gitlab_token_for_remote.return_value = "owner-token"

        with pytest.raises(SystemExit) as exc_info:
            _run([_mr(76, author=BOT)], overlay=overlay)

        assert exc_info.value.code == 1

    def test_a_refused_mr_alone_does_not_fail_the_run(self) -> None:
        """An out-of-scope MR is the scope check working, not the command failing."""
        rows = _run([_mr(87, author=OWNER)])

        assert [row.outcome for row in rows] == ["refused"]


class TestNoArbitraryTarget:
    def test_the_command_exposes_no_username_option(self) -> None:
        parameters = inspect.signature(policy_commands.ReviewerPolicyCommands.apply_reviewer_policy).parameters

        assert not [name for name in parameters if "review" in name or "user" in name]

    def test_dry_run_writes_nothing(self) -> None:
        host = _host([_mr(76, author=BOT)])
        with (
            patch.object(policy_commands, "get_overlay", return_value=_overlay()),
            patch.object(policy_commands.git, "remote_url", return_value=REMOTE),
            patch.object(policy_commands, "get_code_host_for_repo", return_value=host),
        ):
            call_command("review", "apply-reviewer-policy", "--dry-run")

        assert host.assigned == []


class TestPolicyErrorSurfacesTheReason:
    def test_the_refusal_reason_reaches_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch.object(policy_commands, "get_overlay", return_value=_overlay()),
            patch.object(policy_commands.git, "remote_url", return_value=REMOTE),
            patch.object(policy_commands, "get_code_host_for_repo", return_value=_host([])),
            patch.object(
                policy_commands,
                "apply_reviewer_policy",
                side_effect=ReviewerPolicyError("no pr_auto_reviewers configured"),
            ),
            pytest.raises(SystemExit),
        ):
            call_command("review", "apply-reviewer-policy")

        assert "no pr_auto_reviewers configured" in capsys.readouterr().err
