"""Tests for the gate-failure feedback loop (#2024).

Built against the REAL Claude Code on-disk session schema (verified against
``~/.claude/projects/*/*.jsonl``): a gate BLOCK is a ``hook_blocking_error`` with
NO ``exitCode``, identity in ``attachment.blockingError.blockingError`` (text
leading with a ``TEATREE GATE — <phrase>`` marker); ``TEATREE LOOP SELF-PUMP`` is
the same attachment type but a continue-the-loop signal, not a failure; a
``hook_non_blocking_error`` carries ``exitCode:1`` and is an infra/dependency
failure (environmental); ``hookName`` is the EVENT:TOOL label ("Stop",
"PreToolUse:Bash"), never a gate name, and ``command`` is the same runner across
every gate. The fixture ``gate_failures_session.jsonl`` is derived from an actual
on-disk block (the gate's own message text — no PII).
"""

import json
from pathlib import Path

import pytest

from teatree.core.review_findings import FilingContext, FindingsStore
from teatree.eval.gate_failures import (
    GateFailure,
    GateVerdict,
    classify_gate_failure,
    escalate_gate_failures,
    extract_gate_failures,
    gate_identity_slug,
    record_gate_failures,
)
from teatree.eval.session_transcript import SessionEvent, parse_session_jsonl

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "gate_failures_session.jsonl"

_STRUCTURED_QUESTION_MARKER = (
    "TEATREE GATE — a user-directed question was asked inline in prose with no AskUserQuestion tool call in this turn."
)


def _events_from_fixture() -> list[SessionEvent]:
    return parse_session_jsonl(_FIXTURE.read_text(encoding="utf-8"))


class TestExtractGateFailures:
    def test_real_block_yields_a_preventable_failure(self) -> None:
        failures = extract_gate_failures(_events_from_fixture(), session_id="s1")
        slugs = {f.gate for f in failures}
        assert any("user-directed-question" in s for s in slugs)

    def test_self_pump_block_is_not_a_failure(self) -> None:
        failures = extract_gate_failures(_events_from_fixture(), session_id="s1")
        assert not any("self-pump" in f.gate for f in failures)
        assert not any("continue" in f.gate for f in failures)

    def test_passing_hook_is_not_a_failure(self) -> None:
        failures = extract_gate_failures(_events_from_fixture(), session_id="s1")
        assert not any("pretooluse" in f.gate for f in failures)

    def test_blocking_error_with_no_exit_code_is_still_detected(self) -> None:
        # The canonical reader reports hook_exit_code=None for a blocking error,
        # so the extractor must NOT rely on exitCode; detection at all proves it.
        failures = extract_gate_failures(_events_from_fixture(), session_id="s1")
        assert any("user-directed-question" in f.gate for f in failures)

    def test_non_blocking_infra_error_is_extracted_and_environmental(self) -> None:
        failures = extract_gate_failures(_events_from_fixture(), session_id="s1")
        infra = [f for f in failures if classify_gate_failure(f) is GateVerdict.ENVIRONMENTAL]
        assert infra

    def test_serialization_never_carries_raw_message_or_command(self) -> None:
        failures = extract_gate_failures(_events_from_fixture(), session_id="s1")
        payload = json.dumps([f.as_dict() for f in failures])
        assert "AskUserQuestion tool call in this turn" not in payload
        assert "hook_router.py" not in payload
        assert "Plugin directory does not exist" not in payload
        assert "blockingError" not in payload
        assert "command" not in payload
        assert "stderr" not in payload


class TestGateIdentitySlug:
    def test_extracts_minimal_slug_from_teatree_gate_marker(self) -> None:
        slug = gate_identity_slug(_STRUCTURED_QUESTION_MARKER)
        assert "user-directed-question" in slug
        assert len(slug) <= 80

    def test_self_pump_marker_slug_is_recognizable(self) -> None:
        slug = gate_identity_slug("TEATREE LOOP SELF-PUMP — consolidated work remains; continue the loop.")
        assert "self-pump" in slug

    def test_same_gate_text_same_slug(self) -> None:
        a = gate_identity_slug(_STRUCTURED_QUESTION_MARKER + " Re-ask now.")
        b = gate_identity_slug(_STRUCTURED_QUESTION_MARKER + " Different trailing sentence.")
        assert a == b

    def test_non_marker_text_still_yields_a_bounded_slug(self) -> None:
        slug = gate_identity_slug("Failed to run: Plugin directory does not exist: /Users/x/.claude")
        assert slug
        assert len(slug) <= 80


class TestInfraIdentityNeverEchoesStderr:
    """A non-blocking error's arbitrary stderr must never enter the stored slug."""

    @staticmethod
    def _failures(stderr: str, *, hook_name: str = "PostToolUse:Bash") -> list[GateFailure]:
        line = json.dumps(
            {
                "type": "attachment",
                "attachment": {
                    "type": "hook_non_blocking_error",
                    "hookEvent": "PostToolUse",
                    "hookName": hook_name,
                    "exitCode": 1,
                    "toolUseID": "t1",
                    "stderr": stderr,
                    "command": 'node "x.mjs"',
                },
            }
        )
        header = '{"type":"user","message":{"role":"user","content":"work"}}'
        return extract_gate_failures(parse_session_jsonl(f"{header}\n{line}"), session_id="s1")

    def test_sensitive_leading_stderr_is_not_echoed(self) -> None:
        failures = self._failures("leaked-token-abc Failed to run: Plugin directory does not exist")
        assert failures
        assert all("leaked-token-abc" not in f.gate for f in failures)
        assert all(classify_gate_failure(f) is GateVerdict.ENVIRONMENTAL for f in failures)

    def test_unrecognized_stderr_falls_back_to_generic_environmental(self) -> None:
        failures = self._failures("some-private-path /Users/secret/thing blew up unexpectedly")
        assert failures
        assert all("secret" not in f.gate and "private" not in f.gate for f in failures)
        assert all("non-blocking-error" in f.gate for f in failures)
        assert all(classify_gate_failure(f) is GateVerdict.ENVIRONMENTAL for f in failures)

    def test_multi_fragment_stderr_maps_to_one_deterministic_specific_identity(self) -> None:
        # "Failed to run: Plugin directory does not exist" matches BOTH the
        # `failed-to-run` wrapper and the `plugin-directory-does-not-exist` reason.
        # The specific reason must win, identically across processes — a frozenset
        # made this hash-seed dependent (the same stderr fingerprinted two ways).
        failures = self._failures("Failed to run: Plugin directory does not exist: /Users/x/.claude")
        assert len(failures) == 1
        gate = failures[0].gate
        assert gate == "posttooluse:plugin-directory-does-not-exist"
        assert "failed-to-run" not in gate


class TestFingerprint:
    def test_same_gate_hashes_together(self) -> None:
        a = GateFailure(gate="stop:user-directed-question", hook_event="Stop", session_id="s1")
        b = GateFailure(gate="stop:user-directed-question", hook_event="Stop", session_id="s2")
        assert a.fingerprint == b.fingerprint

    def test_different_gate_hashes_apart(self) -> None:
        a = GateFailure(gate="stop:user-directed-question", hook_event="Stop", session_id="s1")
        b = GateFailure(gate="stop:comment-density", hook_event="Stop", session_id="s1")
        assert a.fingerprint != b.fingerprint


class TestClassify:
    @pytest.mark.parametrize(
        ("gate", "expected"),
        [
            ("stop:user-directed-question", GateVerdict.PREVENTABLE),
            ("stop:comment-density", GateVerdict.PREVENTABLE),
            ("stop:banned-terms", GateVerdict.PREVENTABLE),
            ("posttooluse:plugin-directory-does-not-exist", GateVerdict.ENVIRONMENTAL),
            ("posttooluse:failed-with-non-blocking-status-code", GateVerdict.ENVIRONMENTAL),
            ("posttooluse:hook-json-output-validation-failed", GateVerdict.ENVIRONMENTAL),
        ],
    )
    def test_table_classification(self, gate: str, expected: GateVerdict) -> None:
        failure = GateFailure(gate=gate, hook_event="Stop", session_id="s1")
        assert classify_gate_failure(failure) is expected

    def test_unknown_gate_is_preventable(self) -> None:
        failure = GateFailure(gate="stop:never-seen-gate", hook_event="Stop", session_id="s1")
        assert classify_gate_failure(failure) is GateVerdict.PREVENTABLE


class TestRecord:
    def test_records_and_recurring_across_sessions(self, tmp_path: Path) -> None:
        store = FindingsStore(data_dir=tmp_path)
        a = GateFailure(gate="stop:user-directed-question", hook_event="Stop", session_id="s1")
        b = GateFailure(gate="stop:user-directed-question", hook_event="Stop", session_id="s2")
        record_gate_failures(store, [a])
        record_gate_failures(store, [b])
        assert a.fingerprint in store.recurring_fingerprints(min_occurrences=2)


class _FakeHost:
    def __init__(self, *, existing: list[dict[str, object]] | None = None) -> None:
        self.created: list[dict[str, object]] = []
        self._existing = existing or []

    def search_open_issues(self, *, repo: str, query: str) -> list[dict[str, object]]:
        return self._existing

    def create_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> dict[str, object]:
        number = len(self.created) + 100
        self.created.append({"repo": repo, "title": title, "body": body, "labels": labels})
        return {"html_url": f"https://github.com/{repo}/issues/{number}", "number": number}


_CONTEXT = FilingContext(repo="o/r", pr_url="https://github.com/o/r/pull/1")


def _preventable(session_id: str = "s1") -> GateFailure:
    return GateFailure(gate="stop:user-directed-question", hook_event="Stop", session_id=session_id)


def _record_recurring(store: FindingsStore, failure: GateFailure) -> None:
    record_gate_failures(store, [failure])
    record_gate_failures(store, [GateFailure(gate=failure.gate, hook_event=failure.hook_event, session_id="s9")])


class TestEscalate:
    def test_recurring_preventable_files_one_issue(self, tmp_path: Path) -> None:
        store = FindingsStore(data_dir=tmp_path)
        failure = _preventable()
        _record_recurring(store, failure)
        host = _FakeHost()
        filed = escalate_gate_failures(host, failures=[failure], store=store, context=_CONTEXT)
        assert len(host.created) == 1
        assert len(filed) == 1
        assert host.created[0]["labels"] == ["enforcement-gap", "needs-triage"]

    def test_rerun_does_not_refile(self, tmp_path: Path) -> None:
        store = FindingsStore(data_dir=tmp_path)
        failure = _preventable()
        _record_recurring(store, failure)
        first = _FakeHost()
        escalate_gate_failures(first, failures=[failure], store=store, context=_CONTEXT)
        body = first.created[0]["body"]
        second = _FakeHost(existing=[{"html_url": "https://github.com/o/r/issues/100", "body": body}])
        filed = escalate_gate_failures(second, failures=[failure], store=store, context=_CONTEXT)
        assert second.created == []
        assert filed[0].already_filed

    def test_environmental_files_nothing(self, tmp_path: Path) -> None:
        store = FindingsStore(data_dir=tmp_path)
        failure = GateFailure(
            gate="posttooluse:plugin-directory-does-not-exist", hook_event="PostToolUse", session_id="s1"
        )
        _record_recurring(store, failure)
        host = _FakeHost()
        filed = escalate_gate_failures(host, failures=[failure], store=store, context=_CONTEXT)
        assert host.created == []
        assert filed == []

    def test_non_recurring_files_nothing(self, tmp_path: Path) -> None:
        store = FindingsStore(data_dir=tmp_path)
        failure = _preventable()
        record_gate_failures(store, [failure])
        host = _FakeHost()
        filed = escalate_gate_failures(host, failures=[failure], store=store, context=_CONTEXT)
        assert host.created == []
        assert filed == []

    @pytest.mark.usefixtures("banned_config")
    def test_banned_term_body_is_withheld(self, tmp_path: Path) -> None:
        store = FindingsStore(data_dir=tmp_path)
        failure = GateFailure(gate="stop:acmecorp-tenant-flow", hook_event="Stop", session_id="s1")
        _record_recurring(store, failure)
        host = _FakeHost()
        filed = escalate_gate_failures(host, failures=[failure], store=store, context=_CONTEXT)
        assert host.created == []
        assert filed[0].withheld
        assert "acmecorp" in filed[0].withheld_reason


@pytest.fixture
def banned_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text('[teatree]\nbanned_terms = ["acmecorp"]\n', encoding="utf-8")
    monkeypatch.setenv("T3_BANNED_TERMS_CONFIG", str(cfg))
    return cfg
