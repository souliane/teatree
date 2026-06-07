"""``t3 <overlay> retro gate-failures`` — extract, classify, record, escalate (#2024).

The session transcript is supplied via ``--file`` (a local on-disk session
JSONL). The list pass extracts the non-zero hook exits, classifies each
preventable / environmental, records to the durable store, and emits JSON +
a human summary. The ``--escalate`` pass files one deduped enforcement issue
per recurring preventable failure via the resolved code host.
"""

import json
from io import StringIO
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase

from teatree.backends import loader as loader_mod
from teatree.core import overlay_loader as overlay_loader_mod
from teatree.core import review_findings as rf_mod
from tests.teatree_core.conftest import CommandOverlay

_PR_URL = "https://github.com/souliane/teatree/pull/2024"
_REPO = "souliane/teatree"
_MOCK_OVERLAY = {"test": CommandOverlay()}


def _hook_line(*, gate: str, exit_code: int, command: str) -> str:
    return json.dumps(
        {
            "type": "attachment",
            "attachment": {
                "type": "hook_blocking_error" if exit_code else "hook_success",
                "hookEvent": "PreToolUse",
                "hookName": gate,
                "exitCode": exit_code,
                "command": command,
                "stdout": "diff body with acmecorp secret",
                "stderr": "leaked-token-xyz",
                "toolUseID": "t1",
            },
        }
    )


def _session_file(tmp: Path, *lines: str, session_id: str = "session") -> Path:
    header = json.dumps({"type": "user", "message": {"role": "user", "content": "work"}})
    path = tmp / f"{session_id}.jsonl"
    path.write_text("\n".join([header, *lines]), encoding="utf-8")
    return path


class RetroGateFailuresTest(TestCase):
    def _run(self, *args: str, store_dir: Path, **kwargs: object) -> dict[str, object]:
        with patch.object(rf_mod, "get_data_dir", return_value=store_dir):
            output = call_command("retro", "gate-failures", *args, stdout=StringIO(), **kwargs)
        return cast("dict[str, object]", json.loads(output))

    def test_lists_classified_failures(self) -> None:
        store_dir = Path(self._tmp())
        session = _session_file(
            store_dir,
            _hook_line(gate="check-comment-density", exit_code=1, command="Write src/x.py"),
            _hook_line(gate="uv-audit", exit_code=1, command="uv pip audit"),
            _hook_line(gate="router", exit_code=0, command="t3"),
        )
        result = self._run(file=str(session), store_dir=store_dir)
        failures = cast("list[dict[str, object]]", result["failures"])
        assert len(failures) == 2
        verdicts = {f["gate"]: f["verdict"] for f in failures}
        assert verdicts == {"check-comment-density": "preventable", "uv-audit": "environmental"}

    def test_serialized_output_never_carries_stdout_or_stderr(self) -> None:
        store_dir = Path(self._tmp())
        session = _session_file(
            store_dir,
            _hook_line(gate="check-comment-density", exit_code=1, command="Write src/x.py"),
        )
        result = self._run(file=str(session), store_dir=store_dir)
        payload = json.dumps(result)
        assert "acmecorp" not in payload
        assert "leaked-token-xyz" not in payload

    def test_no_transcript_reports_skip(self) -> None:
        store_dir = Path(self._tmp())
        result = self._run(file=str(store_dir / "missing.jsonl"), store_dir=store_dir)
        assert result["skipped"] is True

    def test_escalate_files_recurring_preventable(self) -> None:
        store_dir = Path(self._tmp())
        # First run records the failure once (session A).
        session_a = _session_file(
            store_dir,
            _hook_line(gate="check-comment-density", exit_code=1, command="Write src/a.py"),
            session_id="sess-a",
        )
        self._run(file=str(session_a), store_dir=store_dir)
        # Second run (session B, different file) makes it recurring, then escalates.
        tmp_b = Path(self._tmp())
        session_b = _session_file(
            tmp_b,
            _hook_line(gate="check-comment-density", exit_code=1, command="Write src/b.py"),
            session_id="sess-b",
        )
        host = MagicMock()
        host.search_open_issues.return_value = []
        host.create_issue.return_value = {"html_url": "https://github.com/souliane/teatree/issues/3000"}
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
        ):
            result = self._run(
                "--escalate",
                file=str(session_b),
                repo=_REPO,
                pr_url=_PR_URL,
                store_dir=store_dir,
            )
        host.create_issue.assert_called_once()
        kwargs = host.create_issue.call_args.kwargs
        assert kwargs["labels"] == ["enforcement-gap", "needs-triage"]
        filed = cast("list[dict[str, object]]", result["filed"])
        assert len(filed) == 1
        assert filed[0]["url"] == "https://github.com/souliane/teatree/issues/3000"

    def test_escalate_environmental_files_nothing(self) -> None:
        store_dir = Path(self._tmp())
        session_a = _session_file(
            store_dir, _hook_line(gate="uv-audit", exit_code=1, command="uv pip audit"), session_id="env-a"
        )
        self._run(file=str(session_a), store_dir=store_dir)
        tmp_b = Path(self._tmp())
        session_b = _session_file(
            tmp_b, _hook_line(gate="uv-audit", exit_code=1, command="uv pip audit run 2"), session_id="env-b"
        )
        host = MagicMock()
        host.search_open_issues.return_value = []
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
        ):
            result = self._run("--escalate", file=str(session_b), repo=_REPO, pr_url=_PR_URL, store_dir=store_dir)
        host.create_issue.assert_not_called()
        assert cast("list[dict[str, object]]", result["filed"]) == []

    @staticmethod
    def _tmp() -> str:
        import tempfile  # noqa: PLC0415

        return tempfile.mkdtemp()
