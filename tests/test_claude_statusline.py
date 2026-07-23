"""Tests for ``hooks/scripts/statusline.sh`` — the Claude Code statusline hook.

The hook composes two info streams: the loop's pre-rendered zones file (anchors,
action_needed, in_flight) and live per-session info from Claude's stdin JSON
(model, ctx %, loaded skills).
"""

import json
import os
import re
import shutil
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


def _harness_tasks_dir(state_dir: Path) -> Path:
    """The harness task-store root the statusline chip is counted from.

    Always isolated under ``state_dir`` so the developer's real
    ``~/.claude/tasks`` never bleeds into a test; a test seeds the chip by
    writing ``<session>/<n>.json`` under this dir (see
    ``TestHarnessTodoSummary._seed_store``).
    """
    return state_dir / "_harness_tasks"


def _run(
    payload: dict,
    *,
    state_dir: Path,
    statusline_file: Path | None = None,
    registry_dir: Path | None = None,
    cpu: tuple[Path, int] | None = None,
) -> subprocess.CompletedProcess:
    # The statusline only renders for a teatree-engaged session that opted into
    # auto-load (#256): stamp the marker AND enable auto-load so these
    # rendering-mechanism tests run as the opted-in owner (the opt-in gates
    # themselves are covered by test_teatree_opt_in.py).
    session_id = payload.get("session_id", "")
    if session_id:
        (state_dir / f"{session_id}.teatree-active").touch()
    env = os.environ.copy()
    env["T3_AUTOLOAD"] = "1"
    env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(state_dir)
    # Isolate the harness config dir onto the test's state dir so the developer's
    # real ~/.claude/settings.json effortLevel never bleeds into these tests; the
    # effort tests plant a settings.json here to drive the fallback (#2214).
    env["CLAUDE_CONFIG_DIR"] = str(state_dir)
    if statusline_file is not None:
        env["TEATREE_STATUSLINE_FILE"] = str(statusline_file)
    if registry_dir is not None:
        env["T3_LOOP_REGISTRY_DIR"] = str(registry_dir)
    # The harness TODO chip is counted from the harness's OWN task store; point
    # it at the test's isolated dir so the developer's real ~/.claude/tasks
    # never bleeds in. A test that does not seed a store leaves this dir empty,
    # so the chip is reliably absent.
    env["CLAUDE_TASKS_DIR"] = str(_harness_tasks_dir(state_dir))
    # Isolate the Agent-Teams config dir so the developer's real ~/.claude/teams
    # roster never bleeds into these tests: the mates zone resolves its teams dir
    # from CLAUDE_CONFIG_DIR (already pinned to state_dir above), so a team config
    # is discovered ONLY when a test plants it under ``state_dir/teams/``.
    if cpu is not None:
        loadavg_file, ncpu = cpu
        env["TEATREE_STATUSLINE_LOADAVG_FILE"] = str(loadavg_file)
        env["TEATREE_STATUSLINE_NCPU"] = str(ncpu)
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

    def test_renders_sdk_cost_chip_next_to_weekly_segment(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        statusline_file = tmp_path / "statusline.txt"
        statusline_file.write_text("tick 5m\n", encoding="utf-8")
        # The cost chip is now the first ``usage``-placed contributed segment
        # (souliane/teatree#3237) — the retired dedicated ``cost_chip`` key.
        (tmp_path / "tick-meta.json").write_text(
            json.dumps({"segments": [{"id": "cost_chip", "text": "SDK mtd ≈$48/$200", "placement": "usage"}]}),
            encoding="utf-8",
        )

        result = _run(
            {
                "session_id": "s-cost",
                "model": {"display_name": "Claude Opus"},
                "rate_limits": {"seven_day": {"used_percentage": 85}},
            },
            state_dir=state_dir,
            statusline_file=statusline_file,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "SDK mtd ≈$48/$200" in plain, plain
        # The cost chip sits immediately after the weekly (7d) usage segment.
        assert plain.index("7d=85%") < plain.index("SDK mtd"), plain

    def test_omits_sdk_cost_chip_when_meta_absent(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _run(
            {"session_id": "s-no-cost", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        assert "SDK mtd" not in _strip_ansi(result.stdout)

    def _run_with_segments(self, tmp_path: Path, segments: list[dict], payload: dict) -> subprocess.CompletedProcess:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        statusline_file = tmp_path / "statusline.txt"
        statusline_file.write_text("tick 5m\n", encoding="utf-8")
        (tmp_path / "tick-meta.json").write_text(json.dumps({"segments": segments}), encoding="utf-8")
        return _run(payload, state_dir=state_dir, statusline_file=statusline_file)

    def test_header_segment_renders_next_to_freshness(self, tmp_path: Path) -> None:
        seg = {"id": "upstream-behind", "text": "⇡12", "color": "yellow", "placement": "header"}
        result = self._run_with_segments(
            tmp_path, [seg], {"session_id": "s-hdr", "model": {"display_name": "Claude Opus"}}
        )
        assert result.returncode == 0, result.stderr
        assert "⇡12" in _strip_ansi(result.stdout), result.stdout

    def test_after_segment_lands_right_after_its_anchor(self, tmp_path: Path) -> None:
        segments = [
            {"id": "cost_chip", "text": "SDK mtd ≈$48/$200", "placement": "usage"},
            {"id": "burn", "text": "burn↑", "placement": "after:cost_chip"},
        ]
        result = self._run_with_segments(
            tmp_path,
            segments,
            {
                "session_id": "s-after",
                "model": {"display_name": "Claude Opus"},
                "rate_limits": {"seven_day": {"used_percentage": 85}},
            },
        )
        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        # ``after:cost_chip`` resolves to the usage group and lands right after
        # the chip it follows.
        assert plain.index("SDK mtd") < plain.index("burn↑"), plain

    def test_unknown_placement_degrades_to_end_of_line(self, tmp_path: Path) -> None:
        segments = [{"id": "mystery", "text": "END-SEG", "placement": "nowhere"}]
        result = self._run_with_segments(
            tmp_path, segments, {"session_id": "s-end", "model": {"display_name": "Claude Opus"}}
        )
        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        # Degrades to the end-of-line group, never errored or dropped.
        assert "END-SEG" in plain, plain
        assert plain.rstrip().index("END-SEG") > plain.index("model="), plain

    def test_segment_color_applies_the_palette(self, tmp_path: Path) -> None:
        seg = {"id": "warn", "text": "warnval", "color": "red", "placement": "header"}
        result = self._run_with_segments(
            tmp_path, [seg], {"session_id": "s-col", "model": {"display_name": "Claude Opus"}}
        )
        assert result.returncode == 0, result.stderr
        # The red SGR (1;31m) wraps the segment text.
        assert "\033[1;31mwarnval" in result.stdout, repr(result.stdout)

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
        rendered by the loop tick into the zones file (``live_loops_anchor``)
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
        statusline_file.write_text("tick 11m\n", encoding="utf-8")
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
        assert "tick 11m" in plain, plain
        # And it appears exactly once; the redundant ``loop running`` token is gone.
        assert plain.count("tick 11m") == 1, plain
        assert "loop running" not in plain, plain

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


class TestEffortSegment:
    """The session's effort level (`/effort`) renders next to the model (#2214).

    The harness statusline stdin JSON carries the model but not the effort
    level; the saved ``/effort`` default lives in the harness settings file
    (``$CLAUDE_CONFIG_DIR/settings.json`` → ``effortLevel``). The hook reads it
    there as a fallback and renders ``model=<m> · <effort>``. The segment is
    omitted entirely when no effort can be resolved, so it never fabricates a
    value or leaves a dangling separator.
    """

    def _write_settings(self, state_dir: Path, effort: str | None) -> None:
        body: dict = {} if effort is None else {"effortLevel": effort}
        (state_dir / "settings.json").write_text(json.dumps(body), encoding="utf-8")

    def test_renders_effort_next_to_model_from_settings(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._write_settings(state_dir, "medium")

        result = _run(
            {"session_id": "s-eff", "model": {"display_name": "opus-4-8"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "model=opus-4-8 · medium" in plain, plain

    def test_omits_effort_when_settings_has_no_effort_level(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._write_settings(state_dir, None)

        result = _run(
            {"session_id": "s-no-eff", "model": {"display_name": "opus-4-8"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "model=opus-4-8" in plain, plain
        # No effort resolved → no suffix, no dangling separator after the model.
        assert "opus-4-8 ·" not in plain, plain

    def test_omits_effort_when_settings_file_absent(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # No settings.json planted in state_dir → effortLevel unreadable.

        result = _run(
            {"session_id": "s-missing-cfg", "model": {"display_name": "opus-4-8"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "model=opus-4-8" in plain, plain
        assert "opus-4-8 ·" not in plain, plain

    def test_stdin_effort_field_takes_precedence_over_settings(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._write_settings(state_dir, "low")

        result = _run(
            {"session_id": "s-stdin-eff", "model": {"display_name": "opus-4-8"}, "effort": "max"},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "model=opus-4-8 · max" in plain, plain
        assert "· low" not in plain, plain

    def test_effort_segment_omitted_when_model_absent(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._write_settings(state_dir, "high")

        result = _run(
            {"session_id": "s-no-model"},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        # No model → no model segment → no orphan effort token.
        assert "model=" not in plain, plain
        assert "high" not in plain, plain

    def test_effort_object_payload_renders_bare_level(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _run(
            {
                "session_id": "s-obj-eff",
                "model": {"display_name": "Opus 4.8"},
                "effort": {"level": "xhigh"},
            },
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "model=Opus 4.8 · xhigh" in plain, plain
        assert "{" not in plain, plain

    def test_model_effort_object_payload_renders_bare_level(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _run(
            {
                "session_id": "s-model-obj-eff",
                "model": {"display_name": "Opus 4.8", "effort": {"level": "high"}},
            },
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "model=Opus 4.8 · high" in plain, plain
        assert "{" not in plain, plain


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


class TestHarnessTodoSummary:
    """The session's harness TODO chip is counted from the harness's OWN task store.

    The chip is sourced directly from ``$CLAUDE_TASKS_DIR/<session>/*.json`` —
    one ``<n>.json`` per todo with a ``status`` field, the harness's own
    storage. Teatree keeps NO mirror of it (the old ``<session>.todos``
    materialiser was removed); the chip and the PreCompact snapshot's
    ``read_harness_todos`` read the same store. ``test_chip_sourced_from_*``
    PINS that wiring so removing the source can never silently kill the chip
    again (the regression this class now guards).
    """

    def _seed_store(self, state_dir: Path, session_id: str, todos: list[tuple[str, str]]) -> None:
        """Write one ``<n>.json`` per harness todo as ``(status, subject)``.

        Under the isolated harness task-store root (``_harness_tasks_dir``) the
        statusline chip counts from — the same dir ``_run`` points
        ``CLAUDE_TASKS_DIR`` at.
        """
        session_dir = _harness_tasks_dir(state_dir) / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        for index, (status, subject) in enumerate(todos, start=1):
            (session_dir / f"{index}.json").write_text(
                json.dumps({"id": str(index), "subject": subject, "status": status}),
                encoding="utf-8",
            )

    def test_chip_sourced_from_harness_store_not_teatree_mirror(self, tmp_path: Path) -> None:
        # The wiring pin: a present harness store renders the chip, and a stale
        # teatree-mirror file (the REMOVED mechanism) is NOT read. If the chip
        # ever silently goes dead because its source was removed, this RED.
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._seed_store(
            state_dir,
            "s-src",
            [("completed", "Wire the parser"), ("in_progress", "Render the summary")],
        )
        # A leftover teatree mirror with DIFFERENT counts must NOT be the source.
        (state_dir / "s-src.todos").write_text(
            "- [pending] STALE mirror a\n- [pending] STALE mirror b\n- [pending] STALE mirror c\n",
            encoding="utf-8",
        )

        result = _run(
            {"session_id": "s-src", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        # The harness store's counts win (1/2), never the stale mirror's (0/3).
        assert "TODO 1/2 ✓" in plain, plain
        # Scoped to the TODO chip: a bare "0/3" also matches the RAM chip on a host
        # whose reading renders as e.g. "26.0/30G".
        assert "TODO 0/3" not in plain, plain

    def test_renders_compact_done_over_total(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._seed_store(
            state_dir,
            "s-todo",
            [
                ("completed", "Wire the parser"),
                ("completed", "Add the validator"),
                ("in_progress", "Render the summary"),
                ("pending", "Write the tests"),
            ],
        )

        result = _run(
            {"session_id": "s-todo", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "TODO 2/4 ✓" in plain, plain
        assert "1▸" in plain, plain
        # No item content ever reaches the statusline.
        assert "Wire the parser" not in plain, plain
        assert "Render the summary" not in plain, plain

    def test_omits_in_progress_marker_when_none_active(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._seed_store(
            state_dir,
            "s-no-wip",
            [("completed", "Done one"), ("pending", "Not started"), ("pending", "Also pending")],
        )

        result = _run(
            {"session_id": "s-no-wip", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "TODO 1/3 ✓" in plain, plain
        assert "▸" not in plain, plain

    def test_all_complete_renders_full_count(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._seed_store(
            state_dir,
            "s-all-done",
            [("completed", "First"), ("completed", "Second")],
        )

        result = _run(
            {"session_id": "s-all-done", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "TODO 2/2 ✓" in plain, plain
        assert "▸" not in plain, plain

    def test_no_todo_segment_when_store_absent(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _run(
            {"session_id": "s-none", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        assert "TODO" not in _strip_ansi(result.stdout)

    def test_no_todo_segment_when_store_empty(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (_harness_tasks_dir(state_dir) / "s-empty").mkdir(parents=True)

        result = _run(
            {"session_id": "s-empty", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        assert "TODO" not in _strip_ansi(result.stdout)

    def test_many_items_stay_a_single_short_segment(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        todos = [("completed", f"item {i}") for i in range(40)]
        todos += [("in_progress", f"item {i}") for i in range(40, 45)]
        todos += [("pending", f"item {i}") for i in range(45, 60)]
        self._seed_store(state_dir, "s-many", todos)

        result = _run(
            {"session_id": "s-many", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "TODO 40/60 ✓" in plain, plain
        assert "5▸" in plain, plain
        # Bounded width: the TODO summary occupies one short token, not 60 lines.
        todo_segment = plain[plain.index("TODO") :].split("│")[0]
        assert len(todo_segment) < 30, todo_segment
        assert "item 0" not in plain, plain

    def test_no_todo_segment_without_session_id(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # A store under the empty session id must never be picked up.
        self._seed_store(state_dir, "", [("pending", "rogue")])

        result = _run(
            {"model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        assert "TODO" not in _strip_ansi(result.stdout)


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


class TestCpuSegment:
    """CPU load indicator in the resource group, normalized by core count.

    The 1-minute load average is read cheaply (a single non-delayed read) and
    divided by the core count so it reads as a percentage comparable to the RAM
    and disk indicators, colored by the same green/yellow/red thresholds.
    """

    def test_renders_cpu_segment_in_resource_group(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        loadavg = tmp_path / "loadavg"
        loadavg.write_text("4.00 3.10 2.50 1/420 99\n", encoding="utf-8")

        result = _run(
            {"session_id": "s-cpu", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            cpu=(loadavg, 8),
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        # 4.00 / 8 cores = 50%.
        assert "cpu=50%" in plain, plain
        # The CPU indicator sits in the resource group alongside ram/disk.
        assert plain.index("cpu=") > plain.index("ram="), plain

    def test_cpu_segment_colors_red_when_overloaded(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        loadavg = tmp_path / "loadavg"
        loadavg.write_text("16.00 12.00 9.00\n", encoding="utf-8")

        result = _run(
            {"session_id": "s-cpu-hot", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            cpu=(loadavg, 8),
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        # 16.00 / 8 = 200% → over the red threshold.
        assert "cpu=200%" in plain, plain
        assert "\033[1;31m" in result.stdout, "expected red SGR for an overloaded CPU"

    def test_cpu_segment_omitted_when_source_unavailable(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        missing = tmp_path / "no-such-loadavg"

        result = _run(
            {"session_id": "s-no-cpu", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            cpu=(missing, 8),
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "cpu=" not in plain, plain
        # The rest of the statusline still renders.
        assert "model=Claude Opus" in plain, plain

    def test_cpu_segment_omitted_when_loadavg_empty(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        loadavg = tmp_path / "loadavg"
        loadavg.write_text("\n", encoding="utf-8")

        result = _run(
            {"session_id": "s-empty-cpu", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            cpu=(loadavg, 8),
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "cpu=" not in plain, plain
        assert "model=Claude Opus" in plain, plain


class TestLoopOwnerBadge:
    """Per-session t3-master badge in the g_context header group.

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
        """Same session owns the loop → green ``t3-master: you ✓``."""
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
        assert "t3-master: you ✓" in plain, plain
        # The badge belongs on the loop-specific line, NOT the context header.
        header = plain.splitlines()[0]
        assert "t3-master:" not in header, header
        # Green SGR present in the raw (non-stripped) output.
        assert "\033[1;32m" in result.stdout, "expected green SGR for owner=you"

    def test_badge_renders_on_loop_line_not_header(self, tmp_path: Path) -> None:
        """The per-session t3-master badge sits on the loop line region, not g_context."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        registry_dir = tmp_path / "registry"
        self._write_registry(registry_dir, session_id="sess-loop", pid=7)
        statusline_file = tmp_path / "statusline.txt"
        statusline_file.write_text("tick 5m\n", encoding="utf-8")

        result = _run(
            {"session_id": "sess-loop", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            statusline_file=statusline_file,
            registry_dir=registry_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        lines = plain.splitlines()
        # The header (line 1) carries model/ctx but not the t3-master badge.
        assert "model=Claude Opus" in lines[0], lines[0]
        assert "t3-master:" not in lines[0], lines[0]
        # The badge rides the loop line, ahead of the tick chunk.
        loop_line = next(line for line in lines if "tick 5m" in line)
        assert "t3-master: you ✓" in loop_line, loop_line
        assert loop_line.index("t3-master:") < loop_line.index("tick 5m"), loop_line

    def test_badge_rides_colorized_production_loop_line(self, tmp_path: Path) -> None:
        r"""The badge must ride the loop line even when it is ANSI-colorized.

        ``loop.statusline.render`` wraps each anchor as
        ``\033[38;5;244m{text}\033[0m`` when ``colorize`` is on (the
        production default), so the real zones-file loop line starts with the
        CSI escape, not its first letter. The matcher must tolerate that
        prefix and keep the badge on the same visible line, ahead of the tick
        chunk — a separate trailing badge line means loop state lost its
        single home.
        """
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        registry_dir = tmp_path / "registry"
        self._write_registry(registry_dir, session_id="sess-color", pid=9)
        statusline_file = tmp_path / "statusline.txt"
        statusline_file.write_text("\033[38;5;244mtick 5m\033[0m\n", encoding="utf-8")

        result = _run(
            {"session_id": "sess-color", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            statusline_file=statusline_file,
            registry_dir=registry_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        lines = plain.splitlines()
        loop_line = next(line for line in lines if "tick 5m" in line)
        assert "t3-master: you ✓" in loop_line, loop_line
        # The badge leads the loop line, ahead of the tick chunk.
        assert loop_line.index("t3-master:") < loop_line.index("tick 5m"), loop_line
        # The badge shares the loop line — never spilled onto its own trailing line.
        assert sum(1 for line in lines if "t3-master:" in line) == 1, plain
        badge_line = next(line for line in lines if "t3-master:" in line)
        assert "tick 5m" in badge_line, plain

    def test_badge_leads_loop_line_with_multiple_chunks(self, tmp_path: Path) -> None:
        """The badge is the very first token of the loop line, before every chunk."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        registry_dir = tmp_path / "registry"
        self._write_registry(registry_dir, session_id="sess-multi", pid=11)
        statusline_file = tmp_path / "statusline.txt"
        statusline_file.write_text("tick 5m · dispatch 2m · tickets 4m\n", encoding="utf-8")

        result = _run(
            {"session_id": "sess-multi", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            statusline_file=statusline_file,
            registry_dir=registry_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        loop_line = next(line for line in plain.splitlines() if "tick 5m" in line)
        # ``t3-master:`` is first; every loop chunk follows it.
        assert loop_line.lstrip().startswith("t3-master:"), loop_line
        assert loop_line.index("t3-master:") < loop_line.index("tick 5m"), loop_line
        assert loop_line.index("tick 5m") < loop_line.index("dispatch 2m"), loop_line

    def test_badge_not_glued_to_overlay_anchor_when_no_loop_line(self, tmp_path: Path) -> None:
        """No live loop line → first line is an overlay anchor; badge stays standalone."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        registry_dir = tmp_path / "registry"
        self._write_registry(registry_dir, session_id="sess-anchor", pid=13)
        statusline_file = tmp_path / "statusline.txt"
        statusline_file.write_text("[acme] coded: #42 (topic)\n", encoding="utf-8")

        result = _run(
            {"session_id": "sess-anchor", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            statusline_file=statusline_file,
            registry_dir=registry_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        # The overlay anchor must NOT have the badge prepended to it.
        anchor_line = next(line for line in plain.splitlines() if "[acme]" in line)
        assert "t3-master:" not in anchor_line, anchor_line
        # The badge surfaces on its own line so ownership context is never lost.
        badge_line = next(line for line in plain.splitlines() if "t3-master:" in line)
        assert "[acme]" not in badge_line, badge_line

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
        assert "t3-master:" in plain, plain
        # Yellow SGR present (neutral — a foreign owner is normal from a non-owner terminal).
        assert "\033[1;33m" in result.stdout, "expected yellow SGR for foreign owner"

    def test_unclaimed_badge_when_registry_has_no_owner(self, tmp_path: Path) -> None:
        """Readable registry but no owner key → dim ``t3-master: unclaimed``."""
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
        assert "t3-master: unclaimed" in plain, plain

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
        assert "t3-master" not in plain, plain

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
        assert "t3-master" not in plain, plain


class TestTeamRoster:
    """The Agent-Teams ``mates:`` zone in the header.

    The statusline reads the harness team config at display time and lists the
    ACTIVE mates of the team THIS session leads (``leadSessionId`` == ours),
    excluding the lead itself. It fails open — renders nothing, never errors —
    whenever there is no session id, no teams dir, no team this session leads,
    or any read/parse failure, so a colleague who never runs a team sees the
    statusline they always did.
    """

    def _write_team(
        self,
        state_dir: Path,
        *,
        team: str,
        lead_session_id: str,
        members: list[dict],
        lead_agent_id: str = "team-lead",
    ) -> None:
        # The mates zone resolves its teams dir from CLAUDE_CONFIG_DIR, which
        # ``_run`` pins to ``state_dir`` — so the config lives at
        # ``state_dir/teams/<team>/config.json``, the real default path.
        team_path = state_dir / "teams" / team
        team_path.mkdir(parents=True, exist_ok=True)
        (team_path / "config.json").write_text(
            json.dumps(
                {
                    "name": team,
                    "leadAgentId": lead_agent_id,
                    "leadSessionId": lead_session_id,
                    "members": members,
                }
            ),
            encoding="utf-8",
        )

    def test_lists_active_mates_for_lead_session(self, tmp_path: Path) -> None:
        """Lead session → ``mates: alice · bob``; lead + inactive members excluded."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._write_team(
            state_dir,
            team="my-team",
            lead_session_id="lead-sess",
            lead_agent_id="team-lead@my-team",
            members=[
                {"agentId": "team-lead@my-team", "name": "team-lead", "isActive": None},
                {"agentId": "alice@my-team", "name": "alice", "color": "blue", "isActive": True},
                {"agentId": "bob@my-team", "name": "bob", "color": "magenta", "isActive": True},
                {"agentId": "carol@my-team", "name": "carol", "color": "green", "isActive": False},
            ],
        )

        result = _run(
            {"session_id": "lead-sess", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        header = plain.splitlines()[0]
        assert "mates: alice · bob" in header, header
        # The lead never lists itself, and an inactive member is omitted.
        assert "team-lead" not in header, header
        assert "carol" not in header, header

    def test_mate_painted_in_its_color(self, tmp_path: Path) -> None:
        """A mate's ``color`` drives its SGR — blue for alice."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._write_team(
            state_dir,
            team="t",
            lead_session_id="ls",
            lead_agent_id="lead@t",
            members=[
                {"agentId": "lead@t", "name": "lead", "isActive": None},
                {"agentId": "alice@t", "name": "alice", "color": "blue", "isActive": True},
            ],
        )

        result = _run(
            {"session_id": "ls", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        # Blue SGR present in the raw (non-stripped) output for the mate chip.
        assert "\033[1;34m" in result.stdout, "expected blue SGR for color=blue mate"

    def test_no_zone_for_non_lead_session(self, tmp_path: Path) -> None:
        """A session that leads NO team renders no ``mates:`` zone (fail-open)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._write_team(
            state_dir,
            team="other-team",
            lead_session_id="someone-else",
            members=[
                {"agentId": "alice", "name": "alice", "color": "blue", "isActive": True},
            ],
        )

        result = _run(
            {"session_id": "not-the-lead", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "mates:" not in plain, plain

    def test_no_zone_when_teams_dir_absent(self, tmp_path: Path) -> None:
        """No teams dir at all → no ``mates:`` zone, clean exit (fail-open)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # No team config planted — state_dir/teams does not exist.

        result = _run(
            {"session_id": "any-sess", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "mates:" not in plain, plain

    def test_no_zone_when_no_active_mates(self, tmp_path: Path) -> None:
        """Lead session whose only members are the lead / inactive → no zone."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._write_team(
            state_dir,
            team="lonely",
            lead_session_id="solo-sess",
            lead_agent_id="lead@lonely",
            members=[
                {"agentId": "lead@lonely", "name": "lead", "isActive": None},
                {"agentId": "gone@lonely", "name": "gone", "color": "red", "isActive": False},
            ],
        )

        result = _run(
            {"session_id": "solo-sess", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "mates:" not in plain, plain

    def test_no_zone_when_no_session_id(self, tmp_path: Path) -> None:
        """No session_id → cannot resolve the lead's team → no zone."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._write_team(
            state_dir,
            team="t",
            lead_session_id="",
            members=[{"agentId": "alice", "name": "alice", "isActive": True}],
        )

        result = _run(
            {"model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "mates:" not in plain, plain

    def test_malformed_team_config_fails_open(self, tmp_path: Path) -> None:
        """A corrupt config.json → no crash, no zone (fail-open)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        team_path = state_dir / "teams" / "broken"
        team_path.mkdir(parents=True)
        (team_path / "config.json").write_text("{ not valid json", encoding="utf-8")

        result = _run(
            {"session_id": "lead-sess", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "mates:" not in plain, plain
        # The rest of the header still renders — the bad config did not blank it.
        assert "model=Claude Opus" in plain, plain


class TestStaleStatuslineBanner:
    """The render-age freshness gate (the months-long stale-info bug).

    The shell hook mirrors the cutoff arithmetic in
    ``teatree.loop.statusline_staleness`` inline. These tests pin the shell
    side to the same boundary; the Python side is pinned in
    ``tests/teatree_loop/test_statusline_staleness.py``.
    """

    def _statusline(self, tmp_path: Path, *, rendered_at: float | None, cadence: int = 720) -> Path:
        sl = tmp_path / "statusline.txt"
        sl.write_text("t3-teatree 3m · next tick 4m\n", encoding="utf-8")
        meta: dict = {"cadence": cadence, "next_epoch": int(time.time())}
        if rendered_at is not None:
            meta["rendered_at"] = int(rendered_at)
        (tmp_path / "statusline-meta.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
        return sl

    def test_frozen_render_emits_banner_first(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        sl = self._statusline(tmp_path, rendered_at=time.time() - 6 * 3600)

        result = _run(
            {"session_id": "stale-sess", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            statusline_file=sl,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "statusline STALE" in plain, plain
        # The banner leads — it appears before the frozen loop line it qualifies.
        assert plain.index("statusline STALE") < plain.index("next tick 4m"), plain

    def test_fresh_render_no_banner(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        sl = self._statusline(tmp_path, rendered_at=time.time() - 30)

        result = _run(
            {"session_id": "fresh-sess", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            statusline_file=sl,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "statusline STALE" not in plain, plain
        assert "next tick 4m" in plain, plain

    def test_missing_rendered_at_fails_open(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # Old-schema sidecar: cadence present, no rendered_at -> no banner.
        sl = self._statusline(tmp_path, rendered_at=None)

        result = _run(
            {"session_id": "noat-sess", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            statusline_file=sl,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "statusline STALE" not in plain, plain
        assert "next tick 4m" in plain, plain

    def test_short_cadence_uses_300s_floor(self, tmp_path: Path) -> None:
        """A 60s test loop must not flag stale on one skipped tick (floor wins)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # cadence 60 -> 2*60=120 < 300 floor. Age 200s is past 2*cadence but
        # within the 300s floor, so it must NOT be flagged stale.
        sl = self._statusline(tmp_path, rendered_at=time.time() - 200, cadence=60)

        result = _run(
            {"session_id": "floor-sess", "model": {"display_name": "Claude Opus"}},
            state_dir=state_dir,
            statusline_file=sl,
        )

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "statusline STALE" not in plain, plain


def _make_config_db(path: Path, *, autoload: object) -> None:
    """Build a real ``teatree_config_setting`` DB carrying a GLOBAL ``autoload`` row."""
    import sqlite3  # noqa: PLC0415

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'autoload', ?)",
            (json.dumps(autoload),),
        )
        conn.commit()
    finally:
        conn.close()


class TestStatuslineAutoloadDbFlip:
    """The statusline autoload gate reads the canonical ConfigSetting DB DB-only.

    The gate decides whether the statusline renders at all. ``autoload`` is DB-home
    (the legacy file tier is removed): the gate reads the DB (sqlite3, the cold_reader
    WAL fallback), then OFF. Exercised here WITHOUT ``T3_AUTOLOAD`` so the env
    short-circuit does not mask the DB path.
    """

    def _run_gate(self, tmp_path: Path, *, config_db: Path | None) -> subprocess.CompletedProcess:
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        session_id = "gate-sess"
        (state_dir / f"{session_id}.teatree-active").touch()
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        env = os.environ.copy()
        env.pop("T3_AUTOLOAD", None)
        env["HOME"] = str(home)
        env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(state_dir)
        env["CLAUDE_CONFIG_DIR"] = str(state_dir)
        env["CLAUDE_TASKS_DIR"] = str(state_dir / "_tasks")
        env.pop("XDG_DATA_HOME", None)
        if config_db is not None:
            env["T3_CONFIG_DB"] = str(config_db)
        else:
            env.pop("T3_CONFIG_DB", None)
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps({"session_id": session_id, "model": {"display_name": "Claude Opus"}}),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            cwd=REPO_ROOT,
        )

    def test_db_autoload_true_renders(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, autoload=True)
        result = self._run_gate(tmp_path, config_db=db)
        assert result.returncode == 0, result.stderr
        assert "model=" in _strip_ansi(result.stdout)

    def test_db_autoload_false_shows_hint_not_blank(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        _make_config_db(db, autoload=False)
        result = self._run_gate(tmp_path, config_db=db)
        assert result.returncode == 0, result.stderr
        # #3233: gated OFF emits a single how-to hint, never zero bytes.
        assert "autoload" in result.stdout
        assert "model=" not in _strip_ansi(result.stdout)

    def test_missing_db_falls_back_to_off_hint(self, tmp_path: Path) -> None:
        # No DB row -> the gate falls through to OFF: one-line hint, not blank (#3233).
        result = self._run_gate(tmp_path, config_db=tmp_path / "absent.sqlite3")
        assert result.returncode == 0, result.stderr
        assert "autoload" in result.stdout
        assert "model=" not in _strip_ansi(result.stdout)


class TestStatuslineRendersOnAutoloadWithoutMarker:
    """Render gate keys on the ``autoload`` owner flag alone, not the marker.

    The per-session ``.teatree-active`` marker is NOT required (souliane/teatree
    render-gate fix). Root cause of the reported blank statusline: the gate ANDed the
    per-session ``.teatree-active`` marker WITH ``autoload``. That marker is written
    by SessionStart-engage / a teatree-skill load, but the harness runs the loop in
    a background ``bg-spare`` daemon session (which gets the marker and owns the
    tick) while the owner's foreground TUI sessions frequently never get it — so the
    statusline blanked in exactly the sessions the owner looks at, despite a global
    ``autoload = true``. ``autoload`` is the ONE owner flag that "engages the
    session", so it alone gates whether the statusline renders. Loop *arming* keeps
    its stricter ``marker AND autoload`` gate (``_loop_auto_load_active``); this is
    display *visibility*, which the owner wants in every one of their sessions. The
    #256 colleague guarantee is preserved: ``autoload`` off shows only a one-line
    how-to hint (#3233), never the loop statusline, regardless of the marker.
    """

    def _run(self, tmp_path: Path, *, autoload: bool, marker: bool) -> subprocess.CompletedProcess:
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        session_id = "owner-sess"
        if marker:
            (state_dir / f"{session_id}.teatree-active").touch()
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        env = os.environ.copy()
        # env autoload short-circuits the DB read, keeping the case hermetic.
        env["T3_AUTOLOAD"] = "1" if autoload else "0"
        env["HOME"] = str(home)
        env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(state_dir)
        env["CLAUDE_CONFIG_DIR"] = str(state_dir)
        env["CLAUDE_TASKS_DIR"] = str(state_dir / "_tasks")
        env.pop("XDG_DATA_HOME", None)
        env.pop("T3_CONFIG_DB", None)
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps({"session_id": session_id, "model": {"display_name": "Claude Opus"}}),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            cwd=REPO_ROOT,
        )

    def test_autoload_on_without_marker_renders(self, tmp_path: Path) -> None:
        # THE regression: the owner enabled autoload but this foreground session
        # never got the .teatree-active marker -> must STILL render (was blank).
        result = self._run(tmp_path, autoload=True, marker=False)
        assert result.returncode == 0, result.stderr
        assert "model=" in _strip_ansi(result.stdout)

    def test_autoload_on_with_marker_renders(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, autoload=True, marker=True)
        assert result.returncode == 0, result.stderr
        assert "model=" in _strip_ansi(result.stdout)

    def test_autoload_off_without_marker_shows_hint(self, tmp_path: Path) -> None:
        # Colleague who merely cloned the repo: autoload off, no marker -> the loop
        # statusline stays suppressed (#256), but a one-line how-to hint shows
        # instead of a blank bar (#3233).
        result = self._run(tmp_path, autoload=False, marker=False)
        assert result.returncode == 0, result.stderr
        assert "autoload" in result.stdout
        assert "model=" not in _strip_ansi(result.stdout)

    def test_autoload_off_with_marker_shows_hint(self, tmp_path: Path) -> None:
        # #256: a colleague who even loaded a teatree skill (marker present) but did
        # NOT enable autoload is still not shown the loop statusline — autoload is the
        # authoritative owner opt-in. Only the one-line hint shows (#3233).
        result = self._run(tmp_path, autoload=False, marker=True)
        assert result.returncode == 0, result.stderr
        assert "autoload" in result.stdout
        assert "model=" not in _strip_ansi(result.stdout)


# A dangling escape is an ESC byte that does NOT open a recognised, complete
# sequence — a CSI (``\x1b[…<final>``) or an OSC 8 wrapper (``\x1b]8;…\x1b\\``).
# The width cap must never cut a line in the middle of an escape, so a capped
# line's raw bytes must contain no such fragment.
_DANGLING_ESC_RE = re.compile(r"\x1b(?![\[\]])|\x1b\[[0-9;?]*(?![0-9;?@-~])|\x1b\][^\x1b\x07]*\Z")


class TestWidthCap:
    """Every emitted line is capped to the visible terminal width (souliane/teatree).

    Claude Code's statusline docs warn that long, multi-line ANSI output gets
    truncated / wraps / renders blank. ``statusline.sh`` therefore bounds every
    emitted line — header, zones, badge, and chain-script output — to the
    visible terminal width at one choke point. The cap is ANSI-aware: it counts
    only visible characters (ignoring SGR and OSC 8 sequences), never splits an
    escape, terminates a truncated line with a reset so colour never bleeds, and
    marks the cut with a single ``…``. Lines already within width pass through
    byte-for-byte. The width comes from ``COLUMNS`` when set and > 0.
    """

    def _run(
        self,
        tmp_path: Path,
        *,
        columns: int,
        zones: str,
        session_id: str = "cap-sess",
    ) -> subprocess.CompletedProcess:
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / f"{session_id}.teatree-active").touch()
        sl = tmp_path / "statusline.txt"
        sl.write_text(zones, encoding="utf-8")
        env = os.environ.copy()
        env["T3_AUTOLOAD"] = "1"
        env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(state_dir)
        env["CLAUDE_CONFIG_DIR"] = str(state_dir)
        env["CLAUDE_TASKS_DIR"] = str(_harness_tasks_dir(state_dir))
        env["TEATREE_STATUSLINE_FILE"] = str(sl)
        env["COLUMNS"] = str(columns)
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps({"session_id": session_id, "model": {"display_name": "Claude Opus"}}),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            cwd=REPO_ROOT,
        )

    def test_long_loop_line_truncated_to_visible_width(self, tmp_path: Path) -> None:
        # The ~900-char per-loop ANSI line that renders blank on some Claude Code
        # surfaces. Each loop name is individually SGR-coloured, so the raw line
        # is dense with escapes while its VISIBLE width is what must be bounded.
        loops = " · ".join(f"\033[1;32mloop{i:02d} {i}m\033[0m" for i in range(90))
        assert len(_strip_ansi(loops)) > 900, "fixture must exceed 900 visible chars"

        result = self._run(tmp_path, columns=120, zones=loops + "\n")

        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        # EVERY emitted line — header included — is within the width cap.
        for line in lines:
            assert len(_strip_ansi(line)) <= 120, (len(_strip_ansi(line)), line)
        # The header still renders (the fix must not drop it).
        assert any("model=Claude Opus" in _strip_ansi(line) for line in lines)
        # The loop line was truncated: it ends with an ellipsis marker + reset so
        # colour never bleeds past the cut.
        loop_line = next(line for line in lines if "loop00" in _strip_ansi(line))
        # The contract is "no line exceeds the width", NOT "every line is exactly
        # the width": an ANSI-aware cut lands on a character boundary at or before
        # the cap, and an awk that measures bytes rather than characters over the
        # multibyte ``·`` separators lands a little short — both ≤ cap. Assert
        # bounded-and-meaningfully-truncated, never an exact fill.
        loop_vis = len(_strip_ansi(loop_line))
        assert 60 < loop_vis <= 120, loop_vis
        assert loop_line.endswith("…\033[0m"), repr(loop_line)

    def test_embedded_sgr_never_split_mid_escape(self, tmp_path: Path) -> None:
        # The cut lands exactly where an embedded SGR sequence sits: the escape
        # must be emitted whole (or wholly dropped), never bisected.
        line = "x" * 49 + "\033[1;31m" + "y" * 100
        result = self._run(tmp_path, columns=50, zones=line + "\n")

        assert result.returncode == 0, result.stderr
        capped = next(ln for ln in result.stdout.splitlines() if ln.startswith("x"))
        # Visible width honoured (49 x's + the ellipsis marker == 50).
        assert len(_strip_ansi(capped)) == 50, capped
        # The boundary SGR is present intact, and no dangling escape fragment
        # survives anywhere on the line.
        assert "\033[1;31m" in capped, repr(capped)
        assert _DANGLING_ESC_RE.search(capped) is None, repr(capped)
        # Colour is closed off with a trailing reset.
        assert capped.endswith("…\033[0m"), repr(capped)

    def test_short_colored_line_passes_through_byte_for_byte(self, tmp_path: Path) -> None:
        colored = "\033[1;32mtick 5m\033[0m"
        result = self._run(tmp_path, columns=200, zones=colored + "\n")

        assert result.returncode == 0, result.stderr
        # A line within width is emitted unchanged, escapes and all.
        assert colored in result.stdout, repr(result.stdout)

    def test_multiline_input_stays_multiline(self, tmp_path: Path) -> None:
        rows = ["\033[38;5;244m[acme] row one\033[0m", "[acme] row two", "→ statusline: three"]
        result = self._run(tmp_path, columns=200, zones="\n".join(rows) + "\n")

        assert result.returncode == 0, result.stderr
        out_lines = result.stdout.splitlines()
        # Each source row stays on its own row (none merged, none dropped).
        for raw in rows:
            visible = _strip_ansi(raw)
            matches = [ln for ln in out_lines if _strip_ansi(ln) == visible]
            assert len(matches) == 1, (visible, out_lines)

    def test_osc8_hyperlink_within_width_preserved_intact(self, tmp_path: Path) -> None:
        # OSC 8 hyperlink: ``\x1b]8;;<uri>\x1b\\<text>\x1b]8;;\x1b\\``. Its visible
        # width is only the link text, so a short linked row passes through with
        # the hyperlink wrapper byte-for-byte intact.
        osc8 = "\033]8;;https://example.com/ticket/42\033\\ticket #42\033]8;;\033\\"
        assert len(_strip_ansi(osc8)) < 20
        result = self._run(tmp_path, columns=200, zones=osc8 + "\n")

        assert result.returncode == 0, result.stderr
        assert osc8 in result.stdout, repr(result.stdout)


class TestAutoloadOffHint:
    """Autoload-off gate emits a one-line hint, never a blank bar (#3233).

    When the opt-in (#256) gate suppresses the statusline under a non-TTY CC
    invocation, it must emit a one-line hint — never zero bytes, which CC
    discards, leaving a mysteriously blank bar.
    """

    def _run_gated(self, tmp_path: Path, *, session_id: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.pop("T3_AUTOLOAD", None)  # autoload OFF: no env override
        # Point the config DB at a path that does not exist so the DB read
        # resolves autoload to OFF as well (fails silent → gate fires).
        env["T3_CONFIG_DB"] = str(tmp_path / "no-such-db.sqlite3")
        env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(tmp_path / "state")
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps({"session_id": session_id, "model": {"display_name": "Claude Opus"}}),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            cwd=REPO_ROOT,
        )

    def test_autoload_off_emits_one_line_hint_not_blank(self, tmp_path: Path) -> None:
        result = self._run_gated(tmp_path, session_id="s-gated")

        assert result.returncode == 0, result.stderr
        # Never zero bytes — CC discards empty output and the bar goes blank.
        assert result.stdout.strip() != "", "autoload-off must not emit an empty statusline"
        # A single actionable line naming the enable knob.
        assert len(result.stdout.splitlines()) == 1, repr(result.stdout)
        assert "autoload" in result.stdout

    def test_no_session_id_still_renders_full_bar(self, tmp_path: Path) -> None:
        # TTY-style / no-session invocation is not gated: the hint must NOT
        # replace the normal (multi-line) render.
        result = self._run_gated(tmp_path, session_id="")
        assert result.returncode == 0, result.stderr
        assert "autoload" not in result.stdout


class TestMultibyteAwkSafety:
    """The width-capping / badge awk must not blank the bar on multibyte input (#3286).

    macOS's ``/usr/bin/awk`` (onetrueawk) aborts with ``towc: multibyte
    conversion failure`` on multibyte characters (the ``·``/``│`` separators,
    the ``⚠``/``—`` stale banner, the ``…`` ellipsis). When that awk aborts the
    whole ``_cap_line_widths`` pipeline emits nothing, so the entire statusline
    disappears. The fix runs those awk invocations under ``LC_ALL=C`` (byte
    mode), where the awk never calls ``towc``. This test reproduces the crash on
    Linux by shadowing ``awk`` with a wrapper that mimics onetrueawk: it aborts
    on multibyte input unless invoked in the ``C`` locale.
    """

    _FAKE_AWK = (
        "#!/usr/bin/env bash\n"
        "# Simulates macOS onetrueawk's towc multibyte crash (#3286).\n"
        "data=$(cat)\n"
        'if [ "${LC_ALL:-}" != "C" ] && '
        '[ -n "$(printf %s "$data" | LC_ALL=C tr -d \'\\000-\\177\')" ]; then\n'
        "    printf 'awk: towc: multibyte conversion failure\\n' >&2\n"
        "    exit 2\n"
        "fi\n"
        'printf %s "$data" | "$REAL_AWK" "$@"\n'
    )

    def _run_with_fake_awk(self, tmp_path: Path, payload: dict) -> subprocess.CompletedProcess:
        awk_path = shutil.which("awk") or "/usr/bin/awk"
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        fake_awk = fake_bin / "awk"
        fake_awk.write_text(self._FAKE_AWK, encoding="utf-8")
        fake_awk.chmod(0o755)

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        session_id = payload["session_id"]
        (state_dir / f"{session_id}.teatree-active").touch()
        env = os.environ.copy()
        env["T3_AUTOLOAD"] = "1"
        env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(state_dir)
        env["CLAUDE_CONFIG_DIR"] = str(state_dir)
        env["CLAUDE_TASKS_DIR"] = str(_harness_tasks_dir(state_dir))
        env["REAL_AWK"] = awk_path
        env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            cwd=REPO_ROOT,
        )

    def test_multibyte_separators_do_not_blank_the_bar(self, tmp_path: Path) -> None:
        # model + rate limits render the multibyte group (``│``) and inline
        # (``·``) separators, which feed multibyte bytes into _cap_line_widths.
        payload = {
            "session_id": "s-mb",
            "model": {"display_name": "Claude Opus"},
            "rate_limits": {
                "five_hour": {"used_percentage": 42},
                "seven_day": {"used_percentage": 85},
            },
        }
        result = self._run_with_fake_awk(tmp_path, payload)

        assert result.returncode == 0, result.stderr
        # The crash makes the whole bar vanish; the fix keeps it rendering.
        assert result.stdout.strip() != "", "multibyte content must not blank the statusline"
        plain = _strip_ansi(result.stdout)
        assert "model=Claude Opus" in plain, plain
        assert "5h=42%" in plain, plain
        assert "7d=85%" in plain, plain

    def test_fake_awk_actually_crashes_without_lc_all_c(self, tmp_path: Path) -> None:
        # Guard the reproduction itself: the fake awk must abort on multibyte
        # input outside the C locale (so the test above is not vacuous).
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        fake_awk = fake_bin / "awk"
        fake_awk.write_text(self._FAKE_AWK, encoding="utf-8")
        fake_awk.chmod(0o755)
        env = os.environ.copy()
        env["REAL_AWK"] = "/usr/bin/awk"
        env.pop("LC_ALL", None)
        crashed = subprocess.run(
            [str(fake_awk), "{print}"],
            input="a · b\n",
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert crashed.returncode == 2, crashed
        assert "towc" in crashed.stderr
        env["LC_ALL"] = "C"
        ok = subprocess.run(
            [str(fake_awk), "{print}"],
            input="a · b\n",
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert ok.returncode == 0, ok.stderr
        assert "a · b" in ok.stdout
