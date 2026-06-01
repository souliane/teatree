"""Tests for ``hooks/scripts/statusline.sh`` — the Claude Code statusline hook.

The hook composes two info streams: the loop's pre-rendered zones file (anchors,
action_needed, in_flight) and live per-session info from Claude's stdin JSON
(model, ctx %, loaded skills).
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hooks" / "scripts" / "statusline.sh"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;[^\x1b]*\x1b\\")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _run(
    payload: dict,
    *,
    state_dir: Path,
    statusline_file: Path | None = None,
    registry_dir: Path | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(state_dir)
    if statusline_file is not None:
        env["TEATREE_STATUSLINE_FILE"] = str(statusline_file)
    if registry_dir is not None:
        env["T3_LOOP_REGISTRY_DIR"] = str(registry_dir)
    return subprocess.run(
        [str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=REPO_ROOT,
    )


class TestStatuslineHook:
    def test_displays_loaded_skills_from_session_file(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "session-skills.skills").write_text("t3:code\nt3:debug\n", encoding="utf-8")

        result = _run(
            {"session_id": "session-skills", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        # Skills sharing a ``<ns>:`` prefix collapse to one ``ns:{a,b}`` token.
        assert "skills: t3:{code,debug}" in _strip_ansi(result.stdout)

    def test_omits_skills_when_session_file_absent(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _run(
            {"session_id": "no-skills", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        assert "skills:" not in _strip_ansi(result.stdout)

    def test_renders_rate_limits_from_stdin(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _run(
            {
                "session_id": "s1",
                "model": {"display_name": "Claude Opus"},
                "rate_limits": {
                    "five_hour": {"used_percentage": 42, "resets_at": "1747047000"},
                    "seven_day": {"used_percentage": 85},
                },
            },
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "5h=42%" in plain
        assert "7d=85%" in plain

    def test_renders_model_and_context_window_from_stdin(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _run(
            {
                "session_id": "s1",
                "model": {"display_name": "Claude Sonnet"},
                "context_window": {"used_percentage": 41.8},
            },
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "model=Claude Sonnet" in plain
        assert "ctx=41%" in plain

    def test_renders_free_disk_segment_after_ram(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _run(
            {"session_id": "s-disk", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        match = re.search(r"disk=\d+% \d+G free", plain)
        assert match is not None, plain
        # The disk segment follows the RAM segment within the resource group.
        assert plain.index("ram=") < match.start()

    def test_disk_segment_omitted_when_df_target_unreadable(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        env = os.environ.copy()
        env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(state_dir)
        env["HOME"] = str(tmp_path / "does-not-exist")

        result = subprocess.run(
            [str(SCRIPT)],
            input=json.dumps({"session_id": "s-nodisk", "model": {"display_name": "Claude Opus"}}),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            cwd=REPO_ROOT,
        )

        assert result.returncode == 0, result.stderr
        assert "disk=" not in _strip_ansi(result.stdout)

    def test_appends_pre_rendered_loop_zones_file(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        statusline_file = tmp_path / "statusline.txt"
        statusline_file.write_text("tick @ 2026-05-07T12:00:00\nIn flight:\n→ statusline: x\n", encoding="utf-8")

        result = _run(
            {"session_id": "s1", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            statusline_file=statusline_file,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "model=Claude Opus" in plain
        assert "tick @ 2026-05-07T12:00:00" in result.stdout
        assert "→ statusline: x" in result.stdout

    def test_handles_missing_loop_file_gracefully(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        missing = tmp_path / "nope.txt"

        result = _run(
            {"session_id": "s1", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            statusline_file=missing,
        )

        assert result.returncode == 0, result.stderr
        assert "model=Claude Opus" in _strip_ansi(result.stdout)

    def test_header_carries_no_loop_or_tick_fragment(self, tmp_path: Path) -> None:
        """#130: loop/tick info has exactly one home — the dedicated loop line.

        Even with a populated ``.crons`` state file and a ``tick-meta.json``
        next-tick epoch present, the header this hook builds must carry NO
        ``loops:`` / ``tick→`` / wakeup fragment. The single loop line is
        rendered by the fat loop into the zones file (``live_loops_anchor``)
        and cat'd verbatim — duplicating it in the header is the pollution
        the dashboard rework removed.
        """
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        crons = {
            "jobs": {"job-1": {"name": "tick", "cron": "*/12 * * * *", "cadence": 720, "created_at": 0}},
            "wakeup": {"name": "checking build", "next_epoch": int(time.time()) + 180},
        }
        (state_dir / "s-no-loop.crons").write_text(json.dumps(crons), encoding="utf-8")
        statusline_file = tmp_path / "statusline.txt"
        statusline_file.write_text("loop running · tick 11m\n", encoding="utf-8")
        (tmp_path / "tick-meta.json").write_text(
            json.dumps({"next_epoch": int(time.time()) + 120, "cadence": 720, "freshness": {}}),
            encoding="utf-8",
        )

        result = _run(
            {"session_id": "s-no-loop", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            statusline_file=statusline_file,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        header = plain.splitlines()[0]
        assert "loops:" not in header, header
        assert "tick→" not in header, header
        assert "tick(" not in header, header
        assert "checking build" not in header, header
        # The one loop line (from the zones file) is still cat'd verbatim.
        assert "loop running · tick 11m" in plain, plain
        # And it appears exactly once.
        assert plain.count("loop running") == 1, plain

    def test_no_session_id_emits_no_skills_section(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # A skills file exists but the payload has no session_id — must not pick it up
        (state_dir / ".skills").write_text("rogue\n", encoding="utf-8")

        result = _run(
            {"model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "skills:" not in plain
        assert "rogue" not in plain


class TestSkillsNamespaceGrouping:
    """Skills sharing a ``<ns>:`` prefix collapse into ``ns:{a,b,c}`` to save width."""

    def test_groups_shared_namespace_into_brace_form(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "s-grp.skills").write_text(
            "t3:code\nt3:ship\nt3:review\nac-django\nupdate-translations\n",
            encoding="utf-8",
        )

        result = _run(
            {"session_id": "s-grp", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        # The three t3:* skills collapse to a single braced token; un-namespaced
        # skills stay verbatim.
        assert "t3:{code,ship,review}" in plain, plain
        assert "ac-django" in plain, plain
        assert "update-translations" in plain, plain
        # The expanded per-skill tokens must not also appear.
        assert "t3:code " not in plain, plain
        assert "t3:ship" not in plain.replace("t3:{code,ship,review}", ""), plain

    def test_single_member_namespace_stays_verbatim(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "s-one.skills").write_text("t3:code\nac-django\n", encoding="utf-8")

        result = _run(
            {"session_id": "s-one", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        # A lone member of a namespace is not worth braces — render as-is.
        assert "t3:code" in plain, plain
        assert "t3:{code}" not in plain, plain
        assert "ac-django" in plain, plain


class TestFreshnessInlineRefresh:
    """statusline.sh recomputes ``behind`` inline when FETCH_HEAD is newer than the tick."""

    def _make_repo_behind_main(self, repo: Path, *, behind_by: int) -> int:
        """Create a tiny git repo with ``behind_by`` commits on origin/main not on HEAD.

        Returns the FETCH_HEAD mtime so the test can choose to mark it
        newer or older than the cached ``fetch_epoch``.
        """
        repo.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        run = lambda *args: subprocess.run(args, cwd=repo, env=env, check=True, capture_output=True)  # noqa: E731
        run("git", "init", "-q", "-b", "main")
        run("git", "config", "user.email", "a@b.c")
        run("git", "config", "user.name", "t")
        (repo / "README").write_text("x")
        run("git", "add", "README")
        run("git", "commit", "-q", "-m", "base")
        # Build "origin/main" as a remote-tracking ref ahead of HEAD by N commits.
        bare = repo.parent / "origin.git"
        run("git", "clone", "-q", "--bare", str(repo), str(bare))
        run("git", "remote", "add", "origin", str(bare))
        for i in range(behind_by):
            wt = repo.parent / "pusher"
            if not wt.exists():
                subprocess.run(["git", "clone", "-q", str(bare), str(wt)], env=env, check=True, capture_output=True)  # noqa: S607
                subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=wt, check=True)  # noqa: S607
                subprocess.run(["git", "config", "user.name", "t"], cwd=wt, check=True)  # noqa: S607
            (wt / f"f{i}").write_text("y")
            subprocess.run(["git", "add", f"f{i}"], cwd=wt, env=env, check=True)  # noqa: S607
            subprocess.run(["git", "commit", "-q", "-m", f"c{i}"], cwd=wt, env=env, check=True)  # noqa: S607
            subprocess.run(["git", "push", "-q", "origin", "main"], cwd=wt, env=env, check=True)  # noqa: S607
        run("git", "fetch", "-q", "origin", "main")
        fetch_head = repo / ".git" / "FETCH_HEAD"
        return int(fetch_head.stat().st_mtime)

    def test_uses_cached_value_when_fetch_head_not_newer(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        fetch_epoch = self._make_repo_behind_main(repo, behind_by=2)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        sl = tmp_path / "statusline.txt"
        sl.write_text("anchors\n")
        # tick-meta says behind=99 (stale-but-cached value) with a fetch_epoch
        # at or after the on-disk FETCH_HEAD mtime → script must NOT recompute.
        meta = sl.with_name("tick-meta.json")
        meta.write_text(
            json.dumps(
                {
                    "freshness": {
                        "repo": {"behind": 99, "fetch_epoch": fetch_epoch + 60, "path": str(repo)},
                    },
                },
            ),
        )

        result = _run({"model": {"display_name": "Claude Opus"}}, state_dir=state_dir, statusline_file=sl)
        plain = _strip_ansi(result.stdout)
        assert "repo=99" in plain

    def test_recomputes_when_fetch_head_is_newer(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        fetch_epoch_now = self._make_repo_behind_main(repo, behind_by=2)
        # Simulate a pre-pull tick that recorded behind=99 with an older fetch_epoch.
        # On-disk FETCH_HEAD is newer → script must refresh and show 2.
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        sl = tmp_path / "statusline.txt"
        sl.write_text("anchors\n")
        meta = sl.with_name("tick-meta.json")
        meta.write_text(
            json.dumps(
                {
                    "freshness": {
                        "repo": {"behind": 99, "fetch_epoch": fetch_epoch_now - 3600, "path": str(repo)},
                    },
                },
            ),
        )

        result = _run({"model": {"display_name": "Claude Opus"}}, state_dir=state_dir, statusline_file=sl)
        plain = _strip_ansi(result.stdout)
        assert "repo=2" in plain
        assert "repo=99" not in plain

    def test_no_path_field_falls_back_to_cached_behind(self, tmp_path: Path) -> None:
        # Older tick-meta.json without `path` should still render (no crash).
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        sl = tmp_path / "statusline.txt"
        sl.write_text("anchors\n")
        meta = sl.with_name("tick-meta.json")
        meta.write_text(
            json.dumps({"freshness": {"old": {"behind": 5, "fetch_epoch": int(time.time())}}}),
        )

        result = _run({"model": {"display_name": "Claude Opus"}}, state_dir=state_dir, statusline_file=sl)
        plain = _strip_ansi(result.stdout)
        assert "old=5" in plain


class TestLoopOwnerBadge:
    """Per-session loop-owner badge in the g_context header group.

    The badge reads ``loop-registry.json`` at display time so each terminal
    reflects its own session's ownership relationship — unlike the shared
    loop line written by the loop owner.
    """

    def _write_registry(self, registry_dir: Path, *, session_id: str, pid: int = 4242) -> None:
        registry_dir.mkdir(parents=True, exist_ok=True)
        reg = registry_dir / "loop-registry.json"
        reg.write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": session_id, "pid": pid}}),
            encoding="utf-8",
        )

    def test_you_badge_when_owner_matches_session(self, tmp_path: Path) -> None:
        """Same session owns the loop → green ``loop-owner: you ✓``."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        registry_dir = tmp_path / "registry"
        self._write_registry(registry_dir, session_id="my-session-abc", pid=1234)

        result = _run(
            {"session_id": "my-session-abc", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            registry_dir=registry_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "loop-owner: you ✓" in plain, plain
        # The badge belongs on the loop-specific line, NOT the context header.
        header = plain.splitlines()[0]
        assert "loop-owner:" not in header, header
        # Green SGR present in the raw (non-stripped) output.
        assert "\033[1;32m" in result.stdout, "expected green SGR for owner=you"

    def test_badge_renders_on_loop_line_not_header(self, tmp_path: Path) -> None:
        """The per-session loop-owner badge sits on the loop line region, not g_context."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        registry_dir = tmp_path / "registry"
        self._write_registry(registry_dir, session_id="sess-loop", pid=7)
        statusline_file = tmp_path / "statusline.txt"
        statusline_file.write_text("loop running · tick 5m\n", encoding="utf-8")

        result = _run(
            {"session_id": "sess-loop", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            statusline_file=statusline_file,
            registry_dir=registry_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        lines = plain.splitlines()
        # The header (line 1) carries model/ctx but not the loop-owner badge.
        assert "model=Claude Opus" in lines[0], lines[0]
        assert "loop-owner:" not in lines[0], lines[0]
        # The badge rides the loop line.
        loop_line = next(line for line in lines if "loop running" in line)
        assert "loop-owner: you ✓" in loop_line, loop_line

    def test_foreign_owner_badge_shows_short_sid_and_pid(self, tmp_path: Path) -> None:
        """Different session owns the loop → yellow ``abcdef01·pid4242``."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        registry_dir = tmp_path / "registry"
        self._write_registry(registry_dir, session_id="abcdef0123456789", pid=4242)

        result = _run(
            {"session_id": "other-session", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            registry_dir=registry_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "abcdef01·pid4242" in plain, plain
        assert "loop-owner:" in plain, plain
        # Yellow SGR present (neutral — a foreign owner is normal from a non-owner terminal).
        assert "\033[1;33m" in result.stdout, "expected yellow SGR for foreign owner"

    def test_unclaimed_badge_when_registry_has_no_owner(self, tmp_path: Path) -> None:
        """Readable registry but no owner key → dim ``loop-owner: unclaimed``."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        registry_dir = tmp_path / "registry"
        registry_dir.mkdir()
        reg = registry_dir / "loop-registry.json"
        reg.write_text(json.dumps({}), encoding="utf-8")

        result = _run(
            {"session_id": "any-session", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            registry_dir=registry_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "loop-owner: unclaimed" in plain, plain

    def test_badge_absent_when_registry_missing(self, tmp_path: Path) -> None:
        """No registry file → NO badge (fail-open)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        registry_dir = tmp_path / "empty-registry"
        # Directory does NOT exist — registry file unreadable.

        result = _run(
            {"session_id": "any-session", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            registry_dir=registry_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "loop-owner" not in plain, plain

    def test_badge_absent_when_no_session_id(self, tmp_path: Path) -> None:
        """No session_id in payload → NO badge (cannot determine ownership)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        registry_dir = tmp_path / "registry"
        self._write_registry(registry_dir, session_id="some-session", pid=999)

        result = _run(
            {"model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            registry_dir=registry_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "loop-owner" not in plain, plain
