"""Author-marked TODO/FIXME anchor gate (souliane/teatree#1186).

When a reviewer posts a blocker-shaped comment via `t3 review post-comment`
or `post-draft-note` anchored to (or within ±3 lines of) an author-marked
TODO/FIXME/XXX/HACK on an added line, the gate refuses the post. The
author has already documented the work is deferred ("not in this MR" /
"follow-up" / "deferred" / "implement later" / "out of scope"); re-asking
them to implement it makes the reviewer look unable to read code.

This is the structural fix for #1186: blocker comments were posted
anchored to author-marked `// TODO:` lines documenting deferred work.
The fix gates publishing so the same shape cannot recur.

The gate is independent of the colleague-MR shape gate and the on-behalf
gate; all three run on every publishing method.
"""

from pathlib import Path
from typing import Any, cast

import pytest

from teatree.cli.review import ReviewService
from teatree.config import OnBehalfPostMode
from teatree.core.models import ConfigSetting

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _gate_immediate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin on-behalf gate to IMMEDIATE so it does not also block the call."""
    ConfigSetting.objects.set_value("on_behalf_post_mode", OnBehalfPostMode.IMMEDIATE.value)


def _disable_shape_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the colleague-MR shape gate so refusals come from the TODO gate only.

    The gate chain lives in :mod:`teatree.cli.review.pre_publish_gates`, so
    the shape gate is patched where the chain looks it up.
    """
    from teatree.cli.review import pre_publish_gates as gates_mod  # noqa: PLC0415

    monkeypatch.setattr(gates_mod, "check_review_shape", lambda **_kw: "")


def _build_diff(target_line: int, marker_line: int, marker_text: str) -> str:
    """Build a unified diff with `+`-added lines, placing ``marker_text`` at ``marker_line``.

    Every line from ``target_line - 5`` to ``target_line + 5`` is added,
    so the TODO marker sits at exactly ``marker_line`` and the anchor at
    ``target_line``. The hunk header reflects the new-file line range.
    """
    start = max(1, target_line - 5)
    end = target_line + 5
    lines = [f"@@ -{start},{end - start + 1} +{start},{end - start + 1} @@"]
    for ln in range(start, end + 1):
        if ln == marker_line:
            lines.append(f"+    {marker_text}")
        else:
            lines.append(f"+    code_at_{ln}()")
    return "\n".join(lines) + "\n"


class _StubAPI:
    """Stand-in for GitLabAPI — records calls and returns a configurable diff."""

    def __init__(self, diff_text: str, mr_author: str = "carol") -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self._diff = diff_text
        self._mr_author = mr_author

    def post_json(self, endpoint: str, payload: object) -> dict[str, object]:
        self.calls.append(("post_json", endpoint, payload))
        # ``line_code`` keeps the draft-notes anchor check happy on the
        # new default-draft ``post_comment`` path (#1207); the discussions
        # endpoint ignores it, so the same shape serves both branches.
        return {"id": 1, "notes": [{"type": "DiffNote"}], "web_url": "", "line_code": "abc_42_42"}

    def post_status(self, endpoint: str) -> int:
        self.calls.append(("post_status", endpoint, None))
        return 200

    def delete(self, endpoint: str) -> int:
        self.calls.append(("delete", endpoint, None))
        return 204

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint, None))
        # MR metadata fetch (for diff_refs and author).
        if "/changes" in endpoint:
            return {
                "changes": [
                    {"new_path": "x.py", "old_path": "x.py", "diff": self._diff},
                ],
            }
        if "/merge_requests/" in endpoint and "/changes" not in endpoint:
            return {
                "author": {"username": self._mr_author},
                "diff_refs": {
                    "base_sha": "b",
                    "head_sha": "h",
                    "start_sha": "s",
                },
            }
        return {}

    def current_username(self) -> str:
        return "alice"


def _service_with_stub(stub: _StubAPI) -> ReviewService:
    service = ReviewService(token="t")
    service._get_api = lambda: stub  # type: ignore[method-assign]
    return service


# ---------------------------------------------------------------------------
# Positive control: a normal `+`-added line accepts a blocker-shaped comment.
# ---------------------------------------------------------------------------


class TestNoMarkerAcceptsBlocker:
    """When the anchor line and its ±3 neighbours have no TODO marker, blocker comments pass."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        _disable_shape_gate(monkeypatch)

    def test_blocker_comment_on_plain_code_line_is_accepted(self) -> None:
        diff = _build_diff(target_line=42, marker_line=999, marker_text="never reached")
        stub = _StubAPI(diff)
        service = _service_with_stub(stub)

        msg, code = service.post_comment(
            "org/repo",
            7,
            "This is blocking — must be addressed before merge.",
            file="x.py",
            line=42,
        )

        assert code == 0, f"plain-line blocker must pass: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" and "/draft_notes" in c[1] for c in stub.calls), (
            "API POST must hit on accepted blocker"
        )


# ---------------------------------------------------------------------------
# Marker-anchor refusal: anchor or ±3 lines contain a deferred-work marker.
# ---------------------------------------------------------------------------


class TestTodoMarkerRefusesBlocker:
    """A TODO/FIXME marker on the anchor line or within ±3 refuses a blocker post."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        _disable_shape_gate(monkeypatch)

    @pytest.mark.parametrize(
        "marker_text",
        [
            "// TODO: Navigate to the results page",
            "# TODO: load user data",
            "/* TODO: refactor this */",
            "// FIXME: handle null case",
            "# FIXME: this is wrong",
            "// XXX: hack alert",
            "# XXX: revisit",
            "// HACK: bypass for now",
            "# HACK: temporary",
            "// todo: lowercase variant",
            "// FixMe: mixed case",
        ],
    )
    def test_blocker_anchored_at_marker_line_is_refused(self, marker_text: str) -> None:
        """Each marker variant on the exact anchor line refuses a blocker post."""
        diff = _build_diff(target_line=42, marker_line=42, marker_text=marker_text)
        stub = _StubAPI(diff)
        service = _service_with_stub(stub)

        msg, code = service.post_comment(
            "org/repo",
            7,
            "This must be implemented before merge — it is blocking.",
            file="x.py",
            line=42,
        )

        assert code == 1, f"TODO-anchor blocker must refuse: code={code} msg={msg!r}"
        assert "author-marked" in msg.lower() or "todo" in msg.lower(), f"refusal must mention the TODO marker: {msg!r}"
        assert not any(c[0] == "post_json" and "/draft_notes" in c[1] for c in stub.calls), (
            "no GitLab POST must fire when the gate refuses"
        )

    @pytest.mark.parametrize("offset", [-3, -2, -1, 1, 2, 3])
    def test_blocker_within_three_lines_of_marker_is_refused(self, offset: int) -> None:
        """A marker within ±3 of the anchor line refuses a blocker post."""
        diff = _build_diff(target_line=42, marker_line=42 + offset, marker_text="// TODO: deferred")
        stub = _StubAPI(diff)
        service = _service_with_stub(stub)

        msg, code = service.post_comment(
            "org/repo",
            7,
            "This is blocking and must be done before merge.",
            file="x.py",
            line=42,
        )

        assert code == 1, f"marker at offset {offset} must refuse: code={code} msg={msg!r}"
        assert not any(c[0] == "post_json" and "/draft_notes" in c[1] for c in stub.calls)

    def test_marker_at_offset_four_does_not_refuse(self) -> None:
        """A marker at ±4 is outside the window — the blocker post is allowed."""
        diff = _build_diff(target_line=42, marker_line=46, marker_text="// TODO: far away")
        stub = _StubAPI(diff)
        service = _service_with_stub(stub)

        msg, code = service.post_comment(
            "org/repo",
            7,
            "This is blocking and must be done before merge.",
            file="x.py",
            line=42,
        )

        assert code == 0, f"marker outside ±3 must NOT refuse: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" and "/draft_notes" in c[1] for c in stub.calls)


class TestDeferralPhrasesRefuseBlocker:
    """Deferral phrases ("not in this MR", "follow-up", etc.) refuse a blocker post."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        _disable_shape_gate(monkeypatch)

    @pytest.mark.parametrize(
        "marker_text",
        [
            "// not in this MR",
            "# follow-up",
            "// follow up later",
            "# deferred to next sprint",
            "// implement later",
            "# out of scope",
        ],
    )
    def test_blocker_anchored_at_deferral_phrase_is_refused(self, marker_text: str) -> None:
        diff = _build_diff(target_line=42, marker_line=42, marker_text=marker_text)
        stub = _StubAPI(diff)
        service = _service_with_stub(stub)

        msg, code = service.post_comment(
            "org/repo",
            7,
            "This must be implemented in this MR — it is blocking.",
            file="x.py",
            line=42,
        )

        assert code == 1, f"deferral phrase must refuse: code={code} msg={msg!r}"
        assert not any(c[0] == "post_json" and "/draft_notes" in c[1] for c in stub.calls)


# ---------------------------------------------------------------------------
# Non-blocker comments and approvals are NOT refused even on TODO lines.
# ---------------------------------------------------------------------------


class TestNonBlockerCommentsAllowed:
    """Non-blocker (nit/cross-reference) comments on a TODO line are allowed."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        _disable_shape_gate(monkeypatch)

    def test_nit_comment_on_todo_line_is_accepted(self) -> None:
        """A non-blocker nit on a TODO line passes — it adds context, not a demand."""
        diff = _build_diff(target_line=42, marker_line=42, marker_text="// TODO: handle later")
        stub = _StubAPI(diff)
        service = _service_with_stub(stub)

        msg, code = service.post_comment(
            "org/repo",
            7,
            "Nit: tracked at #1234.",
            file="x.py",
            line=42,
        )

        assert code == 0, f"nit on TODO line must pass: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" and "/draft_notes" in c[1] for c in stub.calls)

    def test_cross_reference_comment_on_todo_line_is_accepted(self) -> None:
        """A neutral cross-reference comment on a TODO line passes."""
        diff = _build_diff(target_line=42, marker_line=42, marker_text="# TODO: refactor")
        stub = _StubAPI(diff)
        service = _service_with_stub(stub)

        msg, code = service.post_comment(
            "org/repo",
            7,
            "Tracked at #5678.",
            file="x.py",
            line=42,
        )

        assert code == 0, f"cross-ref on TODO line must pass: code={code} msg={msg!r}"


# ---------------------------------------------------------------------------
# Unit tests on the gate function itself (no Django, no API round-trips).
# ---------------------------------------------------------------------------


class TestAllowTodoBlockerOverride:
    """A documented ``--allow-todo-blocker`` escape lets an authorized in-MR blocker proceed.

    Mirrors the sibling ``--quote-ok`` / ``--allow-banned-term`` overrides
    (#126 round-2, gap 2).

    Matrix:
    * refused without the flag (the gate still fires);
    * allowed with the flag (the escape works);
    * fail-open on diff-fetch failure is preserved (the flag does not relax it).
    """

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        _disable_shape_gate(monkeypatch)

    def test_blocker_refused_without_flag(self) -> None:
        """Control: a TODO-anchored blocker is refused when the override is absent."""
        diff = _build_diff(target_line=42, marker_line=42, marker_text="// TODO: defer")
        stub = _StubAPI(diff)
        service = _service_with_stub(stub)

        msg, code = service.post_comment(
            "org/repo",
            7,
            "This must be implemented before merge — it is blocking.",
            file="x.py",
            line=42,
        )

        assert code == 1, f"control: blocker must refuse without flag: code={code} msg={msg!r}"
        assert not any(c[0] == "post_json" and "/draft_notes" in c[1] for c in stub.calls)

    def test_blocker_allowed_with_flag(self) -> None:
        """The escape works: the same blocker proceeds with ``allow_todo_blocker=True``."""
        diff = _build_diff(target_line=42, marker_line=42, marker_text="// TODO: defer")
        stub = _StubAPI(diff)
        service = _service_with_stub(stub)

        msg, code = service.post_comment(
            "org/repo",
            7,
            "This must be implemented before merge — it is blocking.",
            file="x.py",
            line=42,
            allow_todo_blocker=True,
        )

        assert code == 0, f"override must let the blocker proceed: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" and "/draft_notes" in c[1] for c in stub.calls), (
            "API POST must fire when the TODO-blocker override is set"
        )

    def test_gate_function_returns_empty_with_override(self) -> None:
        """Direct unit: the override short-circuits to ``""`` (proceed)."""
        from teatree.cli.review.todo_gate import InlineAnchor, check_todo_anchor  # noqa: PLC0415

        diff = _build_diff(target_line=42, marker_line=42, marker_text="// TODO: defer")

        class _DiffAPI:
            def get_json(self, endpoint: str) -> object:
                if "/changes" in endpoint:
                    return {"changes": [{"new_path": "x.py", "diff": diff}]}
                return {}

        assert (
            check_todo_anchor(
                api=cast("Any", _DiffAPI()),
                encoded_repo="org%2Frepo",
                mr=7,
                body="This is blocking and must be done before merge.",
                anchor=InlineAnchor(file="x.py", line=42),
                allow_todo_blocker=True,
            )
            == ""
        )

    def test_fail_open_on_diff_failure_unchanged_by_flag(self) -> None:
        """Fail-open is preserved: a diff-fetch failure proceeds with OR without the flag."""
        from teatree.cli.review.todo_gate import InlineAnchor, check_todo_anchor  # noqa: PLC0415

        class _BoomAPI:
            def get_json(self, _endpoint: str) -> object:
                msg = "network down"
                raise RuntimeError(msg)

        for flag in (False, True):
            assert (
                check_todo_anchor(
                    api=cast("Any", _BoomAPI()),
                    encoded_repo="org%2Frepo",
                    mr=7,
                    body="This is blocking and must be done.",
                    anchor=InlineAnchor(file="x.py", line=42),
                    allow_todo_blocker=flag,
                )
                == ""
            )


class TestCheckTodoAnchorUnit:
    """Direct unit tests on ``check_todo_anchor`` — no ReviewService round-trip."""

    def test_no_file_or_line_is_proceed(self) -> None:
        """General (MR-level) comments — empty anchor — bypass the gate."""
        from teatree.cli.review.todo_gate import InlineAnchor, check_todo_anchor  # noqa: PLC0415

        class _NoAPI:
            def get_json(self, _endpoint: str) -> object:
                msg = "must not GET for a general comment"
                raise AssertionError(msg)

        assert (
            check_todo_anchor(
                api=cast("Any", _NoAPI()),
                encoded_repo="org%2Frepo",
                mr=7,
                body="This is blocking.",
                anchor=InlineAnchor(file="", line=0),
            )
            == ""
        )

    def test_non_blocker_body_is_proceed_without_api(self) -> None:
        """A non-blocker body skips the diff fetch entirely (cheap fast-path)."""
        from teatree.cli.review.todo_gate import InlineAnchor, check_todo_anchor  # noqa: PLC0415

        class _NoAPI:
            def get_json(self, _endpoint: str) -> object:
                msg = "must not GET on a non-blocker body"
                raise AssertionError(msg)

        assert (
            check_todo_anchor(
                api=cast("Any", _NoAPI()),
                encoded_repo="org%2Frepo",
                mr=7,
                body="Nit: tracked at #1234.",
                anchor=InlineAnchor(file="x.py", line=42),
            )
            == ""
        )

    def test_network_failure_fails_open(self) -> None:
        """If the diff fetch raises, fail-open — do not break the legitimate post path."""
        from teatree.cli.review.todo_gate import InlineAnchor, check_todo_anchor  # noqa: PLC0415

        class _BoomAPI:
            def get_json(self, _endpoint: str) -> object:
                msg = "network down"
                raise RuntimeError(msg)

        assert (
            check_todo_anchor(
                api=cast("Any", _BoomAPI()),
                encoded_repo="org%2Frepo",
                mr=7,
                body="This is blocking and must be done.",
                anchor=InlineAnchor(file="x.py", line=42),
            )
            == ""
        )

    def test_blocker_with_marker_at_anchor_is_refused(self) -> None:
        """End-to-end: blocker body + deferred-work marker at anchor → refusal string."""
        from teatree.cli.review.todo_gate import InlineAnchor, check_todo_anchor  # noqa: PLC0415

        diff = _build_diff(target_line=42, marker_line=42, marker_text="// TODO: defer")

        class _DiffAPI:
            def get_json(self, endpoint: str) -> object:
                if "/changes" in endpoint:
                    return {"changes": [{"new_path": "x.py", "diff": diff}]}
                return {}

        msg = check_todo_anchor(
            api=cast("Any", _DiffAPI()),
            encoded_repo="org%2Frepo",
            mr=7,
            body="This is blocking and must be done before merge.",
            anchor=InlineAnchor(file="x.py", line=42),
        )
        assert msg, "expected a refusal string"
        assert "todo" in msg.lower() or "author-marked" in msg.lower()
