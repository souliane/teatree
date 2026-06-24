"""Tests for the review-comment bloat gate.

The gate refuses a review note that drags in *project chatter* on the
posting path before any GitLab API call — the off-topic-coordination
bloat shape the user flagged on two customer MRs that no sibling gate
covered:

* a stakeholder named by ``@handle``,
* a Slack thread quoted by timestamp, or
* a tracker id (``#1234`` / ``!567``) paired with a coordinate-with-people
    directive (``ping the author``, ``sync with the team``, ``in standup``).

A review comment is about the diff, not the tracker. The note-LENGTH
dimension of bloat is deliberately left to the colleague-MR shape gate
(``teatree.cli.review.shape_gate``); this gate does not duplicate it, and
a bare ``tracked at #1234`` non-blocker pointer (the TODO-gate remediation
form) is explicitly NOT refused.

Service-level tests pin the on-behalf gate to IMMEDIATE and the
colleague-MR shape gate to a no-op (own-MR author) so any blocking is
attributable to the gate under test.
"""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.review import ReviewService
from teatree.cli.review.bloat_gate import check_review_bloat, references_project_chatter
from teatree.config import OnBehalfPostMode
from teatree.core.models import ConfigSetting

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_AUTHOR_ALICE = "alice"


def _gate_immediate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the on-behalf gate to IMMEDIATE so it does NOT also block the call."""
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text("[teatree]\n", encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    ConfigSetting.objects.set_value("on_behalf_post_mode", OnBehalfPostMode.IMMEDIATE.value)


class _StubAPI:
    """In-memory stand-in for ``GitLabAPI`` — records every network call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def post_json(self, endpoint: str, payload: object) -> dict[str, object]:
        self.calls.append(("post_json", endpoint, payload))
        return {"id": 1, "notes": [{"type": "DiffNote"}], "line_code": "abc123_10_10"}

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint, None))
        return {}

    def delete(self, endpoint: str) -> int:
        self.calls.append(("delete", endpoint, None))
        return 204

    def current_username(self) -> str:
        return _AUTHOR_ALICE


def _service_with_stub() -> tuple[ReviewService, _StubAPI]:
    """Build a ReviewService backed by the recording stub API."""
    service = ReviewService(token="t")
    stub = _StubAPI()
    service._get_api = lambda: stub  # type: ignore[method-assign]
    return service, stub


_TERSE_NIT = "Nit: rename `x` to `count` — clearer intent."
_TRACKED_AT = "Nit: tracked at #1234."


class TestBloatGateAtPostingPath:
    """A chatter-laden note is refused on the posting path; a terse nit is allowed."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        self.monkeypatch = monkeypatch
        from teatree.cli.review import shape_gate as shape_mod  # noqa: PLC0415

        monkeypatch.setattr(shape_mod, "fetch_mr_author", lambda api, encoded_repo, mr: _AUTHOR_ALICE)
        from teatree.cli.review import service as review_mod  # noqa: PLC0415

        monkeypatch.setattr(
            review_mod,
            "resolve_inline_position",
            lambda api, encoded, mr, file, line: ({"new_path": file, "new_line": line}, ""),
        )

    def test_ticket_plus_coordination_note_is_refused(self) -> None:
        """A tracker id paired with a coordinate-with-people directive is refused."""
        service, stub = _service_with_stub()
        body = "Nit: this overlaps the work in #1234, ping the author."

        msg, code = service.post_comment("org/repo", 7, body, file="x.py", line=10)

        assert code == 1, f"expected refuse, got code={code} msg={msg!r}"
        assert "bloat" in msg.lower(), f"steering must name bloat: {msg!r}"
        assert stub.calls == [], "gate must block BEFORE any GitLab POST"

    def test_slack_handle_note_is_refused(self) -> None:
        """A note that @-mentions a stakeholder handle is refused."""
        service, stub = _service_with_stub()
        body = "Nit: @bob said in standup this should change."

        msg, code = service.post_comment("org/repo", 7, body, file="x.py", line=10)

        assert code == 1, f"expected refuse, got code={code} msg={msg!r}"
        assert stub.calls == [], "gate must block BEFORE any GitLab POST"

    def test_terse_diff_anchored_nit_is_allowed(self) -> None:
        """A terse, diff-anchored nit with no chatter passes to the API."""
        service, stub = _service_with_stub()

        msg, code = service.post_comment("org/repo", 7, _TERSE_NIT, file="x.py", line=10)

        assert code == 0, f"terse nit must pass: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls), "API POST must fire on a terse nit"

    def test_bare_tracked_at_reference_is_allowed(self) -> None:
        """A bare ``tracked at #1234`` pointer (no coordination) passes — the TODO-gate form."""
        service, stub = _service_with_stub()

        msg, code = service.post_comment("org/repo", 7, _TRACKED_AT, file="x.py", line=10)

        assert code == 0, f"bare tracked-at pointer must pass: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls)

    def test_allow_bloat_override_lets_a_load_bearing_reference_through(self) -> None:
        """The ``allow_bloat`` escape lets a genuinely load-bearing reference proceed."""
        service, stub = _service_with_stub()
        body = "Nit: this overlaps the work in #1234, ping the author."

        msg, code = service.post_comment("org/repo", 7, body, file="x.py", line=10, allow_bloat=True)

        assert code == 0, f"override must let the post proceed: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls), "API POST must fire with allow_bloat"

    def test_bloat_gate_also_fires_on_post_draft_note(self) -> None:
        """The gate runs on the ``post_draft_note`` sibling path too."""
        service, stub = _service_with_stub()
        body = "Nit: relates to #999 — the author should sync with the team."

        msg, code = service.post_draft_note("org/repo", 7, body, file="x.py", line=10)

        assert code == 1, f"expected refuse on post_draft_note: code={code} msg={msg!r}"
        assert stub.calls == [], "gate must block BEFORE any GitLab POST"


class TestBloatGateUnit:
    """Direct unit coverage of the pure classifier (no DB, no API)."""

    def test_empty_body_proceeds(self) -> None:
        assert check_review_bloat(body="") == ""
        assert references_project_chatter("") is False

    def test_allow_bloat_short_circuits_to_proceed(self) -> None:
        assert check_review_bloat(body="@alice ping the team about #42", allow_bloat=True) == ""

    def test_short_clean_nit_proceeds(self) -> None:
        assert check_review_bloat(body=_TERSE_NIT) == ""

    def test_legitimate_five_sentence_finding_is_not_chatter(self) -> None:
        """A long multi-sentence finding with no chatter is NOT this gate's job.

        Note length is owned by the colleague-MR shape gate; this gate
        leaves a chatter-free finding alone regardless of its length.
        """
        body = (
            "The factory drops a stale row when both writers race. "
            "First writer reads version=1 and computes the new state. "
            "Second writer commits first, version is now 2. "
            "First writer's bare-autocommit write then clobbers the second's commit. "
            "Wrapping the inner read-modify-write in atomic() with SELECT FOR UPDATE fixes it."
        )
        assert check_review_bloat(body=body) == ""

    def test_at_handle_is_chatter(self) -> None:
        assert references_project_chatter("@alice flagged this") is True

    def test_slack_ts_reference_is_chatter(self) -> None:
        assert references_project_chatter("per the thread at 1717000000.123456") is True

    def test_ticket_plus_coordination_is_chatter(self) -> None:
        assert references_project_chatter("relates to #1234, ping the author") is True
        assert references_project_chatter("see !567 — sync with the team first") is True

    def test_bare_ticket_reference_is_not_chatter(self) -> None:
        """A bare ``tracked at #1234`` pointer with no coordination is NOT chatter."""
        assert references_project_chatter("Nit: tracked at #1234.") is False
        assert references_project_chatter("duplicates the fix in !567") is False

    def test_code_symbol_with_hash_is_not_chatter(self) -> None:
        """A bare ``#`` not followed by 2+ digits (a heading, a lint token) is not chatter."""
        assert references_project_chatter("the `# type: ignore` here is wrong, sync with the team") is False

    def test_email_is_not_chatter(self) -> None:
        """An ``@`` inside an email-like token is not a bare stakeholder handle."""
        assert references_project_chatter("matches the user@example.com pattern in the test") is False

    def test_decimal_number_is_not_slack_ts(self) -> None:
        """A plain decimal (a ratio, a version) is not a Slack timestamp."""
        assert references_project_chatter("the 3.14 constant") is False
        assert references_project_chatter("bump to version 2.5") is False


_runner = CliRunner()


class TestAllowBloatReachesGateViaCLI:
    """The ``--allow-bloat`` flag is plumbed through the typer commands."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        self.stub = _StubAPI()
        monkeypatch.setattr(ReviewService, "get_gitlab_token", staticmethod(lambda: "t"))
        monkeypatch.setattr(ReviewService, "_get_api", lambda _self: self.stub)
        from teatree.cli.review import shape_gate as shape_mod  # noqa: PLC0415

        monkeypatch.setattr(shape_mod, "fetch_mr_author", lambda api, encoded_repo, mr: _AUTHOR_ALICE)
        from teatree.cli.review import service as review_mod  # noqa: PLC0415

        monkeypatch.setattr(
            review_mod,
            "resolve_inline_position",
            lambda api, encoded, mr, file, line: ({"new_path": file, "new_line": line}, ""),
        )
        monkeypatch.setattr(
            "teatree.cli.review.default_draft.notify_draft_created",
            lambda **_kwargs: None,
        )

    _BLOATED = "Nit: relates to #4321, the author should coordinate with the wider team before merge."

    def test_post_comment_bloated_refused_via_cli(self) -> None:
        result = _runner.invoke(
            app, ["review", "post-comment", "org/repo", "7", "-m", self._BLOATED, "--file", "x.py", "--line", "10"]
        )
        assert result.exit_code == 1, result.output
        assert "bloat" in result.output.lower()
        assert self.stub.calls == [], "gate must block BEFORE any GitLab POST"

    def test_post_comment_allow_bloat_proceeds_via_cli(self) -> None:
        result = _runner.invoke(
            app,
            [
                "review",
                "post-comment",
                "org/repo",
                "7",
                "-m",
                self._BLOATED,
                "--file",
                "x.py",
                "--line",
                "10",
                "--allow-bloat",
            ],
        )
        assert result.exit_code == 0, result.output
        assert any(c[0] == "post_json" for c in self.stub.calls), "POST must fire with --allow-bloat"

    def test_post_draft_note_allow_bloat_proceeds_via_cli(self) -> None:
        result = _runner.invoke(
            app,
            [
                "review",
                "post-draft-note",
                "org/repo",
                "7",
                self._BLOATED,
                "--file",
                "x.py",
                "--line",
                "10",
                "--allow-bloat",
            ],
        )
        assert result.exit_code == 0, result.output
        assert any(c[0] == "post_json" for c in self.stub.calls), "POST must fire with --allow-bloat"
