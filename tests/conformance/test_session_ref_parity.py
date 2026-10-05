"""The hook and Django tiers must join the same agent session without its raw id."""

from hooks.scripts import gate_ledger
from teatree.core.telemetry import observation_read
from teatree.utils import hook_registry, session_ref


def test_hook_and_django_session_refs_match() -> None:
    session_id = "session-123"
    assert gate_ledger.session_ref(session_id) == session_ref.session_ref(session_id)
    assert gate_ledger.session_ref("") == session_ref.session_ref("") == ""


def test_hook_and_django_gate_directories_match(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
    assert gate_ledger._directory() == hook_registry.loop_registry_dir() / "otel"


def test_the_hook_keeps_gate_files_exactly_as_long_as_the_reader_can_ask_for_them() -> None:
    assert gate_ledger.RETENTION_DAYS == observation_read.RETENTION_DAYS
