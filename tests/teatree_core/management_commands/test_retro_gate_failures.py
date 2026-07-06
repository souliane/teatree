"""``t3 <overlay> retro gate-failures`` — extract, classify, record, escalate (#2024).

Session lines are built in the REAL on-disk schema: a gate BLOCK is a
``hook_blocking_error`` whose ``blockingError.blockingError`` carries a
``TEATREE GATE`` marker (no ``exitCode``); an infra failure is a
``hook_non_blocking_error`` with ``exitCode:1`` and a ``stderr``. The list pass
extracts, classifies, records, and emits JSON + a human summary; ``--escalate``
files one deduped enforcement issue per recurring preventable failure.
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
from teatree.core.review import review_findings as rf_mod
from tests.teatree_core.conftest import CommandOverlay

_PR_URL = "https://github.com/souliane/teatree/pull/2024"
_REPO = "souliane/teatree"
_MOCK_OVERLAY = {"test": CommandOverlay()}

_RUNNER = "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/hook_router.py --event Stop"


def _gate_block_line(*, message: str) -> str:
    return json.dumps(
        {
            "type": "attachment",
            "attachment": {
                "type": "hook_blocking_error",
                "hookEvent": "Stop",
                "hookName": "Stop",
                "toolUseID": "t1",
                "blockingError": {"blockingError": message, "command": _RUNNER},
            },
        }
    )


def _infra_error_line(*, stderr: str) -> str:
    return json.dumps(
        {
            "type": "attachment",
            "attachment": {
                "type": "hook_non_blocking_error",
                "hookEvent": "PostToolUse",
                "hookName": "PostToolUse:Bash",
                "exitCode": 1,
                "toolUseID": "t2",
                "stderr": stderr,
                "stdout": "",
                "command": 'node "${CLAUDE_PLUGIN_ROOT}/hooks/posttooluse.mjs"',
            },
        }
    )


def _passing_line() -> str:
    return json.dumps(
        {
            "type": "attachment",
            "attachment": {
                "type": "hook_success",
                "hookEvent": "PreToolUse",
                "hookName": "PreToolUse:Bash",
                "exitCode": 0,
                "toolUseID": "t3",
                "command": _RUNNER,
            },
        }
    )


_QUESTION_GATE = (
    "TEATREE GATE — a user-directed question was asked inline in prose with no "
    "AskUserQuestion tool call in this turn. Re-ask through the structured tool."
)
_PLUGIN_INFRA = "Failed to run: Plugin directory does not exist: /Users/x/.claude/plugins/cache"


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
            _gate_block_line(message=_QUESTION_GATE),
            _infra_error_line(stderr=_PLUGIN_INFRA),
            _passing_line(),
        )
        result = self._run(file=str(session), store_dir=store_dir)
        failures = cast("list[dict[str, object]]", result["failures"])
        verdicts = {str(f["gate"]): f["verdict"] for f in failures}
        preventable = [g for g, v in verdicts.items() if v == "preventable"]
        environmental = [g for g, v in verdicts.items() if v == "environmental"]
        assert any("user-directed-question" in g for g in preventable)
        assert any("plugin-directory-does-not-exist" in g for g in environmental)
        assert not any("pretooluse" in g for g in verdicts)

    def test_serialized_output_never_carries_message_stderr_or_command(self) -> None:
        store_dir = Path(self._tmp())
        session = _session_file(
            store_dir,
            _gate_block_line(message=_QUESTION_GATE + " acmecorp-secret"),
            _infra_error_line(stderr="leaked-token-xyz " + _PLUGIN_INFRA),
        )
        result = self._run(file=str(session), store_dir=store_dir)
        payload = json.dumps(result)
        assert "acmecorp-secret" not in payload
        assert "leaked-token-xyz" not in payload
        assert "AskUserQuestion tool call in this turn" not in payload
        assert "hook_router.py" not in payload

    def test_no_transcript_reports_skip(self) -> None:
        store_dir = Path(self._tmp())
        result = self._run(file=str(store_dir / "missing.jsonl"), store_dir=store_dir)
        assert result["skipped"] is True

    def test_escalate_files_recurring_preventable(self) -> None:
        store_dir = Path(self._tmp())
        session_a = _session_file(store_dir, _gate_block_line(message=_QUESTION_GATE), session_id="sess-a")
        self._run(file=str(session_a), store_dir=store_dir)
        tmp_b = Path(self._tmp())
        session_b = _session_file(tmp_b, _gate_block_line(message=_QUESTION_GATE), session_id="sess-b")
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
        session_a = _session_file(store_dir, _infra_error_line(stderr=_PLUGIN_INFRA), session_id="env-a")
        self._run(file=str(session_a), store_dir=store_dir)
        tmp_b = Path(self._tmp())
        session_b = _session_file(tmp_b, _infra_error_line(stderr=_PLUGIN_INFRA), session_id="env-b")
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
