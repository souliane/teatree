"""Tests for teatree opt-in: marker mechanism, gated injection points.

Covers must-fire / must-NOT-fire directions for:
1. Fresh session (no marker) -- injection points silent, loop-registration exempt.
2. Marker present -- injection points fire as before.
3. handle_track_skill_usage sets marker for t3:interactive and for skills that
    require: [interactive] (closure expansion).
"""

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _OWNER_LOOP,
    _loop_auto_load_active,
    _read_loop_registry,
    _t3_engaged,
    _teatree_active,
    _teatree_engaged,
    _write_loop_registry,
    handle_session_start_bootstrap,
    handle_track_skill_usage,
)
from hooks.scripts.teatree_settings import autoload_enabled

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router, "STATE_DIR", state)

    reg_dir = tmp_path / "data"
    reg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg_dir))

    monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))
    monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(tmp_path / "no-bash-env"))
    # A headless factory agent runs this suite with the Agent-SDK lane exported
    # (``session_lane``) — an unpinned env would decide the delivery assertions
    # below rather than the gating under test.
    monkeypatch.delenv("CLAUDE_AGENT_SDK_VERSION", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    # Hermetic HOME: ``autoload_enabled`` is DB-home (the legacy file tier is
    # removed) — it reads ``T3_AUTOLOAD`` env first, else the canonical ConfigSetting
    # sqlite. A clean home with no DB keeps the default-OFF (#256) path deterministic
    # regardless of the developer's own config.
    clean_home = tmp_path / "home"
    clean_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(clean_home))
    # ``autoload`` is the ONE owner flag — it both engages the session AND arms
    # its loops (the former separate ``[loops] auto_load`` arming flag is
    # subsumed). Model the opted-in loop OWNER for the marker-mechanism tests
    # below by setting ``T3_AUTOLOAD``; classes that exercise the default-OFF
    # engagement / arming path delete this var in their own fixtures.
    monkeypatch.setenv("T3_AUTOLOAD", "1")


def _mark_active(session_id: str) -> None:
    (router.STATE_DIR / f"{session_id}.teatree-active").touch()


def _is_marked_active(session_id: str) -> bool:
    return (router.STATE_DIR / f"{session_id}.teatree-active").is_file()


def _live_pid() -> int:
    return os.getpid()


# ── _teatree_active helper ─────────────────────────────────────────────


class TestTeatreeActiveHelper:
    def test_returns_false_when_no_marker(self) -> None:
        assert _teatree_active("fresh-session") is False

    def test_returns_false_for_empty_session_id(self) -> None:
        assert _teatree_active("") is False

    def test_returns_true_when_marker_exists(self) -> None:
        _mark_active("active-session")
        assert _teatree_active("active-session") is True

    def test_different_session_is_not_active(self) -> None:
        _mark_active("session-a")
        assert _teatree_active("session-b") is False


# ── handle_session_start_bootstrap gating ─────────────────────────────


class TestSessionStartBootstrapGating:
    def test_fresh_session_without_marker_emits_how_to_advisory(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #256 default-OFF: a fresh, not-engaged session (autoload OFF) no longer
        # returns silently — it surfaces the one-line how-to-start advisory and
        # never the loop bootstrap directive.
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)
        handle_session_start_bootstrap({"session_id": "no-teatree"})
        out = capsys.readouterr().out
        assert out != ""
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "run /t3:interactive" in ctx
        assert "t3 loops tick" not in ctx

    def test_fresh_session_without_marker_does_not_claim_ownership(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)
        handle_session_start_bootstrap({"session_id": "no-teatree"})
        assert _read_loop_registry() == {}

    def test_marked_session_emits_tick_dispatch_directive(self, capsys: pytest.CaptureFixture[str]) -> None:
        _mark_active("teatree-session")
        handle_session_start_bootstrap({"session_id": "teatree-session"})
        out = capsys.readouterr().out
        assert out != ""
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "t3 loops tick" in ctx

    def test_marked_session_claims_ownership(self) -> None:
        _mark_active("teatree-session")
        handle_session_start_bootstrap({"session_id": "teatree-session"})
        owner = _read_loop_registry().get(_OWNER_LOOP)
        assert owner is not None
        assert owner["session_id"] == "teatree-session"

    def test_post_compaction_same_session_with_marker_still_fires(self, capsys: pytest.CaptureFixture[str]) -> None:
        _mark_active("compact-session")
        _write_loop_registry(
            {
                _OWNER_LOOP: {
                    "session_id": "compact-session",
                    "agent_id": "",
                    "pid": _live_pid(),
                }
            }
        )
        handle_session_start_bootstrap({"session_id": "compact-session", "source": "compact"})
        out = capsys.readouterr().out
        assert out != ""
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "t3 loops tick" in ctx


# ── handle_track_skill_usage sets marker ──────────────────────────────


class TestTrackSkillUsageSetsMarker:
    def test_teatree_skill_sets_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_resolve_skill_closure",
            lambda skills: skills,
        )
        handle_track_skill_usage(
            {
                "session_id": "track-sess",
                "tool_name": "Skill",
                "tool_input": {"skill": "t3:interactive"},
            }
        )
        assert _is_marked_active("track-sess")

    def test_bare_teatree_skill_name_sets_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_resolve_skill_closure",
            lambda skills: skills,
        )
        handle_track_skill_usage(
            {
                "session_id": "track-sess2",
                "tool_name": "Skill",
                "tool_input": {"skill": "interactive"},
            }
        )
        assert _is_marked_active("track-sess2")

    def test_non_teatree_skill_does_not_set_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_resolve_skill_closure",
            lambda skills: skills,
        )
        handle_track_skill_usage(
            {
                "session_id": "track-sess3",
                "tool_name": "Skill",
                "tool_input": {"skill": "t3:code"},
            }
        )
        assert not _is_marked_active("track-sess3")

    def test_closure_with_teatree_sets_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_resolve_skill_closure",
            lambda skills: [*list(skills), "t3:interactive"],
        )
        handle_track_skill_usage(
            {
                "session_id": "track-sess4",
                "tool_name": "Skill",
                "tool_input": {"skill": "t3:ticket"},
            }
        )
        assert _is_marked_active("track-sess4")

    def test_marker_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_resolve_skill_closure",
            lambda skills: skills,
        )
        for _ in range(3):
            handle_track_skill_usage(
                {
                    "session_id": "track-sess5",
                    "tool_name": "Skill",
                    "tool_input": {"skill": "t3:interactive"},
                }
            )
        assert _is_marked_active("track-sess5")

    def test_instructions_loaded_with_teatree_sets_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_resolve_skill_closure",
            lambda skills: skills,
        )
        handle_track_skill_usage(
            {
                "session_id": "track-sess6",
                "skills": [{"name": "t3:interactive"}],
            }
        )
        assert _is_marked_active("track-sess6")


# ── Real closure: cross-cutting skills must NOT drag in teatree (FINDING B) ──

_SKILLS_TREE = Path(__file__).resolve().parents[1] / "skills"
# Lifecycle skills shared with downstream overlays: loading one in an overlay
# session must NOT activate teatree's loop machinery.
_CROSS_CUTTING_SKILLS = [
    "code",
    "ticket",
    "review",
    "ship",
    "test",
    "followup",
    "handover",
    "next",
    "review-request",
    "wip",
]
# Genuinely teatree-specific skills keep the transitive opt-in.
_TEATREE_SPECIFIC_SKILLS = ["dogfooding"]


class TestRealClosureMarkerActivation:
    @pytest.fixture(autouse=True)
    def _real_skill_tree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(_SKILLS_TREE))

    @pytest.mark.parametrize("skill", _CROSS_CUTTING_SKILLS)
    def test_cross_cutting_skill_does_not_set_marker(self, skill: str) -> None:
        session = f"overlay-{skill}"
        handle_track_skill_usage(
            {
                "session_id": session,
                "tool_name": "Skill",
                "tool_input": {"skill": f"t3:{skill}"},
            }
        )
        assert not _is_marked_active(session)

    @pytest.mark.parametrize("skill", _TEATREE_SPECIFIC_SKILLS)
    def test_teatree_specific_skill_sets_marker(self, skill: str) -> None:
        session = f"tt-{skill}"
        handle_track_skill_usage(
            {
                "session_id": session,
                "tool_name": "Skill",
                "tool_input": {"skill": f"t3:{skill}"},
            }
        )
        assert _is_marked_active(session)

    def test_direct_teatree_skill_sets_marker(self) -> None:
        handle_track_skill_usage(
            {
                "session_id": "tt-direct",
                "tool_name": "Skill",
                "tool_input": {"skill": "t3:interactive"},
            }
        )
        assert _is_marked_active("tt-direct")


# ── engage(): the single engagement seam (redesign §8.1) ──────────────


class TestEngageIsTheSingleSeam:
    """Both engagement paths write ``.teatree-active`` through one routine.

    Pins the autonomous-lane redesign §6/§8.1 invariant: auto-loading does
    exactly what manual engagement does — a single ``engage(session)`` writer,
    not two parallel marker touches that can drift.
    """

    def test_engage_marks_active(self) -> None:
        router.engage("eng-sess")
        assert _is_marked_active("eng-sess")

    def test_engage_creates_state_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        missing = tmp_path / "not-yet"
        monkeypatch.setattr(router, "STATE_DIR", missing)
        router.engage("dir-sess")
        assert (missing / "dir-sess.teatree-active").is_file()

    def test_engage_empty_session_is_noop(self) -> None:
        router.engage("")
        assert not any(router.STATE_DIR.glob("*.teatree-active"))

    def test_engage_is_idempotent(self) -> None:
        router.engage("idem-sess")
        router.engage("idem-sess")
        assert _is_marked_active("idem-sess")

    def test_autoload_bootstrap_engages_through_the_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []
        monkeypatch.setattr(router, "engage", lambda sid, **_kw: seen.append(sid))
        handle_session_start_bootstrap({"session_id": "auto-sess"})
        assert seen == ["auto-sess"]

    def test_skill_load_engages_through_the_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "_resolve_skill_closure", lambda skills: skills)
        seen: list[str] = []
        monkeypatch.setattr(router, "engage", seen.append)
        handle_track_skill_usage(
            {
                "session_id": "skill-sess",
                "tool_name": "Skill",
                "tool_input": {"skill": "t3:interactive"},
            }
        )
        assert seen == ["skill-sess"]


# ── #256: session-start auto-load is opt-in (default OFF, colleague-friendly) ──


class TestLoopAutoLoadOptInGate:
    """A teatree-marked session that did NOT enable autoload is silent (#256).

    Symmetric must-fire/must-NOT-fire for ``_loop_auto_load_active`` and the
    injection point it gates (the bootstrap claim). The
    marker is always present here, so the ONLY variable is the ``autoload`` opt-in —
    revert the ``_loop_auto_load_active`` gate at any call site and the matching
    ``*_silent`` assertion goes RED.
    """

    @pytest.fixture(autouse=True)
    def _marked_colleague(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mark_active("colleague")
        # Drop the file-level autoload opt-in so the default-OFF path is exercised.
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def _opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")

    # combined predicate ───────────────────────────────────────────────
    def test_active_predicate_off_without_opt_in(self) -> None:
        assert _loop_auto_load_active("colleague") is False

    def test_active_predicate_on_with_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._opt_in(monkeypatch)
        assert _loop_auto_load_active("colleague") is True

    # bootstrap (SessionStart) ─────────────────────────────────────────
    def test_bootstrap_silent_without_opt_in(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "colleague"})
        assert capsys.readouterr().out == ""

    def test_bootstrap_does_not_claim_ownership_without_opt_in(self) -> None:
        handle_session_start_bootstrap({"session_id": "colleague"})
        assert _read_loop_registry() == {}

    def test_bootstrap_fires_with_opt_in(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._opt_in(monkeypatch)
        handle_session_start_bootstrap({"session_id": "colleague"})
        out = capsys.readouterr().out
        assert "t3 loops tick" in out
        assert _read_loop_registry().get(_OWNER_LOOP, {}).get("session_id") == "colleague"


# ── Statusline shell script gating ────────────────────────────────────

_BASH = shutil.which("bash") or "/bin/bash"


def _seed_autoload_db(path: Path, *, autoload: object) -> None:
    """Build a real ``teatree_config_setting`` sqlite carrying a GLOBAL ``autoload`` row.

    Mirrors the Django-migration shape (JSON-encoded ``value``) so both the cold
    Python reader (``teatree_settings._cold_db_bool``) and the bash statusline gate
    (``statusline.sh._autoload_db_value``) resolve it — autoload is DB-home now, read
    DB-only.
    """
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


class TestStatuslineGating:
    def _run_statusline(
        self,
        session_id: str,
        state_dir: Path,
        *,
        home: Path | None = None,
        extra_env: dict | None = None,
    ) -> str:
        script = Path(__file__).resolve().parents[1] / "hooks" / "scripts" / "statusline.sh"
        payload = json.dumps({"session_id": session_id})
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home) if home is not None else os.environ.get("HOME", ""),
            "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(state_dir),
            "TEATREE_STATUSLINE_FILE": str(state_dir / "statusline.txt"),
        }
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            [_BASH, str(script)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        return result.stdout

    def test_no_marker_but_autoload_on_produces_output(self, tmp_path: Path) -> None:
        # The render gate keys on the ``autoload`` owner flag ALONE, not the
        # per-session ``.teatree-active`` marker: the owner's foreground TUI
        # sessions frequently never get that marker (the harness's background
        # bg-spare daemon owns the tick and holds it), so requiring it blanked the
        # statusline in exactly the sessions the owner looks at. ``autoload`` on ->
        # render even without the marker. (Loop *arming* keeps its stricter
        # marker AND autoload gate; this is display *visibility*.)
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        out = self._run_statusline("no-teatree-sess", state_dir, extra_env={"T3_AUTOLOAD": "1"})
        assert out != ""

    def test_t3_engaged_marker_with_autoload_off_renders_full_bar(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "teatree-sess.t3-engaged").touch()
        (state_dir / "statusline.txt").write_text("full-bar-marker\n", encoding="utf-8")
        out = self._run_statusline("teatree-sess", state_dir, home=tmp_path / "fresh-home")
        assert "full-bar-marker" in out
        assert "autoload" not in out

    def test_marker_and_env_opt_in_produces_output(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "teatree-sess.teatree-active").touch()
        out = self._run_statusline("teatree-sess", state_dir, extra_env={"T3_AUTOLOAD": "1"})
        assert out != ""

    def test_marker_and_db_opt_in_produces_output(self, tmp_path: Path) -> None:
        # ``autoload`` is DB-home now: the statusline gate reads the canonical
        # ConfigSetting sqlite (via T3_CONFIG_DB).
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "teatree-sess.teatree-active").touch()
        db = tmp_path / "db.sqlite3"
        _seed_autoload_db(db, autoload=True)
        out = self._run_statusline("teatree-sess", state_dir, extra_env={"T3_CONFIG_DB": str(db)})
        assert out != ""


# ── #256: default-off teatree autoload + engagement seam ──────────────────


class TestAutoloadEnabledHelper:
    """``autoload_enabled`` — env-first, then the DB-home ``autoload`` ConfigSetting, default OFF, fail-closed.

    ``autoload`` is DB-home, read DB-only via the Django-free ``_cold_db_bool`` (the
    legacy file tier is removed). ``T3_AUTOLOAD`` env still wins.
    """

    @pytest.fixture(autouse=True)
    def _no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def test_default_off_with_no_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "no-config-home"))
        assert autoload_enabled() is False

    def test_env_truthy_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        assert autoload_enabled() is True

    def test_env_falsey_disables_over_db_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Env wins over the stored row: ``T3_AUTOLOAD=false`` disables even when a
        # GLOBAL ``autoload`` row is true.
        db = tmp_path / "db.sqlite3"
        _seed_autoload_db(db, autoload=True)
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        monkeypatch.setenv("T3_AUTOLOAD", "false")
        assert autoload_enabled() is False

    def test_db_true_enables(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # DB-home: a stored GLOBAL ``autoload`` row engages autoload.
        db = tmp_path / "db.sqlite3"
        _seed_autoload_db(db, autoload=True)
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        assert autoload_enabled() is True

    def test_broken_db_fails_closed_off(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A corrupt/unreadable DB fails CLOSED (OFF) — it never raises.
        garbage = tmp_path / "corrupt.sqlite3"
        garbage.write_bytes(b"this is not a sqlite database at all")
        monkeypatch.setenv("T3_CONFIG_DB", str(garbage))
        assert autoload_enabled() is False

    def test_non_bool_db_value_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Strict bool (DB-home): a stored JSON string ``"true"`` is not a real bool,
        # so it must not enable autoload — it falls through to the default (OFF).
        db = tmp_path / "db.sqlite3"
        _seed_autoload_db(db, autoload="true")
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        assert autoload_enabled() is False


class TestTeatreeEngagedSeam:
    """``_teatree_engaged`` = autoload OR ``.teatree-active`` OR ``.t3-engaged`` (#256)."""

    @pytest.fixture(autouse=True)
    def _no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The file fixture models the opted-in owner via T3_AUTOLOAD; drop it so
        # the default-OFF engagement seam is exercised here.
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def test_not_engaged_by_default(self) -> None:
        assert _teatree_engaged("fresh") is False

    def test_engaged_via_autoload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        assert _teatree_engaged("fresh") is True

    def test_engaged_via_teatree_active_marker(self) -> None:
        _mark_active("tt-eng")
        assert _teatree_engaged("tt-eng") is True

    def test_engaged_via_t3_engaged_marker(self) -> None:
        (router.STATE_DIR / "marker-sess.t3-engaged").touch()
        assert _t3_engaged("marker-sess") is True
        assert _teatree_engaged("marker-sess") is True

    def test_empty_session_id_is_not_engaged(self) -> None:
        assert _t3_engaged("") is False
        assert _teatree_engaged("") is False


class TestAutoloadSessionStart:
    """#256 (a): autoload ON flips ``.teatree-active`` + fires the loop bootstrap, not the how-to."""

    @pytest.fixture(autouse=True)
    def _no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Drop the file fixture's autoload opt-in; the on-tests re-enable it.
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def test_autoload_on_touches_teatree_active_and_emits_tick_directive(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # autoload is the single owner flag: it flips ``.teatree-active`` AND arms
        # the loop bootstrap, so the existing loop bootstrap fires unchanged.
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        handle_session_start_bootstrap({"session_id": "owner-default"})
        assert _is_marked_active("owner-default")
        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "t3 loops tick" in ctx
        assert "run /t3:interactive" not in ctx

    def test_autoload_on_claims_ownership(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        handle_session_start_bootstrap({"session_id": "owner-default"})
        assert _read_loop_registry().get(_OWNER_LOOP, {}).get("session_id") == "owner-default"

    def test_default_off_emits_how_to_and_does_not_claim(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "off-sess"})
        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "run /t3:interactive" in ctx
        # autoload is DB-home, so the auto-start how-to points at the config_setting
        # store.
        assert "config_setting set autoload true" in ctx
        assert "t3 loops tick" not in ctx
        assert _read_loop_registry() == {}

    def test_compact_resume_default_off_skips_how_to(self, capsys: pytest.CaptureFixture[str]) -> None:
        # On a compact/resume of a not-engaged session the how-to is suppressed,
        # but the merge still runs so snapshot-recovery / hand-off is never dropped.
        handle_session_start_bootstrap({"session_id": "off-compact", "source": "compact"})
        assert "run /t3:interactive" not in capsys.readouterr().out


class TestOption1T3EngagedMarker:
    """#256 Option-1: any ``t3:`` skill engages the SUGGESTER (``.t3-engaged``); loops stay off."""

    @pytest.fixture(autouse=True)
    def _identity_closure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "_resolve_skill_closure", lambda skills: skills)
        # Drop the file fixture's autoload opt-in so engagement is driven only by
        # the skill markers under test (not by autoload).
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def test_t3_code_sets_t3_engaged_not_teatree_active(self) -> None:
        handle_track_skill_usage({"session_id": "o1", "tool_name": "Skill", "tool_input": {"skill": "t3:code"}})
        assert (router.STATE_DIR / "o1.t3-engaged").is_file()
        # Loops are reserved for teatree-requiring skills — never armed by a plain
        # lifecycle skill (keeps TestRealClosureMarkerActivation's contract).
        assert not _is_marked_active("o1")

    def test_t3_code_engages_session_but_loops_stay_off(self) -> None:
        handle_track_skill_usage({"session_id": "o1b", "tool_name": "Skill", "tool_input": {"skill": "t3:code"}})
        assert _teatree_engaged("o1b") is True
        assert _loop_auto_load_active("o1b") is False

    def test_instructions_loaded_t3_skill_sets_t3_engaged(self) -> None:
        handle_track_skill_usage({"session_id": "o1c", "skills": [{"name": "t3:review"}]})
        assert (router.STATE_DIR / "o1c.t3-engaged").is_file()
        assert not _is_marked_active("o1c")


class TestExplicitTeatreeEngages:
    """#256: explicitly loading ``/t3:interactive`` while OFF sets the marker and engages."""

    @pytest.fixture(autouse=True)
    def _identity_closure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "_resolve_skill_closure", lambda skills: skills)
        # Drop the file fixture's autoload opt-in so engagement is driven only by
        # the skill markers under test (not by autoload).
        monkeypatch.delenv("T3_AUTOLOAD", raising=False)

    def test_teatree_skill_engages_session(self) -> None:
        assert _teatree_engaged("tt-explicit") is False
        handle_track_skill_usage(
            {"session_id": "tt-explicit", "tool_name": "Skill", "tool_input": {"skill": "t3:interactive"}}
        )
        assert _is_marked_active("tt-explicit")
        assert _teatree_engaged("tt-explicit") is True


class TestMaybeEngageT3Normalization:
    """Nit A: ``_maybe_engage_t3`` canonicalizes the token through the SAME identity seam.

    The bug: ``_maybe_engage_t3`` engaged on a raw ``name.startswith("t3:")`` while
    its sibling ``_skill_load_activates_teatree`` normalized every token through the
    ``normalize_skill_name`` identity seam — so a bare / path-form ``t3:`` skill was
    detected by one but not the other. The fix routes ``_maybe_engage_t3`` through
    ``normalize_skill_name`` (normalize UP), so bare and qualified forms engage
    identically while a foreign namespace keeps its qualifier and never matches.
    """

    @pytest.fixture(autouse=True)
    def _snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Deterministic owned-set + namespace so the seam resolves without a live
        # skill-tree scan: ``code``/``teatree``/``review`` are owned in ``t3``.
        monkeypatch.setattr(router, "_skill_canon_snapshot", lambda: (frozenset({"code", "teatree", "review"}), "t3"))

    @staticmethod
    def _engaged(session_id: str) -> bool:
        return (router.STATE_DIR / f"{session_id}.t3-engaged").is_file()

    def test_qualified_t3_token_engages(self) -> None:
        router._maybe_engage_t3("q", ["t3:code"])
        assert self._engaged("q")

    def test_bare_owned_token_engages_like_qualified(self) -> None:
        # RED before the fix: ``"code".startswith("t3:")`` is False.
        router._maybe_engage_t3("b", ["code"])
        assert self._engaged("b")

    def test_path_form_token_engages(self) -> None:
        # RED before the fix: the ``/SKILL.md`` path form does not start with ``t3:``.
        router._maybe_engage_t3("p", ["teatree/SKILL.md"])
        assert self._engaged("p")

    def test_foreign_namespace_does_not_engage(self) -> None:
        # Identity-correct: a foreign-namespaced token keeps its qualifier and is
        # never promoted to ``t3:`` (no qualifier-stripping conflation).
        router._maybe_engage_t3("f", ["other:review"])
        assert not self._engaged("f")

    def test_non_owned_skill_does_not_engage(self) -> None:
        router._maybe_engage_t3("a", ["ac-python"])
        assert not self._engaged("a")
