"""Tests for the dream transcript line-keeping concern (#1933)."""

import json

from django.test import TestCase

from teatree.loops.dream.transcript_extract import (
    decode_transcript_line,
    high_signal_lines,
    looks_like_agent_authored_turn,
    looks_like_learning,
    looks_like_user_ask,
    looks_like_user_correction,
    user_ask_lines,
)


class LooksLikeUserCorrectionTestCase(TestCase):
    """The pure heuristic that keeps a user-correction turn carrying no keyword (#1933)."""

    def test_imperative_negation_is_flagged(self) -> None:
        assert looks_like_user_correction('{"type":"user","text":"do not build a new banner"}')

    def test_told_you_is_flagged(self) -> None:
        assert looks_like_user_correction('{"type":"user","text":"I told you this already"}')

    def test_again_is_flagged(self) -> None:
        assert looks_like_user_correction('{"type":"user","text":"you did it again"}')

    def test_stop_is_flagged(self) -> None:
        assert looks_like_user_correction('{"type":"user","text":"stop — that is wrong"}')

    def test_never_is_flagged(self) -> None:
        assert looks_like_user_correction('{"type":"user","text":"never do that"}')

    def test_why_question_is_flagged(self) -> None:
        assert looks_like_user_correction('{"type":"user","text":"why did you do that?"}')

    def test_repeated_bang_is_flagged(self) -> None:
        assert looks_like_user_correction('{"type":"user","text":"no!! that is broken"}')

    def test_role_user_shape_is_flagged(self) -> None:
        assert looks_like_user_correction('{"role":"user","content":"stop doing that"}')

    def test_neutral_assistant_line_is_not_flagged(self) -> None:
        assert not looks_like_user_correction('{"type":"assistant","text":"computed result row 7"}')

    def test_neutral_user_line_without_cue_is_not_flagged(self) -> None:
        assert not looks_like_user_correction('{"type":"user","text":"please add a new endpoint here"}')

    def test_only_user_turns_are_considered(self) -> None:
        # An assistant line with a frustration cue is the agent's own text, not a correction.
        assert not looks_like_user_correction('{"type":"assistant","text":"do not worry, again it works"}')

    def test_why_without_question_mark_is_not_flagged(self) -> None:
        assert not looks_like_user_correction('{"type":"user","text":"the reason why it works is clear"}')


class LooksLikeUserAskTestCase(TestCase):
    """The keyword-blind keeper for a USER directive/request the loop could automate.

    The sibling of :func:`looks_like_user_correction`: a user turn that reads like a
    manual ask t3 could take over (imperative request OR operational urgency). An
    assistant turn echoing the same words is NOT a user ask.
    """

    def test_can_you_imperative_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"can you push the branch now"}')

    def test_please_request_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"please open the PR for me"}')

    def test_i_need_you_to_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"i need you to set up the worktree"}')

    def test_i_want_you_to_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"i want you to merge it"}')

    def test_lets_collective_imperative_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"let\'s ship this today"}')

    def test_we_should_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"we should run the tests first"}')

    def test_set_up_directive_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"set up the dev database again"}')

    def test_make_sure_directive_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"make sure the migrations apply"}')

    def test_go_ahead_and_directive_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"go ahead and deploy it"}')

    def test_could_you_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"could you rebase onto main"}')

    def test_operational_hotfix_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"we have a hotfix that needs to go out"}')

    def test_operational_urgent_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"this is urgent, the build is red"}')

    def test_operational_asap_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"merge it asap"}')

    def test_operational_drop_everything_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"drop everything and look at this"}')

    def test_operational_rollback_is_flagged(self) -> None:
        assert looks_like_user_ask('{"type":"user","text":"we need a rollback of that change"}')

    def test_role_user_shape_is_flagged(self) -> None:
        assert looks_like_user_ask('{"role":"user","content":"please run the suite"}')

    def test_assistant_echo_is_not_flagged(self) -> None:
        # The agent's own text saying "can you" / "please" is not a user ask.
        assert not looks_like_user_ask('{"type":"assistant","text":"can you confirm? please review the PR"}')

    def test_neutral_user_statement_is_not_flagged(self) -> None:
        assert not looks_like_user_ask('{"type":"user","text":"the result row was computed correctly"}')

    def test_user_question_without_directive_is_not_flagged(self) -> None:
        assert not looks_like_user_ask('{"type":"user","text":"what does this function return?"}')

    # #2732: bare incident-STATE words describe a situation, not a request — they
    # over-matched on incident chatter that carried no user ask, so they no longer flag.
    def test_incident_production_state_is_not_flagged(self) -> None:
        assert not looks_like_user_ask('{"type":"user","text":"there is a production issue on checkout"}')

    def test_incident_broken_state_is_not_flagged(self) -> None:
        assert not looks_like_user_ask('{"type":"user","text":"the checkout page is broken right now"}')

    def test_incident_blocker_state_is_not_flagged(self) -> None:
        assert not looks_like_user_ask('{"type":"user","text":"there is a blocker on the release train"}')

    def test_incident_wedged_state_is_not_flagged(self) -> None:
        assert not looks_like_user_ask('{"type":"user","text":"the pipeline looks wedged today"}')


class LooksLikeLearningTestCase(TestCase):
    """The role-agnostic keeper for a SUBSTANTIVE learning line (#2986).

    The richest raw drift is often a declarative finding/decision carrying none of
    :data:`TRANSCRIPT_SIGNALS` and neither a correction nor an ask cue — an
    assistant "root caused X to Y", or a user stating a lesson. This keeper catches
    it, keyword-blind of the literal-signal list and, unlike the correction/ask
    keepers, from EITHER role.
    """

    def test_root_cause_finding_is_flagged(self) -> None:
        assert looks_like_learning('{"type":"assistant","text":"root caused the crash to a missing tenant filter"}')

    def test_turns_out_discovery_is_flagged(self) -> None:
        assert looks_like_learning('{"type":"assistant","text":"turns out the migration was never applied"}')

    def test_the_bug_was_is_flagged(self) -> None:
        assert looks_like_learning('{"type":"assistant","text":"the bug was an off-by-one in the paginator"}')

    def test_decision_is_flagged(self) -> None:
        assert looks_like_learning('{"type":"assistant","text":"decided to split the resolver into two passes"}')

    def test_user_stated_lesson_is_flagged(self) -> None:
        # A lesson stated by the USER (not a correction, not an ask) is still drift.
        assert looks_like_learning('{"type":"user","text":"the reason is the tenant scope is applied too late"}')

    def test_generic_should_have_coordination_is_not_kept_but_real_learning_is(self) -> None:
        # "should have" over-kept generic coordination hindsight that carries no
        # substantive finding, so it was dropped from the cue list; a declarative
        # learning that names the actual cause still rides a real cue.
        assert not looks_like_learning('{"type":"user","text":"we should have pinged the release manager sooner"}')
        assert looks_like_learning('{"type":"assistant","text":"the mistake was not running the gate before pushing"}')

    def test_neutral_status_chatter_is_not_flagged(self) -> None:
        assert not looks_like_learning('{"type":"assistant","text":"computed result row 7"}')

    def test_neutral_filler_prose_is_not_flagged(self) -> None:
        assert not looks_like_learning('{"type":"user","text":"a neutral request with no cue at all here"}')


class UserAskLinesTestCase(TestCase):
    """The sibling of :func:`high_signal_lines` that keeps only user-ask turns."""

    def test_keeps_imperative_ask(self) -> None:
        raw = '{"type":"assistant","text":"noise"}\n{"type":"user","text":"please open the PR for me"}'
        assert "please open the PR for me" in user_ask_lines(raw)

    def test_keeps_operational_ask(self) -> None:
        raw = '{"type":"assistant","text":"noise"}\n{"type":"user","text":"hotfix needs to ship asap"}'
        assert "hotfix needs to ship asap" in user_ask_lines(raw)

    def test_drops_neutral_and_assistant_chatter(self) -> None:
        raw = "\n".join(f'{{"type":"assistant","text":"can you do row {i}"}}' for i in range(20))
        assert user_ask_lines(raw) == ""

    def test_drops_neutral_user_statement(self) -> None:
        raw = '{"type":"user","text":"the build finished and the row count is fine"}'
        assert user_ask_lines(raw) == ""


class HighSignalLinesTestCase(TestCase):
    def test_keeps_keyword_signal_line(self) -> None:
        raw = '{"type":"assistant","text":"noise"}\n{"type":"user","text":"TEATREE GATE BLOCK fired"}'
        assert "TEATREE GATE BLOCK" in high_signal_lines(raw)

    def test_keeps_correction_prose_with_no_keyword(self) -> None:
        raw = '{"type":"assistant","text":"noise"}\n{"type":"user","text":"do not build a new banner, stop"}'
        assert "do not build a new banner" in high_signal_lines(raw)

    def test_drops_neutral_chatter(self) -> None:
        raw = "\n".join(f'{{"type":"assistant","text":"row {i}"}}' for i in range(20))
        assert high_signal_lines(raw) == ""

    def test_keeps_repeated_near_identical_user_turn(self) -> None:
        repeated = '{"type":"user","text":"the authoring UI is still missing from the deliverable"}'
        raw = f'{repeated}\n{repeated}\n{repeated}\n{{"type":"assistant","text":"neutral"}}'
        assert "the authoring UI is still missing" in high_signal_lines(raw)

    def test_user_turn_seen_twice_is_repeated(self) -> None:
        # F6.10: the threshold is "at least 2" (the documented contract), so a turn seen
        # exactly twice IS a repeat and is kept — the old `> 2` needed three and let a
        # twice-repeated correction slip past the keeper.
        twice = '{"type":"user","text":"a neutral request with no cue at all here"}'
        raw = f"{twice}\n{twice}"
        assert "a neutral request with no cue at all here" in high_signal_lines(raw)

    def test_user_turn_seen_once_is_not_repeated(self) -> None:
        # A single, cue-free user turn is not a repeat — nothing keeps it.
        once = '{"type":"user","text":"a neutral request with no cue at all here"}'
        assert high_signal_lines(once) == ""

    def test_keeps_user_ask_prose_for_automation_clustering(self) -> None:
        # A directive ask carries no correction cue and no keyword signal, but the
        # automation half needs it clustered — high_signal_lines must keep it too.
        raw = '{"type":"assistant","text":"noise"}\n{"type":"user","text":"please set up the hotfix lane"}'
        assert "please set up the hotfix lane" in high_signal_lines(raw)

    def test_keeps_substantive_learning_prose_with_no_signal_keyword(self) -> None:
        # #2986: a declarative learning carries no literal signal token and neither a
        # correction nor an ask cue, yet it is the day's richest drift — the input the
        # keyword gate starved before. high_signal_lines must keep it (either role).
        learning = '{"type":"assistant","text":"root caused the empty owner crash to a missing tenant filter"}'
        raw = f'{{"type":"assistant","text":"row noise"}}\n{learning}'
        assert "root caused the empty owner crash" in high_signal_lines(raw)

    def test_kept_line_is_decoded_not_escaped(self) -> None:
        # The distiller prompt and the grounding index must share ONE decoded form:
        # an escaped em-dash / inner quote decodes so a model's verbatim citation of
        # the human text is a real substring of the kept snippet.
        raw = json.dumps({"type": "user", "message": {"role": "user", "content": 'stop — the "banner", again'}})
        kept = high_signal_lines(raw)
        assert 'stop — the "banner", again' in kept
        assert "\\u2014" not in kept
        assert '\\"' not in kept


class DecodeTranscriptLineTestCase(TestCase):
    """The raw JSONL line → decoded, single-line human text seam that lets citations ground (#1933)."""

    def test_decodes_escaped_message_content(self) -> None:
        raw = json.dumps({"type": "user", "message": {"role": "user", "content": 'do not build a new "banner" — stop'}})
        assert decode_transcript_line(raw) == '{"role": "user"} do not build a new "banner" — stop'

    def test_retains_role_tag_so_correction_heuristic_still_fires(self) -> None:
        # compliance + the weight ladder re-run looks_like_user_correction on the
        # DECODED stream; the retained JSON role tag keeps that per-line signal alive.
        raw = json.dumps({"type": "user", "message": {"role": "user", "content": "do not do that again"}})
        decoded = decode_transcript_line(raw)
        assert looks_like_user_correction(decoded)

    def test_flattens_content_block_list(self) -> None:
        raw = json.dumps(
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "a finding"}]}}
        )
        assert decode_transcript_line(raw) == '{"role": "assistant"} a finding'

    def test_collapses_internal_newlines_to_one_line(self) -> None:
        raw = json.dumps({"type": "user", "message": {"role": "user", "content": "line one\nline two"}})
        assert decode_transcript_line(raw) == '{"role": "user"} line one line two'

    def test_tool_result_content_is_decoded(self) -> None:
        raw = json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": [{"type": "text", "text": "DENIED by gate"}]}],
                },
            }
        )
        assert decode_transcript_line(raw) == '{"role": "tool_result"} DENIED by gate'

    def test_non_json_line_passes_through_unchanged(self) -> None:
        assert decode_transcript_line("BINDING: never push to a red branch") == "BINDING: never push to a red branch"

    def test_non_message_json_passes_through_unchanged(self) -> None:
        raw = json.dumps({"type": "queue-operation", "operation": "enqueue"})
        assert decode_transcript_line(raw) == raw

    def test_top_level_text_shape_is_decoded(self) -> None:
        raw = json.dumps({"type": "user", "text": "please open the PR"})
        assert decode_transcript_line(raw) == '{"role": "user"} please open the PR'


def _tool_result_turn(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"tool_use_id": "toolu_017v8pK", "type": "tool_result", "content": text}],
            },
        }
    )


def _meta_turn(text: str) -> str:
    return json.dumps(
        {"type": "user", "isMeta": True, "userType": "external", "message": {"role": "user", "content": text}}
    )


class ToolResultTurnTestCase(TestCase):
    """A relayed tool result arrives on the user channel but nobody typed it (#2663)."""

    #: Shell output the harness relayed: imperative-negation cues, zero human intent.
    _OUTPUT = "fatal: not a git repository — do not retry, never force it"

    def test_decodes_with_its_own_role_tag(self) -> None:
        assert decode_transcript_line(_tool_result_turn(self._OUTPUT)) == f'{{"role": "tool_result"}} {self._OUTPUT}'

    def test_raw_turn_is_not_a_correction(self) -> None:
        assert not looks_like_user_correction(_tool_result_turn(self._OUTPUT))

    def test_decoded_turn_is_not_a_correction(self) -> None:
        assert not looks_like_user_correction(decode_transcript_line(_tool_result_turn(self._OUTPUT)))

    def test_decoded_turn_quoting_a_user_role_payload_is_not_a_correction(self) -> None:
        # Tool output routinely echoes transcript JSON; the tag decides, never the payload.
        assert not looks_like_user_correction(decode_transcript_line(_tool_result_turn('{"role": "user"} stop that')))

    def test_raw_turn_is_not_an_ask(self) -> None:
        assert not looks_like_user_ask(_tool_result_turn("please rerun: hotfix branch is urgent"))

    def test_decoded_turn_is_not_an_ask(self) -> None:
        raw = _tool_result_turn("please rerun: hotfix branch is urgent")
        assert not looks_like_user_ask(decode_transcript_line(raw))

    def test_a_gate_denial_inside_a_tool_result_is_still_kept(self) -> None:
        # The keyword keeper is role-agnostic: a BLOCK reason still reaches the distiller.
        kept = high_signal_lines(_tool_result_turn("DENIED by the push gate"))
        assert kept == '{"role": "tool_result"} DENIED by the push gate'

    def test_two_identical_tool_results_are_not_a_repeated_user_turn(self) -> None:
        line = _tool_result_turn("ok")
        assert high_signal_lines(f"{line}\n{line}") == ""


class HarnessMetaTurnTestCase(TestCase):
    """``isMeta`` marks the harness speaking on the user channel — hook feedback, not a person."""

    _STOP_HOOK = "Stop hook feedback: EVIDENCE GATE — this turn states a diagnosis; do not stop"

    def test_decodes_with_the_harness_role_tag(self) -> None:
        assert decode_transcript_line(_meta_turn(self._STOP_HOOK)) == f'{{"role": "harness"}} {self._STOP_HOOK}'

    def test_raw_turn_is_not_a_correction(self) -> None:
        assert not looks_like_user_correction(_meta_turn(self._STOP_HOOK))

    def test_decoded_turn_is_not_a_correction(self) -> None:
        assert not looks_like_user_correction(decode_transcript_line(_meta_turn(self._STOP_HOOK)))

    def test_raw_turn_is_not_an_ask(self) -> None:
        assert not looks_like_user_ask(_meta_turn("A session-scoped Stop hook is now active; please acknowledge"))

    def test_task_notification_turn_is_agent_authored(self) -> None:
        line = '{"role": "user"} <task-notification> <summary>background work no longer running</summary>'
        assert looks_like_agent_authored_turn(line)


class HumanTurnPositiveControlTestCase(TestCase):
    """The narrowing must not cost a single genuine typed correction (#2663)."""

    def test_a_typed_correction_is_still_a_correction(self) -> None:
        raw = json.dumps({"type": "user", "message": {"role": "user", "content": "why was it closed????? "}})
        assert looks_like_user_correction(raw)
        assert looks_like_user_correction(decode_transcript_line(raw))

    def test_a_typed_correction_is_not_agent_authored(self) -> None:
        assert not looks_like_agent_authored_turn('{"role": "user"} why was it closed?????')

    def test_a_synthesised_slack_dm_turn_is_still_a_correction(self) -> None:
        # slack_corpus renders the owner's DMs into this shape; it carries neither marker.
        raw = json.dumps({"type": "user", "message": {"role": "user", "content": "stop the digest, I told you"}})
        assert looks_like_user_correction(decode_transcript_line(raw))

    def test_a_typed_ask_is_still_an_ask(self) -> None:
        raw = json.dumps({"type": "user", "message": {"role": "user", "content": "can you open the PR please"}})
        assert looks_like_user_ask(decode_transcript_line(raw))
