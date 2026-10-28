# test-path: cross-cutting — drives hooks/scripts/hook_router.py, the Notion boundary handler and the publish scanners.
"""A Notion connector call goes through the same PreToolUse publish gates as any other publish (#5023).

A write is scanned for a verbatim user quote and a secret, and is held to the page boundary: the page must
be a live one that this session read first. A successful direct fetch is what stamps a page as read.
"""

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.notion_mcp_boundary as boundary
from hooks.scripts.gate_escape import _audit_request
from hooks.scripts.hook_router import handle_banned_terms_pretool, handle_quote_scanner_pretool
from hooks.scripts.quote_verdict import QuoteVerdict
from teatree.backends.notion import credentials
from teatree.backends.notion.errors import NotionTokenMissingError, normalize_object_id
from teatree.backends.notion.liveness import Liveness, LivenessVerdict

HOOK_ROUTER = Path(router.__file__)
SESSION = "sess-notion"
PAGE = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
OTHER_PAGE = "99999999-8888-4777-8666-555555555555"
FETCH = "mcp__connector_Notion__notion-fetch"
UPDATE = "mcp__connector_Notion__notion-update-page"
CREATE_PAGES = "mcp__connector_Notion__notion-create-pages"
CREATE_COMMENT = "mcp__notion__notion-create-comment"
SEARCH = "mcp__connector_Notion__notion-search"
HIGH_QUOTE = "## User ask (verbatim, 2026-05-20)\nplease ship"
AWS_KEY = "AKIA" + "A1B2C3D4E5F6G7H8"

LIVE = LivenessVerdict(state=Liveness.LIVE, reason="not_a_database_row", detail="live")
ARCHIVED = LivenessVerdict(
    state=Liveness.DEAD, reason="archived_flag", detail="Notion reports it as archived or in the trash"
)
UNKNOWN = LivenessVerdict(state=Liveness.UNKNOWN, reason="absent_from_parent_database", detail="not in its database")


class FakeNotion:
    def __init__(self) -> None:
        self.verdict = LIVE
        self.probed: list[str] = []
        self.failure: Exception | None = None

    def page_liveness(self, page_id: str) -> LivenessVerdict:
        self.probed.append(page_id)
        if self.failure is not None:
            raise self.failure
        return self.verdict


@pytest.fixture
def notion(monkeypatch: pytest.MonkeyPatch) -> FakeNotion:
    fake = FakeNotion()
    monkeypatch.setattr(credentials, "build_notion_client", lambda *_args, **_kwargs: fake)
    return fake


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(router, "STATE_DIR", tmp_path)
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path))
    return tmp_path


def _page_text(page_id: str) -> str:
    return f'Here is the result of "view" for the Page with URL https://www.notion.so/{page_id.replace("-", "")}:'


def _fetched(page_id: str = PAGE, *, session: str = SESSION, requested: str | None = None, **extra: object) -> dict:
    return {
        "session_id": session,
        "tool_name": FETCH,
        "tool_input": {"id": requested or page_id},
        "tool_response": [{"type": "text", "text": _page_text(page_id)}],
        **extra,
    }


def _write(page_id: str | None = PAGE, *, session: str = SESSION, tool_name: str = UPDATE, **fields: object) -> dict:
    tool_input: dict[str, object] = {"command": "update_content", "new_str": "an edit", **fields}
    if page_id is not None:
        tool_input["page_id"] = page_id
    return {"session_id": session, "tool_name": tool_name, "tool_input": tool_input}


def _deny_reason(capsys: pytest.CaptureFixture[str]) -> str:
    return json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecisionReason"]


class TestPublishScanners:
    def test_a_quote_being_removed_does_not_block_a_redaction(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = _write(content_updates=[{"old_str": HIGH_QUOTE, "new_str": "clean"}])
        assert handle_quote_scanner_pretool(data) is False
        assert capsys.readouterr().out == ""

    def test_a_secret_under_a_property_named_id_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = _write(properties={"id": AWS_KEY})
        assert handle_banned_terms_pretool(data) is True
        assert "secret" in _deny_reason(capsys)

    def test_a_secret_after_a_uuid_in_an_id_field_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = _write(properties={"id": f"{PAGE} {AWS_KEY}"})
        assert handle_banned_terms_pretool(data) is True
        assert "secret" in _deny_reason(capsys)

    def test_high_quote_in_a_write_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = _write(content_updates=[{"old_str": "x", "new_str": HIGH_QUOTE}])
        assert handle_quote_scanner_pretool(data) is True
        assert "heading-user-ask-verbatim" in _deny_reason(capsys)

    def test_the_same_text_through_a_read_is_not_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = {"session_id": SESSION, "tool_name": SEARCH, "tool_input": {"query": HIGH_QUOTE}}
        assert handle_quote_scanner_pretool(data) is False
        assert capsys.readouterr().out == ""

    def test_a_connector_command_field_never_reaches_the_bash_visibility_verdict(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen: list[str] = []

        def _spy(command: str, _cwd: Path | None) -> QuoteVerdict:
            seen.append(command)
            return QuoteVerdict(deny=True, warning=None, decision="deny")

        monkeypatch.setattr(router, "_resolve_quote_verdict", _spy)
        data = _write(content_updates=[{"old_str": "x", "new_str": HIGH_QUOTE}])
        data["tool_input"]["command"] = "gh pr comment 1 -R owner/private-repo --body x"
        assert handle_quote_scanner_pretool(data) is True
        assert seen == [""]
        bash = {"tool_name": "Bash", "tool_input": {"command": f'gh pr create --title t --body "{HIGH_QUOTE}"'}}
        assert handle_quote_scanner_pretool(bash) is True
        assert seen[1].startswith("gh pr create")
        capsys.readouterr()

    def test_a_secret_in_a_write_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = _write(new_str=f"the key is {AWS_KEY}")
        assert handle_banned_terms_pretool(data) is True
        assert "secret" in _deny_reason(capsys)

    def test_a_secret_in_a_read_is_not_this_gates_business(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = {"session_id": SESSION, "tool_name": SEARCH, "tool_input": {"query": AWS_KEY}}
        assert handle_banned_terms_pretool(data) is False
        assert capsys.readouterr().out == ""


class TestRealRouterRoute:
    def _run(self, event: str, payload: dict, tmp_path: Path) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "HOME": str(tmp_path), "T3_DATA_DIR": str(tmp_path), "T3_HOOK_STATE_DIR": str(tmp_path)}
        env.pop("T3_CONFIG_DB", None)
        return subprocess.run(
            [sys.executable, str(HOOK_ROUTER), "--event", event],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )

    def test_pretooluse_refuses_a_high_quote_notion_write_with_exit_2(self, tmp_path: Path) -> None:
        data = _write(content_updates=[{"old_str": "x", "new_str": HIGH_QUOTE}])
        result = self._run("PreToolUse", data, tmp_path)
        assert result.returncode == 2, result.stderr
        assert "heading-user-ask-verbatim" in result.stdout

    def test_pretooluse_lets_a_notion_read_with_the_same_text_through(self, tmp_path: Path) -> None:
        data = {"session_id": SESSION, "tool_name": SEARCH, "tool_input": {"query": HIGH_QUOTE}}
        result = self._run("PreToolUse", data, tmp_path)
        assert result.returncode == 0, result.stderr
        assert "permissionDecision" not in result.stdout

    def test_posttooluse_stamps_a_fetch_that_a_later_pretooluse_write_then_passes_the_boundary(
        self, tmp_path: Path
    ) -> None:
        stamped = self._run("PostToolUse", _fetched(), tmp_path)
        assert stamped.returncode == 0, stamped.stderr
        assert PAGE in (tmp_path / f"{SESSION}.notion-fetches").read_text(encoding="utf-8")
        written = self._run("PreToolUse", _write(), tmp_path)
        assert written.returncode == 0, written.stdout
        unread = self._run("PreToolUse", _write(session="fresh-session"), tmp_path)
        assert unread.returncode == 2, unread.stderr
        assert "was not read in this session" in unread.stdout


class TestFetchStamps:
    @pytest.mark.parametrize(
        "reference",
        [
            PAGE,
            PAGE.replace("-", ""),
            f"https://www.notion.so/space/{PAGE.replace('-', '')}?v={OTHER_PAGE.replace('-', '')}",
            f"https://www.notion.so/?p={PAGE.replace('-', '')}",
        ],
    )
    def test_cold_normalizer_matches_the_notion_api_and_selects_the_page(self, reference: str) -> None:
        assert boundary._canonical(reference) == normalize_object_id(reference) == PAGE

    def _stamps(self, state: Path, session: str = SESSION) -> list[str]:
        path = state / f"{session}.notion-fetches"
        return path.read_text(encoding="utf-8").split() if path.is_file() else []

    def test_a_successful_direct_fetch_stamps_the_canonical_page_id(self, state_dir: Path) -> None:
        boundary.handle_track_notion_pages(_fetched())
        assert self._stamps(state_dir) == [PAGE]

    def test_a_structured_title_beginning_with_error_is_not_a_failed_fetch(self, state_dir: Path) -> None:
        data = _fetched()
        data["tool_response"] = {"title": "Error budget policy", "id": PAGE}
        boundary.handle_track_notion_pages(data)
        assert self._stamps(state_dir) == [PAGE]

    def test_a_top_level_text_error_blob_is_not_stamped(self, state_dir: Path) -> None:
        data = _fetched()
        data["tool_response"] = {"type": "text", "text": f"error: page {PAGE}"}
        boundary.handle_track_notion_pages(data)
        assert self._stamps(state_dir) == []

    def test_a_url_request_stamps_the_same_canonical_id(self, state_dir: Path) -> None:
        url = f"https://www.notion.so/Some-Title-{PAGE.replace('-', '')}"
        boundary.handle_track_notion_pages(_fetched(requested=url))
        assert self._stamps(state_dir) == [PAGE]

    def test_stamping_is_idempotent(self, state_dir: Path) -> None:
        boundary.handle_track_notion_pages(_fetched())
        boundary.handle_track_notion_pages(_fetched(requested=PAGE.upper()))
        assert self._stamps(state_dir) == [PAGE]

    def test_a_search_never_stamps(self, state_dir: Path) -> None:
        data = {
            "session_id": SESSION,
            "tool_name": SEARCH,
            "tool_input": {"query": "plan"},
            "tool_response": [{"type": "text", "text": _page_text(PAGE)}],
        }
        boundary.handle_track_notion_pages(data)
        assert self._stamps(state_dir) == []

    @pytest.mark.parametrize(
        "response",
        [
            [{"type": "text", "text": f"object_not_found: could not find page {PAGE}"}],
            {"is_error": True, "content": [{"type": "text", "text": _page_text(PAGE)}]},
            {"isError": True, "content": [{"type": "text", "text": _page_text(PAGE)}]},
            [{"type": "text", "text": _page_text(OTHER_PAGE)}],
            "",
            None,
        ],
        ids=["error-text", "is_error", "isError", "other-page-returned", "empty", "missing"],
    )
    def test_a_failed_or_mismatched_fetch_never_stamps(self, response: object, state_dir: Path) -> None:
        data = _fetched()
        data["tool_response"] = response
        boundary.handle_track_notion_pages(data)
        assert self._stamps(state_dir) == []

    def test_a_malformed_requested_id_never_stamps(self, state_dir: Path) -> None:
        boundary.handle_track_notion_pages(_fetched(requested="not-an-id"))
        assert self._stamps(state_dir) == []

    def test_stamps_are_per_session(self, state_dir: Path) -> None:
        boundary.handle_track_notion_pages(_fetched(session="other-session"))
        assert self._stamps(state_dir) == []
        assert self._stamps(state_dir, "other-session") == [PAGE]

    def test_a_fetch_without_a_session_id_stamps_nothing(self, state_dir: Path) -> None:
        boundary.handle_track_notion_pages(_fetched(session=""))
        assert list(state_dir.glob("*.notion-fetches")) == []

    def test_a_stamp_that_did_not_land_is_named(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(boundary, "append_line", lambda _path, _line: None)
        boundary.handle_track_notion_pages(_fetched())
        assert f"could not record that this session read Notion page {PAGE}" in capsys.readouterr().err

    def test_a_refetch_refreshes_the_stamp_so_the_stale_sweep_keeps_an_active_page(self, state_dir: Path) -> None:
        boundary.handle_track_notion_pages(_fetched())
        path = state_dir / f"{SESSION}.notion-fetches"
        os.utime(path, (1, 1))
        boundary.handle_track_notion_pages(_fetched())
        assert path.stat().st_mtime > 1


class TestWriteBoundary:
    def test_django_bootstrap_uses_the_probe_budget(
        self, notion: FakeNotion, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        boundary.handle_track_notion_pages(_fetched())
        monkeypatch.setattr(boundary, "remaining_timeout_s", lambda _preferred: 0.1)

        def _slow_bootstrap() -> bool:
            time.sleep(0.5)
            return True

        monkeypatch.setattr(boundary, "bootstrap_django_registry", _slow_bootstrap)
        started = time.monotonic()
        assert boundary.handle_block_notion_write_boundary(_write()) is False
        assert time.monotonic() - started < 0.4
        assert notion.probed == []
        assert "liveness" in capsys.readouterr().err

    def test_an_unread_page_is_denied_before_a_network_probe(self, notion: FakeNotion) -> None:
        assert boundary.handle_block_notion_write_boundary(_write()) is True
        assert notion.probed == []

    def test_fetch_stamp_and_write_deny_need_no_django_or_httpx(self) -> None:
        code = textwrap.dedent(f"""
            import builtins
            import tempfile
            from pathlib import Path
            original = builtins.__import__
            def reject(name, *args, **kwargs):
                if name == 'httpx' or name.startswith('django'):
                    raise ModuleNotFoundError(name)
                return original(name, *args, **kwargs)
            builtins.__import__ = reject
            from hooks.scripts import hook_router as router
            from hooks.scripts import notion_mcp_boundary as boundary
            assert boundary._canonical('{PAGE.replace("-", "")}') == '{PAGE}'
            with tempfile.TemporaryDirectory() as root:
                router.STATE_DIR = Path(root)
                write = {{'session_id': 'cold', 'tool_name': '{UPDATE}', 'tool_input': {{'page_id': '{PAGE}'}}}}
                assert boundary.handle_block_notion_write_boundary(write) is True
                fetch = {{'session_id': 'cold', 'tool_name': '{FETCH}', 'tool_input': {{'id': '{PAGE}'}}}}
                fetch['tool_response'] = [{{'type': 'text', 'text': 'Page {PAGE}'}}]
                boundary.handle_track_notion_pages(fetch)
                assert (Path(root) / 'cold.notion-fetches').read_text().strip() == '{PAGE}'
                boundary.bootstrap_django_registry = lambda: False
                assert boundary.handle_block_notion_write_boundary(write) is False
        """)
        result = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "tool_input",
        [
            {"data": {"content_updates": [{"new_str": "[gate-escape:notion_boundary: audited edit]"}]}},
            {"rich_text": [{"text": {"content": "[gate-escape:notion_boundary: audited comment]"}}]},
            {
                "children": [
                    {
                        "paragraph": {
                            "rich_text": [{"text": {"content": "[gate-escape:notion_boundary: audited block]"}}]
                        }
                    }
                ]
            },
        ],
    )
    def test_nested_notion_escape_reaches_the_real_audit_request(
        self, tool_input: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("hooks.scripts.gate_escape._head", lambda _cwd: "head")
        request = _audit_request(
            {"session_id": SESSION, "cwd": "/tmp", "tool_name": UPDATE, "tool_input": tool_input},
            "notion_boundary",
            None,
        )
        assert isinstance(request, dict)
        assert request["reason"].startswith("audited ")

    def test_an_unread_page_is_refused_with_the_page_id_and_the_fetch_guidance(
        self, notion: FakeNotion, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert boundary.handle_block_notion_write_boundary(_write()) is True
        reason = _deny_reason(capsys)
        assert PAGE in reason
        assert "fetch" in reason
        assert "notion_boundary" in reason

    def test_a_page_fetched_in_this_session_is_allowed(self, notion: FakeNotion) -> None:
        boundary.handle_track_notion_pages(_fetched())
        assert boundary.handle_block_notion_write_boundary(_write()) is False

    def test_the_stamp_does_not_carry_to_another_session(
        self, notion: FakeNotion, capsys: pytest.CaptureFixture[str]
    ) -> None:
        boundary.handle_track_notion_pages(_fetched())
        assert boundary.handle_block_notion_write_boundary(_write(session="sub-agent-session")) is True
        assert PAGE in _deny_reason(capsys)

    def test_a_sub_agent_sharing_the_session_id_must_fetch_for_itself(self, notion: FakeNotion) -> None:
        boundary.handle_track_notion_pages(_fetched())
        assert boundary.handle_block_notion_write_boundary({**_write(), "agent_id": "agent-7"}) is True
        boundary.handle_track_notion_pages({**_fetched(), "agent_id": "agent-7"})
        assert boundary.handle_block_notion_write_boundary({**_write(), "agent_id": "agent-7"}) is False
        assert boundary.handle_block_notion_write_boundary({**_write(), "agent_id": "agent-8"}) is True

    def test_a_fetch_of_another_page_does_not_clear_this_one(self, notion: FakeNotion) -> None:
        boundary.handle_track_notion_pages(_fetched(OTHER_PAGE))
        assert boundary.handle_block_notion_write_boundary(_write()) is True

    @pytest.mark.parametrize("spelling", ["upper", "bare", "url"])
    def test_any_spelling_of_the_page_id_meets_the_same_stamp(self, spelling: str, notion: FakeNotion) -> None:
        boundary.handle_track_notion_pages(_fetched())
        written = {
            "upper": PAGE.upper(),
            "bare": PAGE.replace("-", ""),
            "url": f"https://www.notion.so/Title-{PAGE.replace('-', '')}",
        }[spelling]
        assert boundary.handle_block_notion_write_boundary(_write(written)) is False

    @pytest.mark.parametrize("verdict", [ARCHIVED, UNKNOWN], ids=["archived", "unknown"])
    def test_a_page_not_confirmed_live_is_refused_even_after_a_fetch(
        self, verdict: LivenessVerdict, notion: FakeNotion, capsys: pytest.CaptureFixture[str]
    ) -> None:
        notion.verdict = verdict
        boundary.handle_track_notion_pages(_fetched())
        assert boundary.handle_block_notion_write_boundary(_write()) is True
        reason = _deny_reason(capsys)
        assert PAGE in reason
        assert verdict.headline(PAGE) in reason
        assert "audit-fetch" in reason

    def test_the_audit_guidance_does_not_claim_to_authorize_the_write(
        self, notion: FakeNotion, capsys: pytest.CaptureFixture[str]
    ) -> None:
        notion.verdict = ARCHIVED
        boundary.handle_track_notion_pages(_fetched())
        boundary.handle_block_notion_write_boundary(_write())
        assert "does not authorize" in _deny_reason(capsys)

    def test_a_live_verdict_is_cached_per_session_and_page(self, notion: FakeNotion) -> None:
        boundary.handle_track_notion_pages(_fetched())
        boundary.handle_block_notion_write_boundary(_write())
        boundary.handle_block_notion_write_boundary(_write())
        assert notion.probed == [PAGE]

    def test_a_live_verdict_expires(self, notion: FakeNotion) -> None:
        boundary.handle_track_notion_pages(_fetched())
        with patch.object(boundary.time, "time", return_value=1_000_000.0):
            boundary.handle_block_notion_write_boundary(_write())
        with patch.object(boundary.time, "time", return_value=1_003_600.0):
            boundary.handle_block_notion_write_boundary(_write())
        assert notion.probed == [PAGE, PAGE]

    def test_a_completed_write_drops_the_cached_live_verdict(self, notion: FakeNotion) -> None:
        boundary.handle_track_notion_pages(_fetched())
        boundary.handle_block_notion_write_boundary(_write())
        boundary.handle_track_notion_pages({**_write(), "tool_response": [{"type": "text", "text": "ok"}]})
        boundary.handle_block_notion_write_boundary(_write())
        assert notion.probed == [PAGE, PAGE]

    def test_a_dead_verdict_is_never_cached(self, notion: FakeNotion) -> None:
        notion.verdict = ARCHIVED
        boundary.handle_track_notion_pages(_fetched())
        boundary.handle_block_notion_write_boundary(_write())
        notion.verdict = LIVE
        boundary.handle_track_notion_pages(_fetched())
        assert boundary.handle_block_notion_write_boundary(_write()) is False

    @pytest.mark.parametrize(
        "failure",
        [NotionTokenMissingError("no token"), httpx.ConnectError("offline")],
        ids=["no-token", "no-network"],
    )
    def test_an_unavailable_probe_is_named_and_the_fetch_boundary_still_applies(
        self, failure: Exception, notion: FakeNotion, capsys: pytest.CaptureFixture[str]
    ) -> None:
        notion.failure = failure
        assert boundary.handle_block_notion_write_boundary(_write()) is True
        assert PAGE in _deny_reason(capsys)
        boundary.handle_track_notion_pages(_fetched())
        assert boundary.handle_block_notion_write_boundary(_write()) is False
        assert "liveness" in capsys.readouterr().err

    def test_a_failed_probe_is_not_cached(self, notion: FakeNotion) -> None:
        notion.failure = httpx.ConnectError("offline")
        boundary.handle_track_notion_pages(_fetched())
        boundary.handle_block_notion_write_boundary(_write())
        notion.failure = None
        boundary.handle_block_notion_write_boundary(_write())
        assert notion.probed == [PAGE, PAGE]

    def test_a_timeout_during_probe_error_reporting_keeps_the_offline_fallback(
        self, notion: FakeNotion, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        notion.failure = NotionTokenMissingError("no token")
        boundary.handle_track_notion_pages(_fetched())
        skipped = boundary._skipped
        calls = 0

        def interrupt_once(page_id: str, why: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise boundary._ProbeOutOfTimeError
            skipped(page_id, why)

        monkeypatch.setattr(boundary, "_skipped", interrupt_once)
        assert boundary.handle_block_notion_write_boundary(_write()) is False
        assert "liveness" in capsys.readouterr().err

    def test_a_missing_django_registry_is_named_and_the_fetch_boundary_still_applies(
        self, notion: FakeNotion, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(boundary, "bootstrap_django_registry", lambda: False)
        assert boundary.handle_block_notion_write_boundary(_write()) is True
        boundary.handle_track_notion_pages(_fetched())
        assert boundary.handle_block_notion_write_boundary(_write()) is False
        assert "liveness" in capsys.readouterr().err
        assert notion.probed == []

    def test_a_probe_that_outruns_the_hook_budget_is_cut_off_and_named(
        self, notion: FakeNotion, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _hang(_page_id: str) -> LivenessVerdict:
            time.sleep(30)
            return LIVE

        monkeypatch.setattr(notion, "page_liveness", _hang)
        monkeypatch.setattr(boundary, "remaining_timeout_s", lambda _preferred: 0.2)
        boundary.handle_track_notion_pages(_fetched())
        started = time.monotonic()
        assert boundary.handle_block_notion_write_boundary(_write()) is False
        assert time.monotonic() - started < 10
        assert "liveness" in capsys.readouterr().err

    def test_no_time_left_in_the_hook_budget_skips_the_probe_and_keeps_the_stamp_check(
        self, notion: FakeNotion, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(boundary, "remaining_timeout_s", lambda _preferred: None)
        boundary.handle_track_notion_pages(_fetched())
        assert boundary.handle_block_notion_write_boundary(_write()) is False
        assert notion.probed == []
        assert "no time left" in capsys.readouterr().err

    def test_the_probe_note_is_one_line_even_when_the_error_is_a_setup_guide(
        self, notion: FakeNotion, capsys: pytest.CaptureFixture[str]
    ) -> None:
        notion.failure = NotionTokenMissingError("no token\nstep 1\nstep 2")
        boundary.handle_track_notion_pages(_fetched())
        boundary.handle_block_notion_write_boundary(_write())
        assert "step 1" not in capsys.readouterr().err

    def test_a_malformed_cache_line_is_ignored(self, notion: FakeNotion, state_dir: Path) -> None:
        (state_dir / f"{SESSION}.notion-live").write_text(f"garbage\n{PAGE}\tsoon\n", encoding="utf-8")
        boundary.handle_track_notion_pages(_fetched())
        assert boundary.handle_block_notion_write_boundary(_write()) is False
        assert notion.probed == [PAGE]

    def test_a_completed_write_with_nothing_cached_changes_nothing(self, notion: FakeNotion, state_dir: Path) -> None:
        boundary.handle_track_notion_pages({**_write(), "tool_response": [{"type": "text", "text": "ok"}]})
        assert not (state_dir / f"{SESSION}.notion-live").exists()

    def test_a_per_call_escape_allows_one_refused_write(
        self, notion: FakeNotion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router._gate_escape, "escape_allows", lambda _data, gate_id: gate_id == "notion_boundary")
        assert boundary.handle_block_notion_write_boundary(_write()) is False

    def test_a_write_without_a_session_id_cannot_be_held_to_a_stamp(self, notion: FakeNotion) -> None:
        assert boundary.handle_block_notion_write_boundary(_write(session="")) is False


class TestWriteTargets:
    def test_a_link_to_page_child_does_not_become_a_write_target(self, notion: FakeNotion) -> None:
        boundary.handle_track_notion_pages(_fetched())
        data = _write(children=[{"link_to_page": {"page_id": OTHER_PAGE}}])
        assert boundary.handle_block_notion_write_boundary(data) is False
        assert notion.probed == [PAGE]

    def test_move_pages_checks_each_moved_page(self, notion: FakeNotion, capsys: pytest.CaptureFixture[str]) -> None:
        data = {
            "session_id": SESSION,
            "tool_name": "mcp__notion__notion-move-pages",
            "tool_input": {"page_or_database_ids": [PAGE, OTHER_PAGE]},
        }
        boundary.handle_track_notion_pages(_fetched(PAGE))
        assert boundary.handle_block_notion_write_boundary(data) is True
        assert OTHER_PAGE in _deny_reason(capsys)

    def test_a_create_under_a_page_is_held_to_the_parent_page(
        self, notion: FakeNotion, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = {
            "session_id": SESSION,
            "tool_name": CREATE_PAGES,
            "tool_input": {"parent": {"page_id": PAGE}, "pages": [{"properties": {"title": "New"}}]},
        }
        assert boundary.handle_block_notion_write_boundary(data) is True
        assert PAGE in _deny_reason(capsys)
        boundary.handle_track_notion_pages(_fetched())
        assert boundary.handle_block_notion_write_boundary(data) is False

    @pytest.mark.parametrize(
        "tool_input",
        [
            {"parent": {"database_id": OTHER_PAGE}, "pages": [{"properties": {"title": "Row"}}]},
            {"parent": {"data_source_id": OTHER_PAGE}, "pages": [{"properties": {"title": "Row"}}]},
            {"pages": [{"properties": {"title": "Top-level page"}}]},
        ],
        ids=["database", "data-source", "workspace"],
    )
    def test_a_write_naming_no_page_has_no_page_boundary(self, tool_input: dict, notion: FakeNotion) -> None:
        data = {"session_id": SESSION, "tool_name": CREATE_PAGES, "tool_input": tool_input}
        assert boundary.handle_block_notion_write_boundary(data) is False
        assert notion.probed == []

    def test_a_block_edit_needs_that_block_fetched_and_is_never_probed_as_a_page(
        self, notion: FakeNotion, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = {
            "session_id": SESSION,
            "tool_name": "mcp__notion__API-update-a-block",
            "tool_input": {"block_id": OTHER_PAGE, "paragraph": {"rich_text": []}},
        }
        assert boundary.handle_block_notion_write_boundary(data) is True
        assert OTHER_PAGE in _deny_reason(capsys)
        boundary.handle_track_notion_pages(
            {**_fetched(OTHER_PAGE), "tool_name": "mcp__notion__API-retrieve-a-block"},
        )
        assert boundary.handle_block_notion_write_boundary(data) is False
        assert notion.probed == []

    def test_every_page_a_call_names_must_be_read(self, notion: FakeNotion, capsys: pytest.CaptureFixture[str]) -> None:
        data = _write(parent={"page_id": OTHER_PAGE})
        boundary.handle_track_notion_pages(_fetched())
        assert boundary.handle_block_notion_write_boundary(data) is True
        assert OTHER_PAGE in _deny_reason(capsys)

    def test_a_malformed_target_is_refused_not_ignored(
        self, notion: FakeNotion, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert boundary.handle_block_notion_write_boundary(_write("not-a-page")) is True
        assert "not-a-page" in _deny_reason(capsys)

    @pytest.mark.parametrize(
        "tool_input",
        [
            {"page_id": [PAGE]},
            {"block_id": [PAGE]},
            {"page_or_database_ids": f'["{PAGE}"]'},
            {"page_or_database_ids": [PAGE, 7]},
        ],
        ids=["page-list", "block-list", "stringified-array", "non-string-member"],
    )
    def test_a_malformed_target_shape_is_refused(
        self, tool_input: dict, notion: FakeNotion, capsys: pytest.CaptureFixture[str]
    ) -> None:
        boundary.handle_track_notion_pages(_fetched())
        data = {"session_id": SESSION, "tool_name": "mcp__notion__notion-move-pages", "tool_input": tool_input}
        assert boundary.handle_block_notion_write_boundary(data) is True
        assert "cannot be checked" in _deny_reason(capsys)

    def test_a_write_with_no_target_at_all_is_not_held(self, notion: FakeNotion) -> None:
        assert boundary.handle_block_notion_write_boundary(_write(None)) is False


class TestToolsTheBoundaryLeavesAlone:
    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__teatree__notion_replace",
            "mcp__plugin_t3_teatree__notion_comment",
            "mcp__connector_Notion__notion-fetch",
            "mcp__connector_Notion__notion-search",
            "mcp__connector_Notion__notion-get-comments",
            "mcp__slack__slack_send_message",
            "Bash",
        ],
    )
    def test_reads_other_servers_and_teatrees_own_notion_tools_are_not_held(
        self, tool_name: str, notion: FakeNotion
    ) -> None:
        assert boundary.handle_block_notion_write_boundary(_write(tool_name=tool_name)) is False
        assert notion.probed == []

    def test_a_teatree_owned_notion_write_still_has_its_text_scanned(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = {
            "session_id": SESSION,
            "tool_name": "mcp__teatree__notion_replace",
            "tool_input": {"page": PAGE, "old_text": "x", "new_text": HIGH_QUOTE},
        }
        assert handle_quote_scanner_pretool(data) is True
        assert "heading-user-ask-verbatim" in _deny_reason(capsys)


class TestRegistration:
    def test_both_handlers_are_on_the_router_chains(self) -> None:
        assert boundary.handle_block_notion_write_boundary in router._HANDLERS["PreToolUse"]
        assert boundary.handle_track_notion_pages in router._HANDLERS["PostToolUse"]
