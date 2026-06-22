"""Multi-finding general-note gate (souliane/teatree#72, round 2).

The #72 fix stopped a half-specified inline (``--file`` without ``--line``)
from silently degrading into a general note. This gate closes the other
half observed on !6281: a deliberate ``--general`` note that crams 2+
distinct per-line findings into one MR-wide note instead of posting each
inline.

The gate runs only on the general (non-inline) path and refuses before any
GitLab call when the body references 2+ distinct ``path.ext:line`` locations
OR a numbered per-file finding list. ``--force-general`` is the documented
escape for a genuinely MR-wide (verdict-only) note. Inline posts are
unaffected.

Every service-level test pins the on-behalf gate to IMMEDIATE and the
shape gate to a colleague-agnostic pass so any blocking is attributable to
the gate under test.
"""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.review import ReviewService
from teatree.cli.review.general_inline_gate import check_general_inline_findings, looks_like_inline_findings
from teatree.config import OnBehalfPostMode
from teatree.core.models import ConfigSetting

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_AUTHOR_ALICE = "alice"


def _gate_immediate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the on-behalf gate to IMMEDIATE so it does NOT also block the call.

    Mirrors the sibling ``test_review_shape_gate`` helper: the gate under
    test is independent of the on-behalf gate, so IMMEDIATE keeps the latter
    silent. ``on_behalf_post_mode`` is DB-home (#1775); an empty config file
    keeps the active-config path pinned to ``tmp_path``.
    """
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


class TestGeneralNoteMultiFindingGate:
    """A general note carrying 2+ inline findings is refused before any POST.

    Every test pins the on-behalf gate to IMMEDIATE and the shape gate's
    MR-author lookup to the current identity (own MR) so the shape gate is
    a no-op and any blocking is attributable to this gate.
    """

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        self.monkeypatch = monkeypatch
        # Own-MR → shape gate is a no-op; isolates this gate.
        from teatree.cli.review import shape_gate as shape_mod  # noqa: PLC0415

        monkeypatch.setattr(shape_mod, "fetch_mr_author", lambda api, encoded_repo, mr: _AUTHOR_ALICE)

    def test_general_note_with_two_file_line_cites_is_refused(self) -> None:
        """RED → GREEN: a general note citing 2 distinct file:line locations is refused.

        The !6281 shape: two distinct per-line findings crammed into one
        MR-wide note. The gate must refuse before any GitLab POST and steer
        to per-finding inline posts.
        """
        service, stub = _service_with_stub()
        body = (
            "Two issues found:\n"
            "- handlers/auth.py:42 swallows the exception silently.\n"
            "- models/user.py:118 leaks the password hash in __repr__."
        )

        msg, code = service.post_comment("org/repo", 7, body)

        assert code == 1, f"expected refuse, got code={code} msg={msg!r}"
        assert "inline findings" in msg, f"steering must name the inline-findings shape: {msg!r}"
        assert "--file" in msg, f"steering must point at the inline command: {msg!r}"
        assert "--line" in msg, f"steering must point at the inline command: {msg!r}"
        assert "--force-general" in msg, f"steering must name the override: {msg!r}"
        assert stub.calls == [], "gate must block BEFORE any GitLab POST"

    def test_general_note_with_numbered_per_file_list_is_refused(self) -> None:
        """A numbered finding list where each item names a file is refused.

        The line-number-less variant of the multi-finding shape: a numbered
        list (``1. foo.py …`` / ``2. bar.py …``) is still N inline findings.
        """
        service, stub = _service_with_stub()
        body = (
            "1. cli/run.py: the retry loop never resets the backoff.\n"
            "2. core/models.py: the on_commit hook fires before the save commits."
        )

        msg, code = service.post_comment("org/repo", 7, body)

        assert code == 1, f"expected refuse, got code={code} msg={msg!r}"
        assert "inline findings" in msg
        assert stub.calls == [], "gate must block BEFORE any GitLab POST"

    def test_verdict_only_general_note_is_allowed(self) -> None:
        """A genuinely MR-wide verdict-only note (no per-line findings) passes.

        The control: an MR-wide summary with no concrete file:line cite and
        no numbered per-file list must NOT be caught — the gate is precise,
        not a blanket ban on general notes.
        """
        service, stub = _service_with_stub()
        body = "LGTM overall — the approach is sound and the tests cover the new path. Approving."

        msg, code = service.post_comment("org/repo", 7, body)

        assert code == 0, f"verdict-only general note must pass: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls), "API POST must fire on an accepted general note"

    def test_single_file_line_cite_general_note_is_allowed(self) -> None:
        """A general note with a SINGLE file:line cite passes (1 finding, not multi).

        One cited location is a single finding — legitimately a one-line
        MR-wide note. The gate fires only at 2+ distinct locations.
        """
        service, stub = _service_with_stub()
        body = "The regression is at handlers/auth.py:42 — the except clause swallows it."

        msg, code = service.post_comment("org/repo", 7, body)

        assert code == 0, f"single-cite general note must pass: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls)

    def test_force_general_overrides_the_refusal(self) -> None:
        """``force_general=True`` lets the 2+ file:line general note proceed.

        The documented escape: a reviewer who genuinely wants the multi-cite
        body as one MR-wide note passes ``--force-general`` and the post
        proceeds, mirroring the sibling ``--allow-long-review`` override.
        """
        service, stub = _service_with_stub()
        body = "- handlers/auth.py:42 issue one.\n- models/user.py:118 issue two."

        msg, code = service.post_comment("org/repo", 7, body, force_general=True)

        assert code == 0, f"override must let the post proceed: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls), "API POST must fire when --force-general is set"

    def test_inline_post_with_multi_cite_body_is_unaffected(self) -> None:
        """An inline (``file``+``line``) post is NOT subject to this gate.

        Even if an inline body happens to mention 2+ file:line locations
        (e.g. "this duplicates the logic at other.py:10 and other.py:20"),
        the post IS being anchored inline, so the #72-round-2 general gate
        does not fire — the post proceeds to the inline path.
        """
        service, stub = _service_with_stub()
        from teatree.cli.review import service as review_mod  # noqa: PLC0415

        self.monkeypatch.setattr(
            review_mod,
            "resolve_inline_position",
            lambda api, encoded, mr, file, line: ({"new_path": file, "new_line": line}, ""),
        )
        body = "Duplicates the logic at other.py:10 and other.py:20 — extract a helper."

        msg, code = service.post_comment("org/repo", 7, body, file="x.py", line=10)

        assert code == 0, f"inline post must be unaffected: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls)

    def test_post_draft_note_general_with_multi_cite_is_refused(self) -> None:
        """The gate also fires on ``post_draft_note`` (the #72 sibling path).

        ``post-draft-note --general`` is the path the #72 validator governs
        for the inline-vs-general split; this gate adds the multi-finding
        refusal on the same general path.
        """
        service, stub = _service_with_stub()
        body = "- a.py:1 finding one.\n- b.py:2 finding two."

        msg, code = service.post_draft_note("org/repo", 7, body)

        assert code == 1, f"expected refuse on post_draft_note general: code={code} msg={msg!r}"
        assert "inline findings" in msg
        assert stub.calls == [], "gate must block BEFORE any GitLab POST"


class TestGeneralInlineGateUnit:
    """Direct unit coverage of the pure gate functions (no DB, no API)."""

    def test_force_general_short_circuits_to_proceed(self) -> None:
        """The override returns ``""`` (proceed) regardless of body shape."""
        body = "- a.py:1 one.\n- b.py:2 two."
        assert check_general_inline_findings(body=body, inline=False, force_general=True) == ""

    def test_inline_flag_short_circuits_to_proceed(self) -> None:
        """An inline post short-circuits to proceed — this gate is general-only."""
        body = "- a.py:1 one.\n- b.py:2 two."
        assert check_general_inline_findings(body=body, inline=True) == ""

    def test_empty_body_proceeds(self) -> None:
        """An empty body is not a multi-finding note — proceed."""
        assert check_general_inline_findings(body="", inline=False) == ""
        assert looks_like_inline_findings("") is False

    def test_two_distinct_file_line_cites_detected(self) -> None:
        """2 distinct file:line cites trip the detector."""
        assert looks_like_inline_findings("see a.py:10 and b.ts:3") is True

    def test_same_file_two_lines_counts_as_two(self) -> None:
        """Two findings in the SAME file (different lines) count as two."""
        assert looks_like_inline_findings("a.py:10 and also a.py:42") is True

    def test_single_cite_is_not_multi(self) -> None:
        """A single file:line cite is one finding — not multi."""
        assert looks_like_inline_findings("only a.py:10 here") is False

    def test_duplicate_cite_collapses_to_one(self) -> None:
        """The same cite repeated is distinct-by-text → one location, not two."""
        assert looks_like_inline_findings("a.py:10 ... and again a.py:10") is False

    def test_time_and_ratio_do_not_false_positive(self) -> None:
        """``12:30`` (a time) and ``3:2`` (a ratio) are not file:line cites.

        The detector requires a dotted ``path.ext`` before the ``:line``,
        so bare ``number:number`` tokens do not register as findings.
        """
        assert looks_like_inline_findings("meeting at 12:30 and a 3:2 ratio") is False

    def test_numbered_list_two_files_detected(self) -> None:
        """A 2-item numbered list each naming a file (no line numbers) trips it."""
        body = "1. foo.py rename the helper.\n2. bar.ts guard the null."
        assert looks_like_inline_findings(body) is True

    def test_numbered_list_one_file_is_not_multi(self) -> None:
        """A single numbered item naming a file is one finding — not multi."""
        body = "1. foo.py rename the helper."
        assert looks_like_inline_findings(body) is False

    def test_paren_numbered_markers_detected(self) -> None:
        """``1)`` / ``2)`` paren-style numbered markers also count."""
        body = "1) a.py do this.\n2) b.py do that."
        assert looks_like_inline_findings(body) is True


_runner = CliRunner()


class TestForceGeneralReachesGateViaCLI:
    """The ``--force-general`` flag is plumbed through the typer commands.

    Drives the full ``t3 review post-comment`` / ``post-draft-note`` typer
    surface (not just the service method) so a regression that drops the
    flag from the CLI wiring turns this red. The GitLab token + API are
    stubbed so the command runs end to end without network.
    """

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        self.stub = _StubAPI()
        monkeypatch.setattr(ReviewService, "get_gitlab_token", staticmethod(lambda: "t"))
        monkeypatch.setattr(ReviewService, "_get_api", lambda _self: self.stub)
        # Own MR → shape gate no-op; isolates this gate end to end.
        from teatree.cli.review import shape_gate as shape_mod  # noqa: PLC0415

        monkeypatch.setattr(shape_mod, "fetch_mr_author", lambda api, encoded_repo, mr: _AUTHOR_ALICE)
        # The default-draft path DMs the user on success — stub the notify
        # transport so the CLI invocation does not touch a real backend.
        monkeypatch.setattr(
            "teatree.cli.review.default_draft.notify_draft_created",
            lambda **_kwargs: None,
        )

    # Two findings naming distinct files, fed via -m/--body so the body
    # (which would naturally start with a list marker) is not parsed as an
    # option by typer. The shape is the multi-cite signal regardless.
    _MULTI = "first at a.py:1 finding one; second at b.py:2 finding two."

    def test_post_comment_general_multi_cite_refused_via_cli(self) -> None:
        """``post-comment`` with a 2-cite general body is refused at the CLI."""
        result = _runner.invoke(app, ["review", "post-comment", "org/repo", "7", "-m", self._MULTI])
        assert result.exit_code == 1, result.output
        assert "inline findings" in result.output
        assert self.stub.calls == [], "gate must block BEFORE any GitLab POST"

    def test_post_comment_force_general_proceeds_via_cli(self) -> None:
        """``--force-general`` on ``post-comment`` lets the multi-cite note proceed."""
        result = _runner.invoke(app, ["review", "post-comment", "org/repo", "7", "-m", self._MULTI, "--force-general"])
        assert result.exit_code == 0, result.output
        assert any(c[0] == "post_json" for c in self.stub.calls), "POST must fire with --force-general"

    def test_post_draft_note_force_general_proceeds_via_cli(self) -> None:
        """``--force-general`` on ``post-draft-note --general`` lets the multi-cite note proceed."""
        result = _runner.invoke(
            app,
            ["review", "post-draft-note", "org/repo", "7", self._MULTI, "--general", "--force-general"],
        )
        assert result.exit_code == 0, result.output
        assert any(c[0] == "post_json" for c in self.stub.calls), "POST must fire with --force-general"
