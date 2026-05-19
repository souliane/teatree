"""Tests for ``teatree.core.session_identity`` (#1107 Prong A).

The headline #1107 root cause: Claude Code delivers the session id only in
the hook JSON payload, NOT as an env var inside Bash-tool subprocesses, so
``current_session_id()`` returned ``""`` in agent-driven mode → ``t3 loop
claim`` hard-refused → loop-owner could never be claimed → every
owner-gated slot was permanently dead (the 131-DM incident).

The fix adds a third, lowest-precedence fallback: read the loop-registry
file's owner record. These tests pin the precedence and the fail-open
branches.
"""

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.core.models import LoopLease
from teatree.core.session_identity import current_session_id

pytestmark = pytest.mark.django_db


def _no_session_env() -> dict[str, str]:
    import os  # noqa: PLC0415

    return {k: v for k, v in os.environ.items() if k not in {"CLAUDE_SESSION_ID", "T3_LOOP_SESSION_ID"}}


class TestSessionIdRegistryFallback:
    def test_current_session_id_reads_loop_registry_when_env_absent(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "sess-abc"}}), encoding="utf-8"
        )
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            assert current_session_id() == "sess-abc"

    def test_env_var_takes_precedence_over_registry(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "sess-abc"}}), encoding="utf-8"
        )
        with patch.dict(
            "os.environ",
            {**_no_session_env(), "CLAUDE_SESSION_ID": "foo", "T3_LOOP_REGISTRY_DIR": str(tmp_path)},
            clear=True,
        ):
            assert current_session_id() == "foo"

    def test_missing_registry_file_is_empty(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            assert current_session_id() == ""

    def test_corrupt_registry_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text("{not valid json", encoding="utf-8")
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            assert current_session_id() == ""

    def test_non_dict_owner_record_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text(json.dumps({"t3-loop-tick-owner": "not-a-dict"}), encoding="utf-8")
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            assert current_session_id() == ""

    def test_xdg_data_home_resolution_when_no_registry_dir_env(self, tmp_path: Path) -> None:
        teatree_dir = tmp_path / "teatree"
        teatree_dir.mkdir()
        (teatree_dir / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "xdg-sess"}}), encoding="utf-8"
        )
        env = {k: v for k, v in _no_session_env().items() if k != "T3_LOOP_REGISTRY_DIR"}
        with patch.dict("os.environ", {**env, "XDG_DATA_HOME": str(tmp_path)}, clear=True):
            assert current_session_id() == "xdg-sess"


class TestLoopClaimSucceedsViaRegistrySessionId:
    """The literal #1107 incident reproduction (Prong A2)."""

    def test_loop_claim_succeeds_via_registry_session_id(self, tmp_path: Path) -> None:
        (tmp_path / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": "sess-abc"}}), encoding="utf-8"
        )
        out = io.StringIO()
        with patch.dict("os.environ", {**_no_session_env(), "T3_LOOP_REGISTRY_DIR": str(tmp_path)}, clear=True):
            call_command("loop_owner", "claim", "--take-over", stdout=out)

        assert "OK    claimed" in out.getvalue()
        status = LoopLease.objects.ownership_status("loop-owner")
        assert status.is_live is True
        assert status.owner_session == "sess-abc"
