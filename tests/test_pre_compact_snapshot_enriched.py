"""Tests for the enriched PreCompact snapshot content (issue #970).

The existing #778 snapshot only captures (a) loop assignment, (b) the
ever-touched ``.active`` ledger and (c) the loaded skills. The user
report on #970 is that this is too thin to actually resume work: it
misses the *current* worktree (cwd), branch, HEAD, working-tree dirty
state, unpushed commits, the live in-flight PRs and the current TODO
list — the things the agent and the user both need to rebuild "what
was I doing" after compaction. These tests pin the enriched capture so
the snapshot reliably contains that recovery-relevant state.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _T3_TEMP_PREFIX, handle_pre_compact, handle_track_agents


@pytest.fixture(autouse=True)
def _isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate STATE_DIR, _TMP_DIR, loop registry and disable network shell-outs.

    The snapshot enrichments shell out to ``gh`` for live PR state; the test
    suite must never hit the network, so the helper is monkeypatched to a
    deterministic stub. Real ``git`` under ``tmp_path`` is the project's
    standard pattern (see ``test_claude_statusline.py``) and is used as-is
    for the per-repo branch / dirty / unpushed checks.
    """
    router.STATE_DIR = tmp_path / "state"
    router._TMP_DIR = tmp_path / "tmp"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    router._TMP_DIR.mkdir(parents=True, exist_ok=True)
    reg_dir = tmp_path / "data"
    reg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))
    # Default: PRs lookup returns empty (no network). Individual tests
    # override this to assert PR rendering.
    monkeypatch.setattr(router, "_open_prs_for_repo", lambda _path: [])


def _snapshot_for(session_id: str) -> Path:
    return router.STATE_DIR / f"{_T3_TEMP_PREFIX}{session_id}-precompact.md"


def _git_init_repo(path: Path) -> None:
    """Create a real git repo with one initial commit on ``main``."""
    path.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)  # noqa: S607
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)  # noqa: S607
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)  # noqa: S607
    (path / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, env=env)  # noqa: S607
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True, env=env)  # noqa: S607


class TestSnapshotIncludesCurrentWorktreeCwd:
    """The cwd the harness passes is the single most useful "where am I" anchor."""

    def test_cwd_when_inside_workspace_appears_in_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session_id = "sess-cwd"
        workspace = tmp_path / "workspace"
        repo = workspace / "myticket" / "myrepo"
        _git_init_repo(repo)
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))

        handle_pre_compact({"session_id": session_id, "cwd": str(repo)})

        body = _snapshot_for(session_id).read_text(encoding="utf-8")
        assert "Current working directory" in body
        assert str(repo) in body

    def test_missing_cwd_does_not_crash_hook(self) -> None:
        # Older harness payloads may omit cwd — the hook must not raise.
        handle_pre_compact({"session_id": "sess-nocwd"})
        assert _snapshot_for("sess-nocwd").is_file()


class TestSnapshotIncludesCurrentGitState:
    """Branch, HEAD, dirty, unpushed — the post-compaction "don't lose work" set."""

    def test_branch_and_head_sha_for_cwd_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        session_id = "sess-git"
        workspace = tmp_path / "workspace"
        repo = workspace / "tk" / "repo"
        _git_init_repo(repo)
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-q", "-b", "ac/feature-x"],  # noqa: S607
            check=True,
        )
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))

        handle_pre_compact({"session_id": session_id, "cwd": str(repo)})

        body = _snapshot_for(session_id).read_text(encoding="utf-8")
        assert "ac/feature-x" in body
        # Short HEAD SHA appears (first 7 chars of the rev-parse output).
        head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],  # noqa: S607
            text=True,
        ).strip()
        assert head in body

    def test_uncommitted_changes_flagged_in_snapshot(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        session_id = "sess-dirty"
        workspace = tmp_path / "workspace"
        repo = workspace / "tk" / "repo"
        _git_init_repo(repo)
        (repo / "uncommitted.txt").write_text("WIP\n", encoding="utf-8")
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))

        handle_pre_compact({"session_id": session_id, "cwd": str(repo)})

        body = _snapshot_for(session_id).read_text(encoding="utf-8")
        # The exact phrasing is not the contract; the contract is that
        # "there is uncommitted work" is visible to a fresh session.
        assert "uncommitted" in body.lower()

    def test_clean_repo_does_not_falsely_claim_dirty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        session_id = "sess-clean"
        workspace = tmp_path / "workspace"
        repo = workspace / "tk" / "repo"
        _git_init_repo(repo)
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))

        handle_pre_compact({"session_id": session_id, "cwd": str(repo)})

        body = _snapshot_for(session_id).read_text(encoding="utf-8")
        assert "uncommitted" not in body.lower() or "0 uncommitted" in body.lower()


class TestSnapshotIncludesOpenPRs:
    """In-flight PRs — recovery-relevant per #970 'in-flight PRs'."""

    def test_open_prs_for_cwd_repo_rendered(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        session_id = "sess-pr"
        workspace = tmp_path / "workspace"
        repo = workspace / "tk" / "repo"
        _git_init_repo(repo)
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.setattr(
            router,
            "_open_prs_for_repo",
            lambda _p: [
                {
                    "number": 970,
                    "title": "feat(hooks): enrich PreCompact snapshot",
                    "headRefName": "s-teatree-970-fix",
                    "isDraft": False,
                },
                {
                    "number": 845,
                    "title": "fix(hooks): SessionStart compact recovery",
                    "headRefName": "ac/recover",
                    "isDraft": True,
                },
            ],
        )

        handle_pre_compact({"session_id": session_id, "cwd": str(repo)})

        body = _snapshot_for(session_id).read_text(encoding="utf-8")
        assert "Open PRs" in body
        assert "#970" in body
        assert "enrich PreCompact snapshot" in body
        assert "#845" in body
        # Draft state is visible so the agent knows the PR is not ready.
        assert "draft" in body.lower()

    def test_no_open_prs_does_not_print_header(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        session_id = "sess-nopr"
        workspace = tmp_path / "workspace"
        repo = workspace / "tk" / "repo"
        _git_init_repo(repo)
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        # Default stub already returns [] — no PRs.

        handle_pre_compact({"session_id": session_id, "cwd": str(repo)})

        body = _snapshot_for(session_id).read_text(encoding="utf-8")
        assert "Open PRs" not in body


def _seed_harness_store(tmp_path: Path, session_id: str, todos: list[tuple[str, str]]) -> None:
    """Write one ``<n>.json`` per harness TODO under ``CLAUDE_TASKS_DIR/<session>``."""
    session_dir = tmp_path / "harness-tasks" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    for index, (status, subject) in enumerate(todos, start=1):
        (session_dir / f"{index}.json").write_text(json.dumps({"subject": subject, "status": status}), encoding="utf-8")


class TestSnapshotTodosFromHarnessStore:
    """The PreCompact snapshot quotes the harness's OWN on-disk store (#1736).

    The harness writes ``~/.claude/tasks/<session>/*.json`` itself; teatree
    only reads it best-effort here for the recovery snapshot. There is no
    teatree-written ``<session>.todos`` mirror — that materialiser was a stale
    mistake-source and was removed; the in-session reconciliation discipline
    (``/t3:checking`` § "Harness-TODO maintenance") keeps the LIVE list faithful.
    """

    def test_snapshot_renders_harness_store_todos(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        session_id = "sess-todo-render"
        _seed_harness_store(
            tmp_path,
            session_id,
            [("in_progress", "fix snapshot"), ("pending", "push PR")],
        )
        monkeypatch.setenv("CLAUDE_TASKS_DIR", str(tmp_path / "harness-tasks"))

        handle_pre_compact({"session_id": session_id})

        body = _snapshot_for(session_id).read_text(encoding="utf-8")
        assert "Pending TODOs" in body
        assert "fix snapshot" in body
        assert "push PR" in body


class TestAgentDispatchCaptureAndSnapshot:
    """Background sub-agents dispatched via the Agent tool must survive compaction.

    Issue #778 (reopened): the PreCompact snapshot captured the loop
    tick-owner (#786 WS3) but NOT ad-hoc background sub-agents an
    orchestrator dispatches via the ``Agent`` tool. Those agentIds — the
    handle ``SendMessage`` needs to resume/steer/collect a running agent
    — live only in the conversation and are lost on auto-compaction,
    orphaning the running agents. Mirror the #970 ``TodoWrite`` capture:
    a PostToolUse on ``Agent`` appends the dispatched agentId + its role
    to ``<session>.agents`` so the snapshot can quote the roster back.
    """

    def test_agent_dispatch_captured_to_session_agents_file(self) -> None:
        session_id = "sess-agent-capture"
        handle_track_agents(
            {
                "session_id": session_id,
                "tool_name": "Agent",
                "tool_input": {"description": "implement #778 fix", "subagent_type": "t3-coder"},
                "tool_response": {"agentId": "a1b2c3d4"},
            }
        )
        agents_file = router.STATE_DIR / f"{session_id}.agents"
        assert agents_file.is_file()
        text = agents_file.read_text(encoding="utf-8")
        assert "a1b2c3d4" in text
        assert "implement #778 fix" in text

    def test_non_agent_tool_does_not_write_agents_file(self) -> None:
        session_id = "sess-agent-noclobber"
        handle_track_agents({"session_id": session_id, "tool_name": "Read", "tool_input": {"file_path": "x"}})
        assert not (router.STATE_DIR / f"{session_id}.agents").is_file()

    def test_multiple_dispatches_accumulate_not_clobber(self) -> None:
        session_id = "sess-agent-accumulate"
        handle_track_agents(
            {
                "session_id": session_id,
                "tool_name": "Agent",
                "tool_input": {"description": "first agent"},
                "tool_response": {"agentId": "aaaa1111"},
            }
        )
        handle_track_agents(
            {
                "session_id": session_id,
                "tool_name": "Agent",
                "tool_input": {"description": "second agent"},
                "tool_response": {"agentId": "bbbb2222"},
            }
        )
        text = (router.STATE_DIR / f"{session_id}.agents").read_text(encoding="utf-8")
        assert "aaaa1111" in text
        assert "bbbb2222" in text
        assert "first agent" in text
        assert "second agent" in text

    def test_snapshot_renders_dispatched_agents_roster(self) -> None:
        session_id = "sess-agent-render"
        handle_track_agents(
            {
                "session_id": session_id,
                "tool_name": "Agent",
                "tool_input": {"description": "fix snapshot regression", "subagent_type": "t3-coder"},
                "tool_response": {"agentId": "deadbeef"},
            }
        )

        handle_pre_compact({"session_id": session_id})

        body = _snapshot_for(session_id).read_text(encoding="utf-8")
        assert "deadbeef" in body
        assert "fix snapshot regression" in body

    def test_agent_dispatch_capture_falls_back_to_tasks_dir_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When the PostToolUse payload does not expose the agentId directly,
        # the handler scans the harness tasks output dir for the newest
        # ``a*``-prefixed id (the SendMessage handle).
        session_id = "sess-agent-fallback"
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir(parents=True)
        (tasks_dir / "afeedface.output").write_text("running\n", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_TASKS_DIR", str(tasks_dir))

        handle_track_agents(
            {
                "session_id": session_id,
                "tool_name": "Agent",
                "tool_input": {"description": "no-id agent"},
                "tool_response": {},
            }
        )

        text = (router.STATE_DIR / f"{session_id}.agents").read_text(encoding="utf-8")
        assert "afeedface" in text
        assert "no-id agent" in text


class TestSnapshotResilience:
    """The snapshot must never fail compaction — the worst case is empty sections."""

    def test_cwd_not_a_git_repo_renders_no_git_section_no_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session_id = "sess-nogit"
        workspace = tmp_path / "workspace"
        not_a_repo = workspace / "loose"
        not_a_repo.mkdir(parents=True)
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))

        handle_pre_compact({"session_id": session_id, "cwd": str(not_a_repo)})

        body = _snapshot_for(session_id).read_text(encoding="utf-8")
        # No crash, file exists, no fake git data injected.
        assert _snapshot_for(session_id).is_file()
        assert "rev-parse" not in body  # no error tracebacks leaked

    def test_gh_failure_in_pr_lookup_is_swallowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        session_id = "sess-gh-fail"
        workspace = tmp_path / "workspace"
        repo = workspace / "tk" / "repo"
        _git_init_repo(repo)
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))

        boom_msg = "gh: auth failed"

        def _boom(_p: Path) -> list[dict]:
            raise RuntimeError(boom_msg)

        monkeypatch.setattr(router, "_open_prs_for_repo", _boom)

        # The hook must still produce a snapshot — never block on a PR lookup.
        handle_pre_compact({"session_id": session_id, "cwd": str(repo)})
        assert _snapshot_for(session_id).is_file()
