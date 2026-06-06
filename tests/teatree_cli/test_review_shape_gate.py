"""Colleague-MR review-shape gate (souliane/teatree#1114, loosened in #1159).

When a review is posted on a colleague's MR (the MR's author is NOT the
current identity), the binding rule from the review skill is **single
terse INLINE Nit:-prefixed comment** — never a multi-section
Problem/Fix/Verification dump. The previous safety-net was a memory
entry (a guideline an agent could forget). The structural gate enforces
it deterministically before the GitLab API call hits.

Shape rule (post-#1159): refuse when the body exceeds
``COLLEAGUE_PROSE_CAP_PARAGRAPHS`` paragraphs (blank-line separated)
OR ``COLLEAGUE_PROSE_CAP_WORDS`` words. The paragraph+word combination
still catches the 27-sentence Problem/Fix/Verification abuse shape the
gate was added to prevent (#1114) without false-rejecting legitimate
≤3-sentence findings whose clauses are split by commas or semicolons.

Own-MR carve-out: when the MR author **is** the current identity, the
gate is a no-op — own-MR reviews can be as long-form as needed.
"""

from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from teatree.cli.review import ReviewService
from teatree.config import OnBehalfPostMode

pytestmark = pytest.mark.django_db

_AUTHOR_CAROL = "carol"
_AUTHOR_ALICE = "alice"


def _gate_immediate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the on-behalf gate to IMMEDIATE so it does NOT also block the call.

    The shape gate is independent of the on-behalf gate — both run on every
    publishing method, but only the shape gate is under test here. IMMEDIATE
    keeps the on-behalf gate silent so any blocking comes from the shape gate
    we are testing.
    """
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f'[teatree]\non_behalf_post_mode = "{OnBehalfPostMode.IMMEDIATE.value}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


class _StubAPI:
    """In-memory stand-in for ``GitLabAPI`` — records every network call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def post_json(self, endpoint: str, payload: object) -> dict[str, object]:
        self.calls.append(("post_json", endpoint, payload))
        # ``line_code`` keeps the draft-notes anchor check happy on the
        # new default-draft ``post_comment`` path (#1207); the discussions
        # endpoint ignores it, so the same shape serves both branches.
        return {"id": 1, "notes": [{"type": "DiffNote"}], "line_code": "abc123_10_10"}

    def post_status(self, endpoint: str) -> int:
        self.calls.append(("post_status", endpoint, None))
        return 200

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint, None))
        return {}

    def delete(self, endpoint: str) -> int:
        self.calls.append(("delete", endpoint, None))
        return 204

    def current_username(self) -> str:
        return _AUTHOR_ALICE


def _service_with_stub(*, mr_author: str) -> tuple[ReviewService, _StubAPI]:
    """Build a ReviewService whose API stub returns ``mr_author`` for the MR."""
    service = ReviewService(token="t")
    stub = _StubAPI()
    service._get_api = lambda: stub  # type: ignore[method-assign]
    # Patch the shape-gate's MR-author lookup at its source module — the
    # canonical surface every call site routes through.
    return service, stub


class TestColleagueMRShapeGate:
    """The shape-gate rejects MR-level prose on a colleague MR.

    Every test pins the on-behalf gate to IMMEDIATE so any blocking is
    attributable to the shape gate. The shape gate's MR-author lookup
    is patched at the source module (``teatree.cli.review_shape_gate``)
    via ``fetch_mr_author``.
    """

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        self.monkeypatch = monkeypatch

    def _patch_mr_author(self, author: str) -> None:
        """Pin the shape gate's MR-author resolution to ``author``."""
        from teatree.cli import review_shape_gate as gate_mod  # noqa: PLC0415

        self.monkeypatch.setattr(
            gate_mod,
            "fetch_mr_author",
            lambda api, encoded_repo, mr: author,
        )

    def test_red_card_rejects_multi_section_mr_level_dump_on_colleague_mr(self) -> None:
        """RED CARD recurrence: a multi-section Problem/Fix/Verification dump is refused.

        The original shape gate refused on a hard 2-sentence cap, which
        over-fit on legitimate short nits. The replacement guard refuses
        on paragraph-count or word-count: a 4+ paragraph dump is the
        exact abuse shape (per !6201 RED CARD).
        """
        self._patch_mr_author(_AUTHOR_CAROL)
        service, stub = _service_with_stub(mr_author=_AUTHOR_CAROL)
        body = (
            "## Problem\n\n"
            "The handler crashes on empty input.\n\n"
            "## Fix\n\n"
            "Guard the empty case before the loop.\n\n"
            "## Verification\n\n"
            "Added a regression test."
        )

        msg, code = service.post_comment("org/repo", 7, body)

        assert code == 1, f"expected refuse, got code={code} msg={msg!r}"
        assert "Refusing colleague-MR on-behalf post" in msg
        assert "paragraph" in msg, f"steering must name the breach concretely: {msg!r}"
        assert "Nit:" in msg
        assert stub.calls == [], "shape gate must block BEFORE any GitLab POST"

    def test_legitimate_three_sentence_finding_passes(self) -> None:
        """Issue #1159: a legitimate 3-sentence MR-level finding must pass.

        Per #1159 the cap is paragraph-and-word, not sentence-count.
        A short, single-paragraph 3-sentence finding with a file:line cite
        is the canonical case the old 2-sentence cap false-rejected.
        """
        self._patch_mr_author(_AUTHOR_CAROL)
        service, stub = _service_with_stub(mr_author=_AUTHOR_CAROL)
        body = (
            "REQUEST-CHANGES on v3: BUSINESS_ENTITY joint-rep search no longer navigates. "
            "The submit handler swallows the click without firing the router. "
            "See line 142 in joint-rep-search.component.ts for the regression."
        )

        msg, code = service.post_comment("org/repo", 7, body)

        assert code == 0, f"3-sentence finding must pass: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls), "API POST must hit on accepted note"

    def test_legitimate_long_single_paragraph_passes(self) -> None:
        """A 5-sentence single-paragraph finding under the word cap passes.

        The old sentence-counting guard rejected anything past 2 sentences;
        the new paragraph/word guard allows multi-sentence prose as long
        as it stays in one paragraph and under the word cap.
        """
        self._patch_mr_author(_AUTHOR_CAROL)
        service, stub = _service_with_stub(mr_author=_AUTHOR_CAROL)
        body = (
            "The factory drops a stale row when both writers race. "
            "First writer reads version=1 and computes the new state. "
            "Second writer commits first, version is now 2. "
            "First writer's bare-autocommit write then clobbers the second's commit. "
            "Wrapping the inner read-modify-write in atomic() with SELECT FOR UPDATE fixes it."
        )

        msg, code = service.post_comment("org/repo", 7, body)

        assert code == 0, f"5-sentence single-paragraph finding must pass: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls)

    def test_twentyseven_sentence_dump_still_rejected(self) -> None:
        """The 27-sentence Problem/Fix/Verification abuse shape is still rejected.

        Issue #1159 loosens the gate but preserves its anti-abuse intent.
        A 27-sentence dump exceeds the word-count cap and must still be
        refused with the steering message pointing at the inline Nit form.
        """
        self._patch_mr_author(_AUTHOR_CAROL)
        service, stub = _service_with_stub(mr_author=_AUTHOR_CAROL)
        # 27 sentences, each long enough to push the body past the
        # word-count cap. Single paragraph so the rejection is keyed
        # on word count, not paragraph count.
        sentence = "this sentence has eight words for the count. "
        body = sentence * 27

        msg, code = service.post_comment("org/repo", 7, body)

        assert code == 1, f"27-sentence dump must be refused: code={code} msg={msg!r}"
        assert "Refusing colleague-MR on-behalf post" in msg
        assert "word" in msg, f"steering must name the word-count breach: {msg!r}"
        assert stub.calls == [], "shape gate must block BEFORE any GitLab POST"

    def test_inline_nit_comment_accepted(self) -> None:
        """Short inline Nit on a colleague MR — accepted, API POST hit."""
        self._patch_mr_author(_AUTHOR_CAROL)
        service, stub = _service_with_stub(mr_author=_AUTHOR_CAROL)
        # Bypass the inline-position resolver (it needs real diff refs).
        from teatree.cli import review as review_mod  # noqa: PLC0415

        self.monkeypatch.setattr(
            review_mod,
            "resolve_inline_position",
            lambda api, encoded, mr, file, line: ({"new_path": file, "new_line": line}, ""),
        )

        msg, code = service.post_comment("org/repo", 7, "Nit: rename foo to bar.", file="x.py", line=10)

        assert code == 0, f"expected success, got code={code} msg={msg!r}"
        assert "OK" in msg
        assert any(c[0] == "post_json" for c in stub.calls), "API POST must hit on accepted inline nit"

    def test_own_mr_long_note_accepted(self) -> None:
        """When MR author == current identity, the shape gate is a no-op.

        Own-MR reviews are exempt: the agent may post long-form prose on its
        own MR (e.g. a self-review summary, a context note, an evidence
        block). The 6-sentence body would breach the colleague cap, but on
        an own MR it passes through to the GitLab API.
        """
        self._patch_mr_author(_AUTHOR_ALICE)
        service, stub = _service_with_stub(mr_author=_AUTHOR_ALICE)
        body = "S1. S2. S3. S4. S5. S6."

        msg, code = service.post_comment("org/repo", 7, body)

        assert code == 0, f"own-MR long prose must pass: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls)

    def test_approve_with_no_comment_zero_friction(self) -> None:
        """Approve on a colleague MR has no body — the shape gate must not interfere.

        ``approve`` has its own review-first precondition; the shape gate
        operates only on methods that take a body. ``approve`` takes no body,
        so even on a colleague MR the shape gate is a no-op.
        """
        self._patch_mr_author(_AUTHOR_CAROL)
        service, stub = _service_with_stub(mr_author=_AUTHOR_CAROL)

        # Pretend the agent has already reviewed (satisfies the
        # review-before-approve precondition) — the focus here is the
        # shape gate, not the review-first gate.
        from teatree.cli import review as review_mod  # noqa: PLC0415

        self.monkeypatch.setattr(
            review_mod,
            "identity_has_reviewed",
            lambda api, encoded, mr: (True, ""),
        )

        msg, code = service.approve("org/repo", 7)

        assert code == 0, f"approve on colleague MR must pass: code={code} msg={msg!r}"
        assert any(c[0] == "post_status" for c in stub.calls)

    def test_inline_long_finding_accepted_under_word_cap(self) -> None:
        """Inline carve-out: a multi-sentence real finding on a colleague MR — accepted.

        Per #1159 the gate guards paragraph and word counts rather than
        sentence counts. A short multi-sentence inline body that fits
        in one paragraph and stays under the word cap passes.
        """
        self._patch_mr_author(_AUTHOR_CAROL)
        service, stub = _service_with_stub(mr_author=_AUTHOR_CAROL)
        from teatree.cli import review as review_mod  # noqa: PLC0415

        self.monkeypatch.setattr(
            review_mod,
            "resolve_inline_position",
            lambda api, encoded, mr, file, line: ({"new_path": file, "new_line": line}, ""),
        )

        body = "This branch is unreachable. The caller already guards with isinstance. Consider deleting the if."

        msg, code = service.post_comment("org/repo", 7, body, file="x.py", line=10)

        assert code == 0, f"multi-sentence inline finding must pass: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls)


class TestAllowLongReviewOverride:
    """A documented ``--allow-long-review`` escape lets an over-cap colleague-MR review proceed.

    Consistent with the sibling override pattern (#126 round-2, gap 5).

    Matrix:
    * refused without the flag (the cap still fires);
    * allowed with the flag (the escape works);
    * fail-open carve-out (own-MR / unreadable identity) is preserved.
    """

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        self.monkeypatch = monkeypatch

    def _patch_mr_author(self, author: str) -> None:
        from teatree.cli import review_shape_gate as gate_mod  # noqa: PLC0415

        self.monkeypatch.setattr(gate_mod, "fetch_mr_author", lambda api, encoded_repo, mr: author)

    def test_over_cap_refused_without_flag(self) -> None:
        """Control: the over-cap dump is refused when the override is absent."""
        self._patch_mr_author(_AUTHOR_CAROL)
        service, stub = _service_with_stub(mr_author=_AUTHOR_CAROL)
        body = (
            "## Problem\n\nThe handler crashes on empty input.\n\n"
            "## Fix\n\nGuard the empty case.\n\n## Verification\n\nAdded a regression test."
        )

        msg, code = service.post_comment("org/repo", 7, body)

        assert code == 1, f"control: over-cap must refuse without flag: code={code} msg={msg!r}"
        assert stub.calls == [], "shape gate must block BEFORE any GitLab POST"

    def test_over_cap_allowed_with_flag(self) -> None:
        """The escape works: the same dump proceeds with ``allow_long_review=True``."""
        self._patch_mr_author(_AUTHOR_CAROL)
        service, stub = _service_with_stub(mr_author=_AUTHOR_CAROL)
        body = (
            "## Problem\n\nThe handler crashes on empty input.\n\n"
            "## Fix\n\nGuard the empty case.\n\n## Verification\n\nAdded a regression test."
        )

        msg, code = service.post_comment("org/repo", 7, body, allow_long_review=True)

        assert code == 0, f"override must let the long review proceed: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls), "API POST must fire when the override is set"

    def test_gate_function_returns_empty_with_override(self) -> None:
        """Direct unit: the override short-circuits to ``""`` (proceed) on a colleague MR."""
        from teatree.cli import review_shape_gate as gate_mod  # noqa: PLC0415
        from teatree.cli.review_shape_gate import check_review_shape  # noqa: PLC0415

        self.monkeypatch.setattr(gate_mod, "fetch_mr_author", lambda api, encoded_repo, mr: _AUTHOR_CAROL)

        class _Api:
            def current_username(self) -> str:
                return _AUTHOR_ALICE

        body = "word " * (gate_mod.COLLEAGUE_PROSE_CAP_WORDS + 50)
        assert (
            check_review_shape(
                api=cast("Any", _Api()),
                encoded_repo="org%2Frepo",
                mr=7,
                body=body,
                inline=False,
                allow_long_review=True,
            )
            == ""
        )

    def test_fail_open_carveout_unchanged_by_flag(self) -> None:
        """Own-MR carve-out is preserved: an own MR proceeds with OR without the flag."""
        self._patch_mr_author(_AUTHOR_ALICE)  # own MR (== current identity)
        service, stub = _service_with_stub(mr_author=_AUTHOR_ALICE)
        body = "S1. S2. S3. S4. S5. S6. " * 20  # well over cap

        for flag in (False, True):
            stub.calls.clear()
            msg, code = service.post_comment("org/repo", 7, body, allow_long_review=flag)
            assert code == 0, f"own-MR must pass regardless of flag={flag}: code={code} msg={msg!r}"


class TestShapeGateCachesMRAuthor:
    """``fetch_mr_author`` caches per ``(encoded_repo, mr)`` with 5-min TTL.

    The cache shape mirrors :func:`GitLabHTTPClient.current_username` — a
    single ``_set_cached``/``_get_cached`` round-trip on the API client.
    A second post on the same MR must reuse the cached author and skip the
    GET.
    """

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)

    def test_fetch_mr_author_uses_api_cache(self) -> None:
        """Two posts on the same MR — only one GET to ``merge_requests/<iid>``."""
        from teatree.backends.gitlab.api import GitLabHTTPClient  # noqa: PLC0415
        from teatree.cli.review_shape_gate import fetch_mr_author  # noqa: PLC0415

        api = GitLabHTTPClient(token="t")
        gets: list[str] = []

        def _fake_get(endpoint: str) -> dict[str, object]:
            gets.append(endpoint)
            return {"author": {"username": _AUTHOR_CAROL}}

        with patch.object(api, "get_json", _fake_get):
            a1 = fetch_mr_author(api, "org%2Frepo", 7)
            a2 = fetch_mr_author(api, "org%2Frepo", 7)

        assert a1 == _AUTHOR_CAROL == a2
        assert len(gets) == 1, f"second call must hit cache, not GitLab; gets={gets}"


class TestShapeGateFailOpenAndCarveOuts:
    """Fail-open branches and degenerate shapes — the gate must not break the existing call sites.

    Covers the defensive paths the gate must satisfy:

    * Empty MR author (GET returned no ``author`` key) — fail-open.
    * Missing ``current_username`` on the API (test stub) — fail-open.
    * Empty ``current_username()`` return — fail-open.
    * Empty body — fast-path proceed (no GET fired).
    * Inline note exceeding the inline cap on a colleague MR — refused
        with the ``inline note`` wording.
    * MR-level note just over the char cap (but under the sentence cap)
        — refused.
    * Network failure on the GitLab GET — fail-open.
    """

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        self.monkeypatch = monkeypatch

    def test_empty_body_returns_proceed_without_api_call(self) -> None:
        """Empty body short-circuits before the MR-author GET fires."""
        from teatree.cli.review_shape_gate import check_review_shape  # noqa: PLC0415

        class _NoAPI:
            def get_json(self, _endpoint: str) -> object:
                msg = "must not GET on empty body"
                raise AssertionError(msg)

        assert (
            check_review_shape(
                api=cast("Any", _NoAPI()),
                encoded_repo="org%2Frepo",
                mr=7,
                body="",
                inline=False,
            )
            == ""
        )

    def test_fetch_returns_empty_when_author_missing(self) -> None:
        """A GET that returns no ``author`` key yields empty author → not a colleague MR."""
        from teatree.backends.gitlab.api import GitLabHTTPClient  # noqa: PLC0415
        from teatree.cli.review_shape_gate import fetch_mr_author, is_colleague_mr  # noqa: PLC0415

        api = GitLabHTTPClient(token="t")
        with patch.object(api, "get_json", lambda _e: {"id": 1}):  # no `author` key
            assert fetch_mr_author(api, "org%2Frepo", 7) == ""
        # No author → not a colleague MR (fail-open).
        with patch.object(api, "get_json", lambda _e: {"id": 1}):
            assert is_colleague_mr(api, "org%2Frepo", 7) is False

    def test_fetch_returns_empty_on_network_exception(self) -> None:
        """``api.get_json`` raising (401, network) yields empty author → fail-open."""
        from teatree.backends.gitlab.api import GitLabHTTPClient  # noqa: PLC0415
        from teatree.cli.review_shape_gate import fetch_mr_author  # noqa: PLC0415

        api = GitLabHTTPClient(token="t")

        def _boom(_endpoint: str) -> object:
            msg = "network down"
            raise RuntimeError(msg)

        with patch.object(api, "get_json", _boom):
            assert fetch_mr_author(api, "org%2Frepo", 7) == ""

    def test_is_colleague_mr_returns_false_when_current_username_missing(self) -> None:
        """An API stub without ``current_username`` is treated as can't-tell → fail-open."""
        from teatree.cli.review_shape_gate import is_colleague_mr  # noqa: PLC0415

        class _NoUsernameAPI:
            def get_json(self, _endpoint: str) -> dict[str, object]:
                return {"author": {"username": _AUTHOR_CAROL}}

        assert is_colleague_mr(cast("Any", _NoUsernameAPI()), "org%2Frepo", 7) is False

    def test_is_colleague_mr_returns_false_when_current_username_empty(self) -> None:
        """An empty ``current_username()`` return (no GitLab token) is can't-tell → fail-open."""
        from teatree.cli.review_shape_gate import is_colleague_mr  # noqa: PLC0415

        class _EmptyMeAPI:
            def get_json(self, _endpoint: str) -> dict[str, object]:
                return {"author": {"username": _AUTHOR_CAROL}}

            def current_username(self) -> str:
                return ""

        assert is_colleague_mr(cast("Any", _EmptyMeAPI()), "org%2Frepo", 7) is False

    def test_inline_note_over_paragraph_cap_is_refused(self) -> None:
        """A multi-section inline note breaches the paragraph cap and is refused.

        The carve-out for inline reviews accepts multi-sentence prose
        but not multi-section dumps. A 4-paragraph inline body still
        gets refused with the ``inline note`` wording so the agent
        knows to tighten it.
        """
        from teatree.cli.review_shape_gate import check_review_shape  # noqa: PLC0415

        class _ColleagueAPI:
            def get_json(self, _endpoint: str) -> dict[str, object]:
                return {"author": {"username": _AUTHOR_CAROL}}

            def current_username(self) -> str:
                return _AUTHOR_ALICE

        body = "P1.\n\nP2.\n\nP3.\n\nP4."
        msg = check_review_shape(
            api=cast("Any", _ColleagueAPI()),
            encoded_repo="org%2Frepo",
            mr=7,
            body=body,
            inline=True,
        )
        assert "inline note" in msg
        assert "paragraph" in msg

    def test_mr_level_note_over_word_cap_is_refused(self) -> None:
        """An MR-level note over the 200-word cap is refused.

        The MR-level prose rule caps on paragraph count OR word count.
        A single-paragraph body that exceeds 200 words is still too
        much surface for an MR-level on-behalf post on a colleague MR.
        """
        from teatree.cli.review_shape_gate import check_review_shape  # noqa: PLC0415

        class _ColleagueAPI:
            def get_json(self, _endpoint: str) -> dict[str, object]:
                return {"author": {"username": _AUTHOR_CAROL}}

            def current_username(self) -> str:
                return _AUTHOR_ALICE

        # 250 words, all in one paragraph — keyed on the word cap.
        body = " ".join(["word"] * 250)
        msg = check_review_shape(
            api=cast("Any", _ColleagueAPI()),
            encoded_repo="org%2Frepo",
            mr=7,
            body=body,
            inline=False,
        )
        assert "MR-level prose" in msg
        assert "word" in msg

    def test_fetch_returns_empty_on_non_dict_response(self) -> None:
        """``api.get_json`` returning ``None`` or a list (not a dict) → empty author."""
        from teatree.backends.gitlab.api import GitLabHTTPClient  # noqa: PLC0415
        from teatree.cli.review_shape_gate import fetch_mr_author  # noqa: PLC0415

        api = GitLabHTTPClient(token="t")
        with patch.object(api, "get_json", lambda _e: None):
            assert fetch_mr_author(api, "org%2Frepo", 7) == ""

    def test_mr_level_note_under_both_caps_proceeds(self) -> None:
        """A short MR-level note (1 sentence, well under 280 chars) is accepted.

        This is the "happy path" for general notes — the gate must not
        get in the way of legitimately terse summary lines.
        """
        from teatree.cli.review_shape_gate import check_review_shape  # noqa: PLC0415

        class _ColleagueAPI:
            def get_json(self, _endpoint: str) -> dict[str, object]:
                return {"author": {"username": _AUTHOR_CAROL}}

            def current_username(self) -> str:
                return _AUTHOR_ALICE

        msg = check_review_shape(
            api=cast("Any", _ColleagueAPI()),
            encoded_repo="org%2Frepo",
            mr=7,
            body="LGTM.",
            inline=False,
        )
        assert msg == ""

    def test_count_paragraphs_splits_on_blank_lines(self) -> None:
        """Paragraph count splits on one or more blank lines.

        Empty input is zero; single-line non-empty is 1; blank-line
        separators split into multiple paragraphs.
        """
        from teatree.cli.review_shape_gate import _count_paragraphs  # noqa: PLC0415

        assert _count_paragraphs("") == 0
        assert _count_paragraphs("   ") == 0
        assert _count_paragraphs("just one paragraph") == 1
        assert _count_paragraphs("first\n\nsecond") == 2
        assert _count_paragraphs("a\n\nb\n\nc") == 3
        # Multiple blank lines collapse to one separator.
        assert _count_paragraphs("a\n\n\n\nb") == 2
        # Leading/trailing blank lines do not introduce extra paragraphs.
        assert _count_paragraphs("\n\na\n\n") == 1

    def test_count_words_splits_on_whitespace(self) -> None:
        """Word count uses whitespace splitting (``str.split()``)."""
        from teatree.cli.review_shape_gate import _count_words  # noqa: PLC0415

        assert _count_words("") == 0
        assert _count_words("   ") == 0
        assert _count_words("one") == 1
        assert _count_words("one two three") == 3
        assert _count_words("one\ntwo\tthree") == 3
