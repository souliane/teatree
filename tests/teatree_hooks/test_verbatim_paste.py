"""A published body must not reproduce the operator's own words (#4195).

The measured incident: an agent pasted the operator's chat messages into a
public issue as blockquotes. The banned-terms gate fired on ONE word in that
body, the agent paraphrased that word, re-posted, and the rest of the operator's
verbatim text went out. Clearing a vocabulary check read as clearing the
concern.

These pin the predicate the term list cannot express — is this text someone's
private message being republished? — plus the two postures that make it
trustworthy: the ledger never holds the operator's words, and an unreadable
history reports UNKNOWN rather than clean.
"""

import json
from pathlib import Path

import pytest

from teatree.hooks import verbatim_paste as vp
from teatree.hooks._parser_primitives import FAIL_CLOSED_SENTINEL

_OPERATOR_MESSAGE = (
    "Stop pasting my chat messages into public issues verbatim. I want you to "
    "write the summary in your own words every single time, without exception."
)

_SESSION = "sess-4195"


@pytest.fixture
def ledger_root(tmp_path: Path) -> Path:
    root = tmp_path / "hook-state"
    root.mkdir()
    return root


@pytest.fixture
def recorded(ledger_root: Path) -> Path:
    vp.record_operator_message(_OPERATOR_MESSAGE, session_id=_SESSION, root=ledger_root)
    return ledger_root


class TestTheMeasuredCase:
    """A body with no banned term left, still carrying the operator's words."""

    def test_blockquoted_operator_message_is_refused(self, recorded: Path) -> None:
        body = f"## Why\n\nThe request was:\n\n> {_OPERATOR_MESSAGE}\n\nThat is the scope.\n"
        verdict = vp.scan_body(body, session_id=_SESSION, root=recorded)
        assert verdict.outcome == vp.REPRODUCED

    def test_the_refusal_names_the_offending_span(self, recorded: Path) -> None:
        body = f"> {_OPERATOR_MESSAGE}\n"
        verdict = vp.scan_body(body, session_id=_SESSION, root=recorded)
        assert "pasting my chat messages into public issues" in verdict.span
        assert verdict.words >= vp.QUOTED_RUN_WORDS
        assert verdict.span in vp.format_block_message(verdict)

    def test_the_check_is_independent_of_any_term_list(self, recorded: Path) -> None:
        """No configured term list is consulted — the module never reads one."""
        source = Path(vp.__file__).read_text(encoding="utf-8")
        assert "banned_term" not in source
        assert vp.scan_body(f"> {_OPERATOR_MESSAGE}", session_id=_SESSION, root=recorded).outcome == vp.REPRODUCED


class TestParaphraseIsAllowed:
    def test_a_summary_in_the_agents_own_words_passes(self, recorded: Path) -> None:
        body = "## Why\n\nThe operator asked for their chat text to be summarised, never reproduced.\n"
        assert vp.scan_body(body, session_id=_SESSION, root=recorded).outcome == vp.CLEAN

    def test_a_short_shared_phrase_is_not_reproduction(self, recorded: Path) -> None:
        body = "The fix is to write the summary in a fresh voice.\n"
        assert vp.scan_body(body, session_id=_SESSION, root=recorded).outcome == vp.CLEAN

    def test_a_session_of_only_short_prompts_is_clean_not_unknown(self, ledger_root: Path) -> None:
        vp.record_operator_message("fix it please", session_id="sess-short", root=ledger_root)
        assert vp.scan_body(f"> {_OPERATOR_MESSAGE}", session_id="sess-short", root=ledger_root).outcome == vp.CLEAN


class TestQuotedAndProseWindows:
    """Quoted regions are the sensitive window; prose needs a much longer run."""

    def test_a_short_run_in_prose_is_not_reproduction(self, ledger_root: Path) -> None:
        long_message = " ".join(f"token{index}" for index in range(60))
        vp.record_operator_message(long_message, session_id="sess-window", root=ledger_root)
        prose = "intro " + " ".join(f"token{index}" for index in range(20)) + " outro"
        assert vp.scan_body(prose, session_id="sess-window", root=ledger_root).outcome == vp.CLEAN

    def test_the_same_short_run_inside_a_blockquote_is_reproduction(self, ledger_root: Path) -> None:
        long_message = " ".join(f"token{index}" for index in range(60))
        vp.record_operator_message(long_message, session_id="sess-window", root=ledger_root)
        quoted = "> " + " ".join(f"token{index}" for index in range(10))
        assert vp.scan_body(quoted, session_id="sess-window", root=ledger_root).outcome == vp.REPRODUCED

    def test_a_long_run_in_bare_prose_is_reproduction(self, ledger_root: Path) -> None:
        long_message = " ".join(f"token{index}" for index in range(60))
        vp.record_operator_message(long_message, session_id="sess-window", root=ledger_root)
        assert vp.scan_body(f"intro {long_message} outro", session_id="sess-window", root=ledger_root).outcome == (
            vp.REPRODUCED
        )

    def test_a_double_quoted_span_counts_as_quoted(self, recorded: Path) -> None:
        body = f'The operator wrote "{_OPERATOR_MESSAGE}" and that settles it.'
        assert vp.scan_body(body, session_id=_SESSION, root=recorded).outcome == vp.REPRODUCED

    def test_a_smart_quoted_span_counts_as_quoted(self, recorded: Path) -> None:
        body = f"The operator wrote “{_OPERATOR_MESSAGE}” and that settles it."
        assert vp.scan_body(body, session_id=_SESSION, root=recorded).outcome == vp.REPRODUCED

    def test_a_smart_apostrophe_does_not_defeat_the_quoted_window(self, ledger_root: Path) -> None:
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
        vp.record_operator_message(smart, session_id="sess-smart-apos", root=ledger_root)
        body = "\n".join(f"> {line}" for line in smart.splitlines())
        assert vp.scan_body(body, session_id="sess-smart-apos", root=ledger_root).outcome == vp.REPRODUCED

    def test_a_fenced_command_the_operator_pasted_is_reproducible(self, ledger_root: Path) -> None:
        """A command/log block is a technical artifact, not the operator's voice."""
        fenced = "```\n" + " ".join(f"token{index}" for index in range(60)) + "\n```"
        vp.record_operator_message(f"run this:\n{fenced}", session_id="sess-code", root=ledger_root)
        assert vp.scan_body(f"Reproduce with:\n{fenced}", session_id="sess-code", root=ledger_root).outcome == vp.CLEAN


class TestUnavailableHistoryIsUnknownNotClean:
    def test_a_session_with_no_recorded_history_is_unknown(self, ledger_root: Path) -> None:
        verdict = vp.scan_body(f"> {_OPERATOR_MESSAGE}", session_id="never-seen", root=ledger_root)
        assert verdict.outcome == vp.UNKNOWN
        assert verdict.reason

    def test_a_call_with_no_session_id_is_unknown(self, recorded: Path) -> None:
        assert vp.scan_body(f"> {_OPERATOR_MESSAGE}", session_id="", root=recorded).outcome == vp.UNKNOWN

    def test_a_corrupt_ledger_is_unknown(self, recorded: Path) -> None:
        vp.ledger_path(_SESSION, recorded).write_text("{not json", encoding="utf-8")
        assert vp.scan_body(f"> {_OPERATOR_MESSAGE}", session_id=_SESSION, root=recorded).outcome == vp.UNKNOWN

    def test_a_future_ledger_version_is_unknown(self, recorded: Path) -> None:
        target = vp.ledger_path(_SESSION, recorded)
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["version"] = vp.LEDGER_VERSION + 1
        target.write_text(json.dumps(payload), encoding="utf-8")
        assert vp.scan_body(f"> {_OPERATOR_MESSAGE}", session_id=_SESSION, root=recorded).outcome == vp.UNKNOWN

    def test_the_unknown_note_refuses_to_claim_a_clean_scan(self, ledger_root: Path) -> None:
        verdict = vp.scan_body("anything", session_id="never-seen", root=ledger_root)
        message = vp.format_unknown_message(verdict)
        assert "could NOT check" in message
        assert "UNKNOWN, not a clean scan" in message


class TestAnUnresolvableBodyIsUnknownNotClean:
    """#4195 review finding: a sentinel-carrying payload must not scan CLEAN."""

    def test_the_fail_closed_sentinel_is_unknown(self, recorded: Path) -> None:
        verdict = vp.scan_body(FAIL_CLOSED_SENTINEL, session_id=_SESSION, root=recorded)
        assert verdict.outcome == vp.UNKNOWN
        assert verdict.reason

    def test_the_sentinel_alongside_real_text_is_still_unknown(self, recorded: Path) -> None:
        body = f"Post-mortem\n{FAIL_CLOSED_SENTINEL}"
        assert vp.scan_body(body, session_id=_SESSION, root=recorded).outcome == vp.UNKNOWN


class TestTheLedgerNeverStoresTheOperatorsWords:
    def test_no_word_of_the_message_appears_in_the_ledger(self, recorded: Path) -> None:
        stored = vp.ledger_path(_SESSION, recorded).read_text(encoding="utf-8")
        for word in ("pasting", "chat", "public", "issues", "verbatim", "exception"):
            assert word not in stored

    def test_the_salt_makes_fingerprints_session_local(self, ledger_root: Path) -> None:
        vp.record_operator_message(_OPERATOR_MESSAGE, session_id="a", root=ledger_root)
        vp.record_operator_message(_OPERATOR_MESSAGE, session_id="b", root=ledger_root)
        first = json.loads(vp.ledger_path("a", ledger_root).read_text(encoding="utf-8"))
        second = json.loads(vp.ledger_path("b", ledger_root).read_text(encoding="utf-8"))
        assert first["messages"] != second["messages"]

    def test_the_audit_record_omits_the_span(self, recorded: Path, tmp_path: Path) -> None:
        verdict = vp.scan_body(f"> {_OPERATOR_MESSAGE}", session_id=_SESSION, root=recorded)
        audit = tmp_path / "audit.jsonl"
        vp.log_decision(decision="blocked", verdict=verdict, ledger=audit)
        record = json.loads(audit.read_text(encoding="utf-8").strip())
        assert record["decision"] == "blocked"
        assert record["words"] == verdict.words
        assert "pasting" not in audit.read_text(encoding="utf-8")


class TestRecordingAccumulates:
    def test_a_later_message_does_not_evict_an_earlier_one(self, recorded: Path) -> None:
        second = " ".join(f"other{index}" for index in range(30))
        vp.record_operator_message(second, session_id=_SESSION, root=recorded)
        assert vp.scan_body(f"> {_OPERATOR_MESSAGE}", session_id=_SESSION, root=recorded).outcome == vp.REPRODUCED
        assert vp.scan_body(f"> {second}", session_id=_SESSION, root=recorded).outcome == vp.REPRODUCED

    def test_the_history_window_is_bounded(self, ledger_root: Path) -> None:
        for index in range(vp.MAX_RECORDED_MESSAGES + 5):
            body = f"message number {index} " + " ".join(["filler"] * 10)
            vp.record_operator_message(body, session_id=_SESSION, root=ledger_root)
        stored = json.loads(vp.ledger_path(_SESSION, ledger_root).read_text(encoding="utf-8"))
        assert len(stored["messages"]) <= vp.MAX_RECORDED_MESSAGES

    def test_an_empty_session_id_records_nothing(self, ledger_root: Path) -> None:
        assert vp.record_operator_message(_OPERATOR_MESSAGE, session_id="", root=ledger_root) is False

    def test_an_unwritable_root_degrades_to_unknown_rather_than_raising(self, tmp_path: Path) -> None:
        blocked = tmp_path / "not-a-dir"
        blocked.write_text("", encoding="utf-8")
        assert vp.record_operator_message(_OPERATOR_MESSAGE, session_id=_SESSION, root=blocked / "sub") is False
        assert vp.scan_body("anything", session_id=_SESSION, root=blocked / "sub").outcome == vp.UNKNOWN


class TestOverride:
    def test_a_leading_env_prefix_on_the_publish_segment_overrides(self) -> None:
        assert vp.has_override("cd /tmp && ALLOW_VERBATIM_PASTE=1 gh issue create --body x") is True

    def test_a_decoy_on_an_unrelated_segment_does_not_override(self) -> None:
        assert vp.has_override("echo ALLOW_VERBATIM_PASTE=1 && gh issue create --body x") is False

    def test_a_plain_publish_carries_no_override(self) -> None:
        assert vp.has_override("gh issue create --body x") is False

    def test_the_process_environment_override_is_announced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(vp.OVERRIDE_ENV, "1")
        assert vp.has_override("gh issue create --body x") is True

    def test_the_block_message_names_the_override(self, recorded: Path) -> None:
        verdict = vp.scan_body(f"> {_OPERATOR_MESSAGE}", session_id=_SESSION, root=recorded)
        assert vp.OVERRIDE_ENV in vp.format_block_message(verdict)
