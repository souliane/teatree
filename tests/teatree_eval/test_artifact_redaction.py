"""Every credential value is redacted from an eval artifact before it reaches disk.

The eval lanes upload transcripts, dashboards and raw run logs, so a credential an
agent echoes, a hook prints, or a provider error quotes would otherwise ride an
artifact out of the runner. The fakes come from :mod:`tests.teatree_eval._redaction_fakes`.
"""

import html
import io
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

from teatree.ci_oauth_switch import CI_OAUTH_POOL_SECRET
from teatree.eval.artifact_redaction import (
    OAUTH_POOL_ENV,
    REDACTED,
    Redactor,
    redact_artifact,
    tee_main,
    write_artifact,
)
from teatree.llm.credentials import AnthropicApiKeyCredential, CredentialSpec
from tests.teatree_eval._redaction_fakes import (
    API_KEY,
    PASS_ONLY,
    POOL,
    ROUTER_KEY,
    SHORT_NAMED,
    SPECIAL,
    SUBSCRIPTION,
    SUFFIXED,
    URL_SPECIAL,
    fake,
    set_fake_credentials,
)


class _FixedSource:
    def __init__(self, value: str) -> None:
        self._value = value

    def lookup(self, _spec: CredentialSpec) -> str:
        return self._value


@pytest.fixture(autouse=True)
def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    set_fake_credentials(monkeypatch)


def test_the_pool_env_is_the_secret_the_oauth_switch_writes() -> None:
    assert OAUTH_POOL_ENV == CI_OAUTH_POOL_SECRET


@pytest.mark.parametrize(
    "value",
    [SUBSCRIPTION, API_KEY, ROUTER_KEY, *POOL, SUFFIXED, SPECIAL],
    ids=["subscription", "api-key", "router-key", "pool-a", "pool-b", "suffixed-env", "special-chars"],
)
def test_every_credential_value_is_redacted(value: str) -> None:
    redacted = redact_artifact(f"<pre>before {value} after</pre>")

    assert redacted == f"<pre>before {REDACTED} after</pre>"


@pytest.mark.parametrize("value", [PASS_ONLY, SHORT_NAMED], ids=["full-length", "short"])
def test_a_value_resolved_only_from_the_pass_store_is_redacted(value: str) -> None:
    assert AnthropicApiKeyCredential(sources=(_FixedSource(value),)).resolve() == value

    assert redact_artifact(f"key={value} used") == f"key={REDACTED} used"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CLAUDE_CODE_OAUTH_TOKEN", SHORT_NAMED),
        ("ANTHROPIC_API_KEY", SHORT_NAMED),
        ("OPENAI_COMPATIBLE_API_KEY", SHORT_NAMED),
        (OAUTH_POOL_ENV, f"{POOL[0]}\n{SHORT_NAMED}"),
    ],
    ids=["subscription", "api-key", "router-key", "pool-line"],
)
def test_a_short_named_credential_is_still_redacted(name: str, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name, value)

    assert redact_artifact(f"Authorization: Bearer {SHORT_NAMED}\n") == f"Authorization: Bearer {REDACTED}\n"


#: Under 8 characters, or all digits, or a JSON/YAML literal: what a summary, a page and a
#: table are full of — tag names, a lone quote, counts, verdicts — and no real key is.
_NOT_SECRETS = ["p", "td", '"', "ab-9", "seven77", "1", "2026", "null", "NULL", "true", "False", "none", "yes", "on"]


@pytest.mark.parametrize("value", _NOT_SECRETS)
def test_a_named_value_that_cannot_be_a_secret_is_left_alone_with_a_warning(
    value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = json.dumps(
        {"scenario": "clean_room", "passed": False, "trials": 1, "baseline": None, "on": "yes", "note": ""}
        | {"reason": "bearer ab-9 seven77", "year": 2026}
    )
    page = '<table><tr><td class="fail">bearer p td</td></tr></table><p class="judge">null</p>'
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "0")
    Redactor.from_environment()
    warning_for_another_value = capsys.readouterr().err
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", value)

    redactor = Redactor.from_environment()

    warning = capsys.readouterr().err
    assert (redactor.redact(summary), redactor.redact(page)) == (summary, page)
    assert json.loads(redactor.redact(summary))["note"] == ""
    assert warning.count("\n") == warning.count("OPENAI_COMPATIBLE_API_KEY") == 1
    assert warning == warning_for_another_value, "the warning must name the variable, never carry its value"


def test_a_named_value_of_8_characters_is_a_secret_wherever_it_appears(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "rk-8ch-x")

    assert redact_artifact("<td>rk-8ch-x</td> xrk-8ch-xy") == f"<td>{REDACTED}</td> x{REDACTED}y"
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "name", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_COMPATIBLE_API_KEY", OAUTH_POOL_ENV]
)
@pytest.mark.parametrize("value", ["123456789012", "9" * 40], ids=["12-digits", "40-digits"])
def test_a_digit_only_named_credential_of_any_length_is_left_alone_with_a_warning(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = json.dumps({"scenario": "clean_room", "run_id": int(value), "cost_usd": 0.0123})
    monkeypatch.setenv(name, value)

    assert redact_artifact(summary) == summary
    warning = capsys.readouterr().err
    assert warning.count("\n") == warning.count(name) == 1
    assert value not in warning


@pytest.mark.parametrize(
    ("name", "value"),
    [("DB_PASSWORD", "1234567890123456"), ("DEPLOY_TOKEN", "12345678901234567890")],
    ids=["16-digit-db-password", "20-digit-deploy-token"],
)
def test_a_digit_only_value_only_its_secret_name_marks_is_still_redacted(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Only the named credentials have a known, never-numeric format; a `*_PASSWORD`
    # of unknown format can be a numeric one, and a missed secret cannot be taken back.
    monkeypatch.setenv(name, value)

    assert redact_artifact(f"password={value}\n") == f"password={REDACTED}\n"
    assert capsys.readouterr().err == ""


def test_a_mixed_named_value_as_long_as_a_numeric_one_is_still_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "12345678901a")

    assert redact_artifact('{"run_id": "12345678901a"}') == f'{{"run_id": "{REDACTED}"}}'
    assert capsys.readouterr().err == ""


_ENCODERS: dict[str, Callable[[str], str]] = {
    "html": html.escape,
    "json": lambda value: json.dumps(value)[1:-1],
    "html-of-json": lambda value: html.escape(json.dumps(value)[1:-1]),
}


@pytest.mark.parametrize("encode", _ENCODERS.values(), ids=_ENCODERS.keys())
def test_an_escaped_copy_is_redacted(encode: Callable[[str], str]) -> None:
    encoded = encode(SPECIAL)
    assert encoded != SPECIAL

    assert redact_artifact(f'{{"output": "{encoded}"}}') == f'{{"output": "{REDACTED}"}}'


def _lower_hex(encoded: str) -> str:
    return re.sub(r"%[0-9A-F]{2}", lambda escape: escape.group().lower(), encoded)


_URL_ENCODERS: dict[str, Callable[[str], str]] = {
    "percent": lambda value: quote(value, safe=""),
    "percent-plus": quote_plus,
    "percent-path": quote,
    "percent-lower-hex": lambda value: _lower_hex(quote(value, safe="")),
    "percent-plus-lower-hex": lambda value: _lower_hex(quote_plus(value)),
    "percent-path-lower-hex": lambda value: _lower_hex(quote(value)),
}


@pytest.mark.parametrize("encode", _URL_ENCODERS.values(), ids=_URL_ENCODERS.keys())
def test_a_url_encoded_copy_is_redacted(encode: Callable[[str], str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGISTRY_PASSWORD", URL_SPECIAL)
    encoded = encode(URL_SPECIAL)
    assert encoded != URL_SPECIAL

    assert redact_artifact(f"GET /login?password={encoded}&next=/") == f"GET /login?password={REDACTED}&next=/"


@pytest.mark.parametrize(
    "fragment",
    [SUBSCRIPTION[:20], SUBSCRIPTION[-20:], SUBSCRIPTION[9:29], POOL[1][:16], POOL[1][-16:]],
    ids=["cut-end", "cut-start", "cut-both", "first-16", "last-16"],
)
def test_a_cut_token_is_redacted(fragment: str) -> None:
    assert redact_artifact(f"hook output: {fragment}\n") == f"hook output: {REDACTED}\n"


def test_a_cut_token_glued_to_other_word_characters_keeps_its_neighbours() -> None:
    assert redact_artifact(f"prefix_{API_KEY[:24]}") == f"prefix_{REDACTED}"


def test_a_fragment_shorter_than_16_characters_survives() -> None:
    text = f"truncated: {SUBSCRIPTION[:15]}"

    assert redact_artifact(text) == text


@pytest.mark.parametrize(
    "token",
    [
        "sk-ant-api03-" + "z" * 40,
        "ghp_" + "x" * 36,
        "ghs_" + "y" * 36,
        "github_pat_" + "w" * 40,
    ],
    ids=["anthropic", "github-classic", "github-app", "github-fine-grained"],
)
def test_a_token_shape_is_redacted_without_being_configured(token: str) -> None:
    assert redact_artifact(f"cat ~/.config: {token}") == f"cat ~/.config: {REDACTED}"


def test_benign_text_and_short_or_non_secret_values_survive() -> None:
    text = (
        "SHORT_TOKEN=short-benign lane=clean_room_lane_label_value "
        "toolu_01abcdefghijklmnop git status && uv run pytest tests/teatree_eval"
    )

    assert redact_artifact(text) == text


def test_write_artifact_writes_and_appends_the_redacted_text(tmp_path: Path) -> None:
    target = tmp_path / "eval-transcripts.html"

    write_artifact(target, f"first {SUBSCRIPTION}\n")
    write_artifact(target, f"second {POOL[0]}\n", append=True)

    assert target.read_text(encoding="utf-8") == f"first {REDACTED}\nsecond {REDACTED}\n"


def _stdin(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8"))


def test_the_tee_redacts_the_stream_and_appends_to_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "eval-run.log"
    log.write_text("attempt 1\n", encoding="utf-8")
    _stdin(monkeypatch, f"RUN scenario\nerror: {API_KEY}\n".encode() + b"bad byte \xff\n")

    assert tee_main([str(log)]) == 0

    expected = f"RUN scenario\nerror: {REDACTED}\nbad byte \N{REPLACEMENT CHARACTER}\n"
    assert capsys.readouterr().out == expected
    assert log.read_text(encoding="utf-8") == "attempt 1\n" + expected


class _LiveJobLog(io.StringIO):
    """Records what a reader of the job log could see at each flush."""

    def __init__(self) -> None:
        super().__init__()
        self.seen_at_flush: list[str] = []

    def flush(self) -> None:
        self.seen_at_flush.append(self.getvalue())


def test_the_tee_flushes_each_line_as_it_arrives(monkeypatch: pytest.MonkeyPatch) -> None:
    live = _LiveJobLog()
    monkeypatch.setattr("sys.stdout", live)
    _stdin(monkeypatch, f"RUN  [1/2] alpha\nDONE [1/2] alpha {API_KEY}\n".encode())

    assert tee_main([]) == 0

    assert live.seen_at_flush == ["RUN  [1/2] alpha\n", f"RUN  [1/2] alpha\nDONE [1/2] alpha {REDACTED}\n"]


def test_the_tee_without_a_log_only_filters_the_stream(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, f"token {SUBSCRIPTION[:30]}\n".encode())

    assert tee_main([]) == 0

    assert capsys.readouterr().out == f"token {REDACTED}\n"


def _large_artifact(planted: list[str], size: int) -> str:
    chunk = (
        '<details class="fail"><summary>scenario_name_here (error_during_execution)</summary>\n'
        '<pre>turn 3: Bash({"command": "git -C /tmp/work/repo status --porcelain", '
        '"description": "Show working tree status"})</pre>\n'
        '<p class="judge">judge (fail): the agent ran 123e4567-e89b-12d3-a456-426614174000 '
        "via toolu_01ABCDEFGHIJKLMNOPQRSTUV and src/teatree/eval/report.py</p>\n"
        '{"scenario": "clean_room", "verdict": "fail", "cost_usd": 0.0123}\n'
    )
    body = chunk * (size // len(chunk))
    third = len(body) // 3
    return "".join((body[:third], planted[0], body[third : 2 * third], planted[1], body[2 * third :], planted[2]))


def _best_of_five(redactor: Redactor, artifact: str) -> tuple[float, str]:
    timings, redacted = [], ""
    for _ in range(5):
        start = time.perf_counter()
        redacted = redactor.redact(artifact)
        timings.append(time.perf_counter() - start)
    return min(timings), redacted


def test_redaction_time_grows_linearly_with_the_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    # A ratio, not a wall-clock bound: a loaded runner slows both sizes alike, while a
    # quadratic scan makes 4x the input cost ~16x the time.
    subscription, pooled, api_key = fake("s", 11), fake("p", 12), fake("k", 13)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", subscription)
    monkeypatch.setenv(OAUTH_POOL_ENV, f"{fake('q', 14)}\n{pooled}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", api_key)
    planted = [f" {subscription[:20]} ", f" {pooled[12:40]}\n", api_key]
    redactor = Redactor.from_environment()

    small, _ = _best_of_five(redactor, _large_artifact(planted, 500_000))
    large, redacted = _best_of_five(redactor, _large_artifact(planted, 2_000_000))

    assert redacted.count(REDACTED) == len(planted)
    assert not any(fragment.strip() in redacted for fragment in planted)
    assert large < 8 * small, f"4x the input took {large / small:.1f}x the time ({small:.3f}s -> {large:.3f}s)"
