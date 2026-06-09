"""The conversation-audit ledger: record, persist, aggregate, and the no-payload guard."""

from django.test import TestCase

from teatree.core.models import EvalRunRecord, EvalVerdict, InvariantOutcome, SessionAuditRecord
from teatree.eval.audit_persistence import persist_audit


def _record(**overrides: object) -> SessionAuditRecord:
    fields: dict[str, object] = {
        "session_id": "synthetic-bg-001",
        "corpus_entry_id": "background_ci_watch",
        "outcome_axis": "backgrounding",
        "expected_outcome": "backgrounded",
        "predicted_outcome": "backgrounded",
        "verdict": EvalVerdict.PASS,
        "oracle": "matcher",
    }
    fields.update(overrides)
    return SessionAuditRecord.record(**fields)


class TestRecord(TestCase):
    def test_record_writes_a_row(self) -> None:
        record = _record()
        assert SessionAuditRecord.objects.get(pk=record.pk).outcome_axis == "backgrounding"

    def test_record_links_eval_run(self) -> None:
        run = EvalRunRecord.objects.record(model="haiku")
        record = _record(eval_run=run)
        assert SessionAuditRecord.objects.get(pk=record.pk).eval_run_id == run.pk

    def test_eval_run_fk_survives_run_deletion(self) -> None:
        run = EvalRunRecord.objects.record(model="haiku")
        record = _record(eval_run=run)
        run.delete()
        record.refresh_from_db()
        assert record.eval_run_id is None

    def test_str_is_human_readable(self) -> None:
        record = _record()
        assert "background_ci_watch" in str(record)

    def test_record_stores_invariant_results_and_gate_slugs(self) -> None:
        outcome: InvariantOutcome = {"invariant_id": "no_main_clone_edit", "ok": False, "offending_index": 3}
        record = _record(invariant_results=[outcome], gate_failure_slugs=["no-edit-in-main-clone"])
        stored = SessionAuditRecord.objects.get(pk=record.pk)
        assert stored.invariant_results == [outcome]
        assert stored.gate_failure_slugs == ["no-edit-in-main-clone"]


class TestPersistAudit(TestCase):
    def test_persist_writes_and_stamps_git_sha(self) -> None:
        record = SessionAuditRecord(
            session_id="s1",
            corpus_entry_id="background_ci_watch",
            outcome_axis="backgrounding",
            expected_outcome="backgrounded",
            predicted_outcome="blocking",
            verdict=EvalVerdict.FAIL,
            oracle="matcher",
        )
        persisted = persist_audit([record], git_sha="deadbeef")
        assert persisted[0].pk is not None
        assert SessionAuditRecord.objects.get(pk=record.pk).git_sha == "deadbeef"

    def test_persist_preserves_explicit_git_sha(self) -> None:
        record = SessionAuditRecord(
            session_id="s1",
            corpus_entry_id="e",
            outcome_axis="a",
            expected_outcome="x",
            predicted_outcome="x",
            verdict=EvalVerdict.PASS,
            oracle="matcher",
            git_sha="explicit-sha",
        )
        persist_audit([record], git_sha="batch-sha")
        assert SessionAuditRecord.objects.get(pk=record.pk).git_sha == "explicit-sha"


class TestAggregates(TestCase):
    def test_nominated_filters(self) -> None:
        _record(nominated_for_label=True, session_id="a")
        _record(nominated_for_label=False, session_id="b")
        nominated = SessionAuditRecord.objects.nominated()
        assert [r.session_id for r in nominated] == ["a"]

    def test_for_session_filters(self) -> None:
        _record(session_id="wanted")
        _record(session_id="other")
        assert list(SessionAuditRecord.objects.for_session("wanted").values_list("session_id", flat=True)) == ["wanted"]

    def test_confusion_pairs_returns_expected_predicted(self) -> None:
        _record(outcome_axis="backgrounding", expected_outcome="backgrounded", predicted_outcome="backgrounded")
        _record(outcome_axis="backgrounding", expected_outcome="backgrounded", predicted_outcome="blocking")
        _record(outcome_axis="other_axis", expected_outcome="x", predicted_outcome="y")
        pairs = SessionAuditRecord.objects.confusion_pairs("backgrounding")
        assert pairs == [("backgrounded", "backgrounded"), ("backgrounded", "blocking")]


class TestNoPayloadStored(TestCase):
    def test_record_has_no_tool_input_or_prompt_field(self) -> None:
        field_names = {field.name for field in SessionAuditRecord._meta.get_fields()}
        forbidden = {"tool_input", "tool_calls", "prompt", "hook_payload", "hook_stdout", "raw", "text_blocks"}
        leaked = field_names & forbidden
        assert not leaked, f"audit record must store no payload, found {leaked}"

    def test_only_categorical_and_id_fields(self) -> None:
        record = _record()
        stored = {
            field.name: getattr(record, field.name) for field in record._meta.concrete_fields if field.name != "id"
        }
        assert set(stored) == {
            "audited_at",
            "session_id",
            "corpus_entry_id",
            "outcome_axis",
            "expected_outcome",
            "predicted_outcome",
            "verdict",
            "oracle",
            "judge_rationale",
            "invariant_results",
            "gate_failure_slugs",
            "nominated_for_label",
            "eval_run",
            "git_sha",
        }
