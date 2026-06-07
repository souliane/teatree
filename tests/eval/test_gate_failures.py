"""Tests for the gate-failure feedback loop (#2024).

The extractor reads the single transcript chokepoint
(``extract_hook_events``), keeps only non-zero hook exits (a gate failure),
and never serializes the privacy-sensitive ``stdout``/``stderr``. The
classifier is one declarative table (environmental vs preventable). The
escalation filer reuses the review-findings file/dedup stack to file one
deduped enforcement issue per recurring preventable failure.
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
    record_gate_failures,
)
from teatree.eval.session_transcript import parse_session_jsonl
from teatree.hooks import banned_terms_scanner


def _session(*lines: str) -> str:
    header = '{"type":"user","message":{"role":"user","content":"work"}}'
    return "\n".join([header, *lines])


def _hook(
    *,
    gate: str = "check-comment-density",
    exit_code: int = 1,
    command: str = "Write src/teatree/util/money.py",
    sensitive: str = "diff with banned content acmecorp / secret token leaked here",
) -> str:
    return json.dumps(
        {
            "type": "attachment",
            "attachment": {
                "type": "hook_blocking_error" if exit_code else "hook_success",
                "hookEvent": "PreToolUse",
                "hookName": gate,
                "exitCode": exit_code,
                "command": command,
                "stdout": sensitive,
                "stderr": sensitive,
                "toolUseID": "t1",
            },
        }
    )


class TestExtractGateFailures:
    def test_one_failure_from_a_nonzero_exit(self) -> None:
        events = parse_session_jsonl(_session(_hook(exit_code=1), _hook(gate="router", exit_code=0)))
        failures = extract_gate_failures(events, session_id="s1")
        assert len(failures) == 1
        assert failures[0].gate == "check-comment-density"

    def test_zero_exit_yields_no_failure(self) -> None:
        events = parse_session_jsonl(_session(_hook(exit_code=0)))
        assert extract_gate_failures(events, session_id="s1") == []

    def test_flipping_the_only_failure_to_zero_drives_count_to_zero(self) -> None:
        passing = parse_session_jsonl(_session(_hook(exit_code=0)))
        failing = parse_session_jsonl(_session(_hook(exit_code=1)))
        assert len(extract_gate_failures(failing, session_id="s1")) == 1
        assert extract_gate_failures(passing, session_id="s1") == []

    def test_none_exit_is_not_a_failure(self) -> None:
        line = (
            '{"type":"attachment","attachment":{"type":"hook_success",'
            '"hookEvent":"PreToolUse","hookName":"router","command":"t3","toolUseID":"t1"}}'
        )
        events = parse_session_jsonl(_session(line))
        assert extract_gate_failures(events, session_id="s1") == []

    def test_serialization_never_carries_stdout_or_stderr(self) -> None:
        events = parse_session_jsonl(_session(_hook(sensitive="acmecorp diff body leaked-secret-xyz")))
        failure = extract_gate_failures(events, session_id="s1")[0]
        payload = json.dumps(failure.as_dict())
        assert "acmecorp" not in payload
        assert "leaked-secret-xyz" not in payload
        assert "stdout" not in payload
        assert "stderr" not in payload


class TestFingerprint:
    def test_same_gate_different_file_hashes_together(self) -> None:
        a = GateFailure(gate="check-comment-density", hook_event="PreToolUse", command="Write a/x.py", session_id="s1")
        b = GateFailure(gate="check-comment-density", hook_event="PreToolUse", command="Write b/y.py", session_id="s2")
        assert a.fingerprint == b.fingerprint

    def test_different_gate_hashes_apart(self) -> None:
        a = GateFailure(gate="check-comment-density", hook_event="PreToolUse", command="Write a/x.py", session_id="s1")
        b = GateFailure(gate="check-banned-terms", hook_event="PreToolUse", command="Write a/x.py", session_id="s1")
        assert a.fingerprint != b.fingerprint


class TestClassify:
    @pytest.mark.parametrize(
        ("gate", "expected"),
        [
            ("check-comment-density", GateVerdict.PREVENTABLE),
            ("check-banned-terms", GateVerdict.PREVENTABLE),
            ("doc-update-gate", GateVerdict.PREVENTABLE),
            ("uv-audit", GateVerdict.ENVIRONMENTAL),
            ("uv-lock", GateVerdict.ENVIRONMENTAL),
            ("uv-sync", GateVerdict.ENVIRONMENTAL),
            ("gitleaks", GateVerdict.ENVIRONMENTAL),
            ("ty", GateVerdict.ENVIRONMENTAL),
            ("tach", GateVerdict.ENVIRONMENTAL),
            ("import-linter", GateVerdict.ENVIRONMENTAL),
        ],
    )
    def test_table_classification(self, gate: str, expected: GateVerdict) -> None:
        failure = GateFailure(gate=gate, hook_event="PreToolUse", command="x", session_id="s1")
        assert classify_gate_failure(failure) is expected

    def test_unknown_gate_is_preventable_and_flagged(self) -> None:
        failure = GateFailure(gate="never-seen-gate", hook_event="PreToolUse", command="x", session_id="s1")
        assert classify_gate_failure(failure) is GateVerdict.PREVENTABLE


class TestRecord:
    def test_records_and_recurring_across_sessions(self, tmp_path: Path) -> None:
        store = FindingsStore(data_dir=tmp_path)
        failure = GateFailure(
            gate="check-comment-density", hook_event="PreToolUse", command="Write a/x.py", session_id="s1"
        )
        record_gate_failures(store, [failure])
        again = GateFailure(
            gate="check-comment-density", hook_event="PreToolUse", command="Write b/y.py", session_id="s2"
        )
        record_gate_failures(store, [again])
        assert failure.fingerprint in store.recurring_fingerprints(min_occurrences=2)


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


def _preventable(command: str = "Write a/x.py", session_id: str = "s1") -> GateFailure:
    return GateFailure(gate="check-comment-density", hook_event="PreToolUse", command=command, session_id=session_id)


def _record_recurring(store: FindingsStore, failure: GateFailure) -> None:
    record_gate_failures(store, [failure])
    record_gate_failures(
        store, [GateFailure(gate=failure.gate, hook_event=failure.hook_event, command="Write c/z.py", session_id="s9")]
    )


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
        failure = GateFailure(gate="uv-audit", hook_event="PreToolUse", command="uv pip audit", session_id="s1")
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
        failure = GateFailure(
            gate="acmecorp-gate", hook_event="PreToolUse", command="Write acmecorp/x.py", session_id="s1"
        )
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


def test_banned_terms_scanner_importable() -> None:
    assert banned_terms_scanner.scan_text("plain prose") is None
