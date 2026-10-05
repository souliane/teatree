"""A published body must not reproduce the operator's own words (#4195).

The measured incident: an agent pasted the operator's chat messages into a
public issue as blockquotes. The banned-terms gate fired on ONE word in that
body, the agent paraphrased that word, re-posted, and the rest of the operator's
verbatim text went out. Clearing a vocabulary check read as clearing the
concern.

These pin the predicate the term list cannot express — is this text someone's
private message being republished? — plus the posture that makes it
trustworthy: an unreadable history reports UNKNOWN rather than clean.
"""

import json
from pathlib import Path

from teatree.hooks import verbatim_paste as vp
from teatree.hooks._parser_primitives import FAIL_CLOSED_SENTINEL

_OPERATOR_MESSAGE = (
    "Stop pasting my chat messages into public issues verbatim. I want you to "
    "write the summary in your own words every single time, without exception."
)

_SAID = [_OPERATOR_MESSAGE]
_LONG = " ".join(f"token{index}" for index in range(60))


class TestTheMeasuredCase:
    """A body with no banned term left, still carrying the operator's words."""

    def test_blockquoted_operator_message_is_refused(self) -> None:
        body = f"## Why\n\nThe request was:\n\n> {_OPERATOR_MESSAGE}\n\nThat is the scope.\n"
        verdict = vp.scan_body(body, operator_messages=_SAID)
        assert verdict.outcome == vp.REPRODUCED

    def test_the_refusal_names_the_offending_span(self) -> None:
        body = f"> {_OPERATOR_MESSAGE}\n"
        verdict = vp.scan_body(body, operator_messages=_SAID)
        assert "pasting my chat messages into public issues" in verdict.span
        assert verdict.words >= vp.QUOTED_RUN_WORDS
        assert verdict.span in vp.format_block_message(verdict)

    def test_the_check_is_independent_of_any_term_list(self) -> None:
        """No configured term list is consulted — the module never reads one."""
        source = Path(vp.__file__).read_text(encoding="utf-8")
        assert "banned_term" not in source
        assert vp.scan_body(f"> {_OPERATOR_MESSAGE}", operator_messages=_SAID).outcome == vp.REPRODUCED


class TestParaphraseIsAllowed:
    def test_a_summary_in_the_agents_own_words_passes(self) -> None:
        body = "## Why\n\nThe operator asked for their chat text to be summarised, never reproduced.\n"
        assert vp.scan_body(body, operator_messages=_SAID).outcome == vp.CLEAN

    def test_a_short_shared_phrase_is_not_reproduction(self) -> None:
        body = "The fix is to write the summary in a fresh voice.\n"
        assert vp.scan_body(body, operator_messages=_SAID).outcome == vp.CLEAN

    def test_a_session_of_only_short_prompts_is_clean_not_unknown(self) -> None:
        assert vp.scan_body(f"> {_OPERATOR_MESSAGE}", operator_messages=["fix it please"]).outcome == vp.CLEAN

    def test_a_session_with_no_owner_messages_is_clean(self) -> None:
        assert vp.scan_body(f"> {_OPERATOR_MESSAGE}", operator_messages=[]).outcome == vp.CLEAN


class TestQuotedAndProseWindows:
    """Quoted regions are the sensitive window; prose needs a much longer run."""

    def test_a_short_run_in_prose_is_not_reproduction(self) -> None:
        prose = "intro " + " ".join(f"token{index}" for index in range(20)) + " outro"
        assert vp.scan_body(prose, operator_messages=[_LONG]).outcome == vp.CLEAN

    def test_the_same_short_run_inside_a_blockquote_is_reproduction(self) -> None:
        quoted = "> " + " ".join(f"token{index}" for index in range(10))
        assert vp.scan_body(quoted, operator_messages=[_LONG]).outcome == vp.REPRODUCED

    def test_a_long_run_in_bare_prose_is_reproduction(self) -> None:
        assert vp.scan_body(f"intro {_LONG} outro", operator_messages=[_LONG]).outcome == (vp.REPRODUCED)

    def test_a_double_quoted_span_counts_as_quoted(self) -> None:
        body = f'The operator wrote "{_OPERATOR_MESSAGE}" and that settles it.'
        assert vp.scan_body(body, operator_messages=_SAID).outcome == vp.REPRODUCED

    def test_a_smart_quoted_span_counts_as_quoted(self) -> None:
        body = f"The operator wrote “{_OPERATOR_MESSAGE}” and that settles it."
        assert vp.scan_body(body, operator_messages=_SAID).outcome == vp.REPRODUCED

    def test_a_smart_apostrophe_does_not_defeat_the_quoted_window(self) -> None:
        """A curly apostrophe must tokenise identically for the recorder and the scan.

        #4195 review finding: the recorder tokenised the raw operator message
        while the quoted-window scan normalised quotes first (``_quoted_regions``
        calls :func:`teatree.hooks._quote_normalize.normalize_quotes`), so
        ``it's`` recorded as two tokens (``it``, ``s``) but scanned as one
        (``it's``) — the shingles never aligned and a smart-quoted blockquote
        paste of the identical smart-quoted message read CLEAN.
        """
        message = (
            "I don't want the factory's internals in a public issue; it's my own words "
            "I'm worried about, and they shouldn't leak."
        )
        smart = message.replace("'", "\u2019")
        body = "\n".join(f"> {line}" for line in smart.splitlines())
        assert vp.scan_body(body, operator_messages=[smart]).outcome == vp.REPRODUCED

    def test_a_fenced_command_the_operator_pasted_is_reproducible(self) -> None:
        """A command/log block is a technical artifact, not the operator's voice."""
        fenced = "```\n" + " ".join(f"token{index}" for index in range(60)) + "\n```"
        said = [f"run this:\n{fenced}"]
        assert vp.scan_body(f"Reproduce with:\n{fenced}", operator_messages=said).outcome == vp.CLEAN

    def test_an_unterminated_fence_does_not_blank_the_rest_of_the_body(self) -> None:
        r"""A stray opening fence with no close must not excise everything after it.

        #4195 review finding (Blocker 3): ``_FENCE_RE`` matched an opening
        ```` ``` ```` through to end-of-string (``\Z``) when no closing fence
        followed, so one unterminated fence marker anywhere in a published body
        made the entire remainder invisible to the gate — the cheapest total
        bypass of the three, requiring no unusual encoding at all. A properly
        CLOSED fence (see the sibling test above) is still excluded by design.
        """
        body = f"Reproduce with:\n```\n{_LONG}"
        assert vp.scan_body(body, operator_messages=[_LONG]).outcome == vp.REPRODUCED


class TestUnavailableHistoryIsUnknownNotClean:
    def test_an_unreadable_history_is_unknown(self) -> None:
        verdict = vp.scan_body(f"> {_OPERATOR_MESSAGE}", operator_messages=None)
        assert verdict.outcome == vp.UNKNOWN
        assert verdict.reason

    def test_the_unknown_note_refuses_to_claim_a_clean_scan(self) -> None:
        message = vp.format_unknown_message(vp.scan_body("anything", operator_messages=None))
        assert "could NOT check" in message
        assert "UNKNOWN, not a clean scan" in message


class TestAnUnresolvableBodyIsUnknownNotClean:
    """#4195 review finding: a sentinel-carrying payload must not scan CLEAN."""

    def test_the_fail_closed_sentinel_is_unknown(self) -> None:
        verdict = vp.scan_body(FAIL_CLOSED_SENTINEL, operator_messages=_SAID)
        assert verdict.outcome == vp.UNKNOWN
        assert verdict.reason

    def test_the_sentinel_alongside_real_text_is_still_unknown(self) -> None:
        body = f"Post-mortem\n{FAIL_CLOSED_SENTINEL}"
        assert vp.scan_body(body, operator_messages=_SAID).outcome == vp.UNKNOWN


class TestTheAuditRecordOmitsTheSpan:
    def test_the_audit_record_omits_the_span(self, tmp_path: Path) -> None:
        verdict = vp.scan_body(f"> {_OPERATOR_MESSAGE}", operator_messages=_SAID)
        audit = tmp_path / "audit.jsonl"
        vp.log_decision(decision="blocked", verdict=verdict, ledger=audit)
        record = json.loads(audit.read_text(encoding="utf-8").strip())
        assert record["decision"] == "blocked"
        assert record["words"] == verdict.words
        assert "pasting" not in audit.read_text(encoding="utf-8")


class TestHistory:
    def test_every_message_in_the_session_counts(self) -> None:
        second = " ".join(f"other{index}" for index in range(30))
        said = [_OPERATOR_MESSAGE, second]
        assert vp.scan_body(f"> {_OPERATOR_MESSAGE}", operator_messages=said).outcome == vp.REPRODUCED
        assert vp.scan_body(f"> {second}", operator_messages=said).outcome == vp.REPRODUCED
