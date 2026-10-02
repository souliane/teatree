"""The dispatch ``[quote-ok: <reason>]`` placement contract (#1401 friction, MR !225 finding).

The gate refused a brief that carried the owner's authorisation quoted verbatim
— which a recorded rule REQUIRES a brief to carry — and the escape its own
refusal named did not work: ``/t3:rules`` § "Sub-Agent Limitations" mandates the
multi-kilobyte ``skill-preamble`` be PREPENDED to every raw ``Agent``/``Task``
brief, so a token written "near the start of the prompt" landed past the
512-character recognition window and was silently ignored.

A prior revision "fixed" that by recognising the token anywhere in the payload
when it stood alone on its own line — which is exactly the shape a COPIED
document produces (a fenced code block, a pasted log, relayed meeting notes),
so a token merely quoted inside relayed content authorised the WHOLE dispatch.
:class:`TestCopiedTokenNeverAuthorisesTheWholeDispatch` is the red-first pin for
that privilege escalation. :class:`TestDescriptionFieldSurvivesALongPreamble` is
the over-block check: the legitimate escape (the short ``description`` field,
always inside its own window) must still work despite a huge ``prompt`` and a
decoy token buried in it. :class:`TestWindowIntentPreserved` is the pre-existing
anti-vacuity half — mid-line and blockquote placements still don't authorise.

Synthetic fixtures only — no real user quotes.
"""

import pytest

from teatree.hooks import _dispatch_quote_ok
from teatree.hooks.quote_gate_messages import format_dispatch_block_message
from teatree.hooks.quote_scanner import (
    dispatch_quote_ok_reason,
    dispatch_quote_ok_reason_for_input,
    extract_dispatch_payload,
    scan_text,
)

_REASON = "relaying the owner's authorisation verbatim to the sub-agent"
_TOKEN = f"[quote-ok: {_REASON}]"

# The machine-generated prefix `t3 <overlay> skill-preamble` emits, long enough
# to push the authored brief past the recognition window (as the real one does by
# orders of magnitude).
_PREAMBLE = "--- SKILL: t3:rules ---\n" + ("Cross-cutting agent rules body line.\n" * 40)

# An authorisation relayed into the brief: a blockquote-attributed user quote,
# which is exactly what `blockquote-attributed` fires on.
_AUTHORISED_BRIEF = (
    f"{_TOKEN}\n\nFix the gate frictions.\n\nThe owner authorised this, verbatim:\n\n"
    '> "Make them stop bothering us, whatever it takes."\n'
)

# A copied fenced code block — the shape a pasted document actually takes. The
# token sits ALONE on its own line inside it, exactly the case the prior
# own-line-anywhere recognition wrongly authorised on.
_COPIED_FENCED_DOCUMENT = (
    "```text\n"
    "Notes copied from an old planning doc, kept for reference:\n"
    f"{_TOKEN}\n"
    "End of pasted notes — unrelated to this dispatch.\n"
    "```\n"
)


class TestCopiedTokenNeverAuthorisesTheWholeDispatch:
    """A token merely quoted inside pasted/relayed content is not an authoring act — MAJOR fix."""

    def test_a_standalone_token_inside_a_copied_fenced_block_does_not_authorise(self) -> None:
        payload = _PREAMBLE + "\n\nFix the gate frictions.\n\n" + _COPIED_FENCED_DOCUMENT
        assert len(payload) > _dispatch_quote_ok.TOKEN_WINDOW, "the token must sit past the window, or this is vacuous"
        assert dispatch_quote_ok_reason(payload) is None

    def test_the_same_shape_does_not_authorise_via_the_prompt_field_either(self) -> None:
        tool_input = {
            "description": "Fix the gate frictions.",
            "prompt": _PREAMBLE + "\n\n" + _COPIED_FENCED_DOCUMENT,
        }
        assert scan_text(extract_dispatch_payload("Task", tool_input)).has_high is False
        assert dispatch_quote_ok_reason_for_input(tool_input) is None


class TestDescriptionFieldSurvivesALongPreamble:
    """The legitimate escape still works — over-block check, symmetric to the bypass check above."""

    def test_token_in_the_one_line_subject_authorises(self) -> None:
        tool_input = {
            "description": f"Fix the gate frictions {_TOKEN}",
            "prompt": _PREAMBLE + '\n\nThe owner said:\n\n> "Do it."\n',
        }
        assert scan_text(extract_dispatch_payload("Task", tool_input)).has_high
        assert dispatch_quote_ok_reason_for_input(tool_input) == _REASON

    def test_the_real_token_in_description_authorises_even_beside_a_decoy_in_the_prompt(self) -> None:
        # A copied fenced block sits in `prompt` (the decoy this class exists to
        # rule out as a distraction); the genuine author-written escape is in
        # `description`. The decoy must not matter either way.
        tool_input = {
            "description": f"Fix the gate frictions {_TOKEN}",
            "prompt": _PREAMBLE + "\n\n" + _COPIED_FENCED_DOCUMENT.replace(_REASON, "an unrelated old reason"),
        }
        assert dispatch_quote_ok_reason_for_input(tool_input) == _REASON

    def test_a_long_description_no_longer_eats_the_prompt_budget(self) -> None:
        # Joined-payload windowing let one field consume the other's budget; each
        # field now gets its own.
        tool_input = {"description": "x" * (_dispatch_quote_ok.TOKEN_WINDOW * 2), "prompt": f"{_TOKEN}\n\nbrief"}
        assert dispatch_quote_ok_reason_for_input(tool_input) == _REASON


class TestWindowIntentPreserved:
    """What the positional window existed to refuse is still refused."""

    def test_token_arriving_mid_line_inside_relayed_content_does_not_authorise(self) -> None:
        payload = _PREAMBLE + "\n\nthe pasted document mentions " + _TOKEN + " in passing.\n"
        assert dispatch_quote_ok_reason(payload) is None

    def test_token_inside_a_relayed_blockquote_does_not_authorise(self) -> None:
        payload = _PREAMBLE + f"\n\nthey wrote: > {_TOKEN} <- not ours\n"
        assert dispatch_quote_ok_reason(payload) is None

    @pytest.mark.parametrize("token", ["[quote-ok:]", "[quote-ok: ]", "[quote-ok:   ]"])
    def test_an_empty_reason_never_authorises(self, token: str) -> None:
        assert dispatch_quote_ok_reason(f"{token}\n\nbrief") is None

    def test_no_token_at_all_does_not_authorise(self) -> None:
        assert dispatch_quote_ok_reason(_AUTHORISED_BRIEF.replace(_TOKEN, "")) is None


class TestRefusalNamesAWorkingPlacement:
    """The refusal must point at somewhere the token is actually read."""

    @pytest.fixture
    def message(self) -> str:
        return format_dispatch_block_message(scan_text(_AUTHORISED_BRIEF))

    def test_names_the_description_field_and_its_window(self, message: str) -> None:
        assert "description" in message
        assert str(_dispatch_quote_ok.TOKEN_WINDOW) in message

    def test_does_not_advertise_an_own_line_anywhere_placement(self, message: str) -> None:
        # The prior revision's escape — own-line, unwindowed — no longer exists,
        # so the refusal must not tell an agent it works.
        assert "OWN LINE" not in message
        assert "anywhere in the prompt" not in message

    def test_states_the_window_so_a_buried_token_is_not_expected_to_work(self, message: str) -> None:
        assert str(_dispatch_quote_ok.TOKEN_WINDOW) in message
        assert "skill preamble" in message

    def test_names_relayed_authorisation_as_a_legitimate_reason(self, message: str) -> None:
        assert "authorisation" in message

    def test_does_not_repeat_the_placement_that_silently_failed(self, message: str) -> None:
        assert "near the start of the prompt" not in message
