"""The note-lifecycle commands bind their service to the repo they act on.

``t3 review``'s seven draft/note-management commands each take the target repo as
their first argument, and that slug is the overlay-resolution context
``_require_token`` reads BOTH the GitLab credential and the API base URL through.
Passing it is load-bearing, not decoration: on an install carrying several
overlays the ambient lookup raises ``Multiple overlays found``, which empties the
credential and makes the addressing read REFUSE — so a command that resolves
ambiently dies before it reaches the wire, and every note lifecycle operation on
that install goes with it (souliane/teatree#1814 class).

Driven through the real ``t3 review`` typer surface against the multi-overlay
install the ``two_overlays`` conftest fixture registers, so a call site that stops
threading its repo fails here instead of only on the operator's machine.
"""

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.review.service import ReviewService
from teatree.core.overlay import OverlayBase

_runner = CliRunner()

# Command name, the ``ReviewService`` method it delegates to, and the arguments
# that follow the repo — one row per command ``drafts.register`` wires up.
_NOTE_LIFECYCLE_COMMANDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("delete-draft-note", "delete_draft_note", ("7", "42")),
    ("delete-discussion", "delete_discussion", ("7", "42")),
    ("delete-issue-note", "delete_issue_note", ("7", "42")),
    ("publish-draft-notes", "publish_draft_notes", ("7",)),
    ("list-draft-notes", "list_draft_notes", ("7",)),
    ("update-note", "update_note", ("7", "42", "revised body")),
    ("resolve-discussion", "resolve_discussion", ("7", "b0a1c2d3")),
)


def _record_binding(monkeypatch: pytest.MonkeyPatch, method: str) -> dict[str, object]:
    """Stand in for one service method, recording what the command bound it to.

    The stand-in resolves the base URL itself rather than asserting on the repo
    alone: the credential read DEGRADES to an empty value while the addressing
    read REFUSES, so only exercising the second one proves the command could
    actually have reached its GitLab instance. Everything below the method — the
    on-behalf gate, the HTTP call — is out of scope here and stays unexercised.
    """
    captured: dict[str, object] = {}

    def _capture(self: ReviewService, repo: str, *_args: object, **_kwargs: object) -> tuple[str, int]:
        captured.update(
            token=self.token,
            bound_repo=self.repo,
            acted_on_repo=repo,
            base_url=self._resolve_base_url(),
        )
        return "OK", 0

    monkeypatch.setattr(ReviewService, method, _capture)
    return captured


@pytest.mark.usefixtures("no_glab_login")
class TestEveryNoteLifecycleCommandResolvesFromItsTargetRepo:
    @pytest.mark.parametrize(
        ("command", "method", "trailing_args"),
        _NOTE_LIFECYCLE_COMMANDS,
        ids=[command for command, _, _ in _NOTE_LIFECYCLE_COMMANDS],
    )
    def test_the_service_carries_the_owning_overlays_credential_and_instance(
        self,
        two_overlays: dict[str, OverlayBase],
        monkeypatch: pytest.MonkeyPatch,
        command: str,
        method: str,
        trailing_args: tuple[str, ...],
    ) -> None:
        captured = _record_binding(monkeypatch, method)

        result = _runner.invoke(app, ["review", command, "acme/alpha", *trailing_args])

        assert result.exit_code == 0, result.output
        assert captured == {
            "token": "glpat-ALPHA",
            "bound_repo": "acme/alpha",
            "acted_on_repo": "acme/alpha",
            "base_url": two_overlays["alpha"].config.gitlab_url,
        }
