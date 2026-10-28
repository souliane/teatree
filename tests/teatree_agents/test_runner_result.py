"""Result-envelope extraction contracts."""

import json

import pytest

from teatree.agents.attempt_recorder import ResultEnvelopeError
from teatree.agents.runner_result import parse_result

# --- Pure function tests (no DB) ---


def test_parse_result_extracts_last_json_line() -> None:
    stdout = "Loading skills...\nRunning task...\n" + json.dumps({"summary": "OK"}) + "\n"
    assert parse_result(stdout) == {"summary": "OK"}


def test_parse_result_returns_empty_dict_for_no_json() -> None:
    assert parse_result("no json here\n") == {}


def test_parse_result_skips_malformed_json() -> None:
    assert parse_result("{bad json\n") == {}


def test_parse_result_extracts_pretty_printed_multiline_json() -> None:
    # A pretty-printed final object spans multiple lines; a line-based scan never
    # parsed it and degraded to truncated prose (breaking the #1284 gate).
    stdout = 'Running task...\n{\n  "summary": "done",\n  "phase": "review"\n}\n'
    assert parse_result(stdout) == {"summary": "done", "phase": "review"}


def test_parse_result_returns_last_object_when_several_present() -> None:
    stdout = '{"summary": "first"}\nmore progress\n{\n  "summary": "final"\n}\n'
    assert parse_result(stdout) == {"summary": "final"}


def test_parse_result_ignores_inner_braces_of_multiline_object() -> None:
    stdout = 'progress\n{\n  "summary": "ok",\n  "nested": {"k": "v"}\n}\n'
    assert parse_result(stdout) == {"summary": "ok", "nested": {"k": "v"}}


def test_early_closed_envelope_refuses_nested_fragment() -> None:
    raw = '{"summary":"ok"},"skill_application":[{"skill":"x","evidence":"y"}],"needs_user_input":false}'
    with pytest.raises(ResultEnvelopeError, match=r"^result is not valid JSON: "):
        parse_result(raw)


def test_unclosed_envelope_refuses_nested_fragment() -> None:
    raw = '{"summary":"ok","adequacy":{"design":"x"}'
    with pytest.raises(ResultEnvelopeError, match=r"^result is not valid JSON: "):
        parse_result(raw)


def test_unclosed_pretty_envelope_does_not_return_line_started_inner_object() -> None:
    raw = '{"summary":"draft","adequacy":\n{"design":"nested"}'
    with pytest.raises(ResultEnvelopeError, match=r"^result is not valid JSON: "):
        parse_result(raw)


def test_unterminated_string_is_reported_as_malformed_envelope() -> None:
    raw = '{"summary":"draft","detail":"unfinished'
    with pytest.raises(ResultEnvelopeError, match=r"^result is not valid JSON: "):
        parse_result(raw)


def test_escaped_quote_and_braces_do_not_expose_later_nested_object() -> None:
    nested = [
        {"skill": "x", "evidence": 'escaped " }}} text'},
        {"summary": "nested fragment"},
    ]
    raw = '{"summary":"draft"},"skill_application":' + json.dumps(nested) + "}"
    with pytest.raises(ResultEnvelopeError, match=r"^result is not valid JSON: "):
        parse_result(raw)


def test_later_corrected_envelope_replaces_malformed_draft() -> None:
    bad = '{"summary":"draft"},"skill_application":[{"skill":"x","evidence":"y"}]}'
    good = '{"summary":"corrected","needs_user_input":false}'
    assert parse_result(bad + "\n" + good) == {"summary": "corrected", "needs_user_input": False}


@pytest.mark.parametrize(
    "prefix",
    [
        '{"summary":"draft"',
        '{"summary":"draft","detail":"unfinished',
        "Narration {} ,",
        '{"summary":"draft"\n{"summary":"first correction"}',
    ],
)
def test_later_line_envelope_recovers_after_poisoned_prefix(prefix: str) -> None:
    good = '{"summary":"corrected","needs_user_input":false}'
    assert parse_result(prefix + "\n" + good) == {"summary": "corrected", "needs_user_input": False}


def test_malformed_tail_after_complete_envelope_is_refused() -> None:
    with pytest.raises(ResultEnvelopeError, match=r"^result is not valid JSON: "):
        parse_result('{"summary":"complete"}\n{"summary":"broken"')


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('prose {x}\n{"summary":"done"}', {"summary": "done"}),
        ('\x60\x60\x60json\n{"summary":"done"}\n\x60\x60\x60', {"summary": "done"}),
        ('[{"summary":"wrapped"}]', {"summary": "wrapped"}),
        (
            json.dumps({"summary": 'a { brace } and " quote'}) + " trailing prose",
            {"summary": 'a { brace } and " quote'},
        ),
    ],
)
def test_preserved_embedded_json_shapes(raw: str, expected: dict[str, object]) -> None:
    assert parse_result(raw) == expected
