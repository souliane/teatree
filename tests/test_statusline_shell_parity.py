# test-path: cross-cutting
"""``statusline.txt``'s Django RENDERER and the shell CONSUMER agree (#3830).

The statusline is a cross-tier artifact of the #3499 / #3819 / #3826 shape,
with three resolvers instead of two:

* :mod:`teatree.loop.statusline_render` writes the zones file under Django.
* ``hooks/scripts/statusline.sh`` parses it back with shell builtins and awk,
    with no Python at all, and composes it with the loop-owner badge it reads
    out of the registry :mod:`hooks.scripts.hook_router` writes.
* the same shell script reads the ``ConfigSetting`` store a THIRD way, through
    raw ``sqlite3`` queries, to decide whether to render at all
    (``_autoload_db_value``) and what to chain (``_statusline_chain_db``).

Every one of the tests that names the file imports only ``teatree.*``, so the
render side is pinned in great detail while the contract the shell actually
parses — which line is the loop line, what an ANSI-wrapped line looks like,
where the file lives, how a stored setting is interpreted — is pinned nowhere.
That is the surface where a working render and a refusing render look identical
to the consumer.

This lane feeds real renderer output to the real shell and asserts the chips
survive, then pins the shell's sqlite3 tier against the parsers the Django
resolver applies to the same stored row.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from teatree.config.host_projection import ProjectionPublisher
from teatree.config.setting_parsers import _parse_str_list, _parse_strict_bool
from teatree.loop.statusline_render import StatuslineEntry, StatuslineZones, default_path, render

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "hooks" / "scripts" / "statusline.sh"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;[^\x1b]*\x1b\\")

_SESSION = "parity-session"

#: Stored ``autoload`` values the Django resolver ACCEPTS, JSON-encoded exactly as
#: ``t3 <overlay> config_setting set`` stores them.
_AUTOLOAD_STORABLE: list[bool] = [True, False]

#: Stored values the Django resolver REJECTS (``_parse_strict_bool`` is strict on
#: purpose — ``bool("false")`` once silently ENABLED an opt-in safety setting,
#: #258). The bash duplicate re-implements the read, so it must reject them too:
#: a truthy-looking string that turns the gate ON in one tier and OFF in the other
#: is the divergence this lane exists to catch.
_AUTOLOAD_REJECTED: list[object] = ["true", "false", "yes", "on", "off", "1", "0", 1, 0]


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _seed_config_db(db: Path, rows: dict[str, object]) -> None:
    """Build the ``teatree_config_setting`` table the shell's sqlite3 tier reads."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        for key, value in rows.items():
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


def _run_shell(
    sandbox: Path,
    *,
    statusline_file: Path | None = None,
    config_db: Path | None = None,
    autoload_env: str | None = "1",
    session_id: str = _SESSION,
) -> subprocess.CompletedProcess[str]:
    """Run the real consumer over a hermetic HOME/XDG sandbox."""
    state_dir = sandbox / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{session_id}.teatree-active").touch()

    env = os.environ.copy()
    env["HOME"] = str(sandbox / "home")
    env["XDG_DATA_HOME"] = str(sandbox / "xdg")
    env["COLUMNS"] = "1000"
    env["TEATREE_CLAUDE_STATUSLINE_STATE_DIR"] = str(state_dir)
    env["CLAUDE_CONFIG_DIR"] = str(state_dir)
    env["CLAUDE_TASKS_DIR"] = str(state_dir / "_tasks")
    env["T3_LOOP_REGISTRY_DIR"] = str(sandbox / "registry")
    env.pop("TEATREE_STATUSLINE_FILE", None)
    if statusline_file is not None:
        env["TEATREE_STATUSLINE_FILE"] = str(statusline_file)
    if autoload_env is None:
        env.pop("T3_AUTOLOAD", None)
    else:
        env["T3_AUTOLOAD"] = autoload_env
    if config_db is None:
        env.pop("T3_CONFIG_DB", None)
    else:
        env["T3_CONFIG_DB"] = str(config_db)

    return subprocess.run(
        [str(_SCRIPT)],
        input=json.dumps({"session_id": session_id, "model": {"display_name": "Claude Opus"}}),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=_REPO_ROOT,
    )


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One HOME/XDG sandbox that BOTH tiers resolve their paths from."""
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "xdg").mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "registry"))
    return tmp_path


def _claim_loop_owner(session_id: str = _SESSION, pid: int = 4242) -> None:
    """Claim the tick-owner slot through the hook tier that owns the registry."""
    router._write_loop_registry({router._OWNER_LOOP: {"session_id": session_id, "pid": pid}})


class TestRenderedZonesSurviveTheShell:
    """Every chip the renderer emits reaches the consumer's output."""

    @pytest.mark.parametrize("colorize", [True, False])
    def test_every_rendered_line_reaches_the_output(self, sandbox: Path, *, colorize: bool) -> None:
        zones = StatuslineZones(
            anchors=["[acme] anchor chip"],
            action_needed=["[acme] action chip"],
            in_flight=["[acme] in-flight chip"],
        )
        target = sandbox / "zones" / "statusline.txt"
        target.parent.mkdir(exist_ok=True)
        render(zones, target=target, colorize=colorize)

        result = _run_shell(sandbox, statusline_file=target)

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        for chip in ("[acme] anchor chip", "[acme] action chip", "[acme] in-flight chip"):
            assert chip in plain, plain

    def test_a_hyperlinked_entry_keeps_its_text_through_the_shell(self, sandbox: Path) -> None:
        # ``StatuslineEntry`` renders as an OSC 8 hyperlink. The shell's width cap
        # walks escape sequences by hand; a mismatch there eats the visible text.
        zones = StatuslineZones(anchors=[StatuslineEntry("[acme] PR !42 needs review", "https://example.com/pr/42")])
        target = sandbox / "zones" / "statusline.txt"
        target.parent.mkdir(exist_ok=True)
        render(zones, target=target, colorize=True)

        result = _run_shell(sandbox, statusline_file=target)

        assert result.returncode == 0, result.stderr
        assert "[acme] PR !42 needs review" in _strip_ansi(result.stdout)

    def test_the_shell_reads_the_renderers_default_path(self, sandbox: Path) -> None:
        # Neither tier is told where the file is: the renderer resolves
        # ``default_path()`` and the shell resolves its own default, from the same
        # ``XDG_DATA_HOME``. A drift in either makes the statusline silently blank.
        render(StatuslineZones(anchors=["[acme] default-path chip"]), colorize=False)
        assert default_path() == sandbox / "xdg" / "teatree" / "statusline.txt"

        result = _run_shell(sandbox, statusline_file=None)

        assert result.returncode == 0, result.stderr
        assert "[acme] default-path chip" in _strip_ansi(result.stdout)


class TestLoopLineClassificationParity:
    """The shell's "is line 1 the loop line?" test matches what the renderer emits."""

    @pytest.mark.parametrize("colorize", [True, False])
    def test_an_unprefixed_first_line_is_treated_as_the_loop_line(self, sandbox: Path, *, colorize: bool) -> None:
        # The renderer gives the loop line no ``[overlay]`` prefix and (colorized)
        # wraps it in an SGR escape. The shell decides "line 1 is the loop line"
        # from exactly those two facts, and prepends the badge INTO that line.
        _claim_loop_owner()
        zones = StatuslineZones(anchors=["tick 5m · 2 loops", "[acme] anchor chip"])
        target = sandbox / "zones" / "statusline.txt"
        target.parent.mkdir(exist_ok=True)
        render(zones, target=target, colorize=colorize)

        result = _run_shell(sandbox, statusline_file=target)

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        loop_line = next(line for line in plain.splitlines() if "tick 5m · 2 loops" in line)
        assert "t3-master: you ✓" in loop_line, plain
        assert loop_line.index("t3-master:") < loop_line.index("tick 5m"), loop_line

    def test_an_overlay_prefixed_first_line_is_not_the_loop_line(self, sandbox: Path) -> None:
        # No loop live ⇒ every rendered line carries an ``[overlay]`` prefix, and the
        # badge must fall back to its own trailing line rather than corrupting an
        # anchor. The fallback is driven by awk's exit status, so a renderer that
        # stopped emitting the prefix would silently graft the badge onto an anchor.
        _claim_loop_owner()
        target = sandbox / "zones" / "statusline.txt"
        target.parent.mkdir(exist_ok=True)
        render(StatuslineZones(anchors=["[acme] anchor chip"]), target=target, colorize=True)

        result = _run_shell(sandbox, statusline_file=target)

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        anchor_line = next(line for line in plain.splitlines() if "[acme] anchor chip" in line)
        assert "t3-master:" not in anchor_line, plain
        assert "t3-master: you ✓" in plain, plain

    def test_a_foreign_owner_in_the_hook_registry_is_reported_as_foreign(self, sandbox: Path) -> None:
        _claim_loop_owner(session_id="someone-else-entirely", pid=os.getpid())
        target = sandbox / "zones" / "statusline.txt"
        target.parent.mkdir(exist_ok=True)
        render(StatuslineZones(anchors=["tick 5m"]), target=target, colorize=False)

        result = _run_shell(sandbox, statusline_file=target)

        assert result.returncode == 0, result.stderr
        plain = _strip_ansi(result.stdout)
        assert "t3-master: you ✓" not in plain, plain
        assert "someone-" in plain, plain

    def test_an_empty_render_is_distinguishable_from_a_refusing_one(self, sandbox: Path) -> None:
        # A render with nothing to say writes an EMPTY file; the gate refusing to
        # render at all emits the autoload hint. The shell must not collapse the
        # two into the same blank bar — that ambiguity is the #3830 failure mode.
        target = sandbox / "zones" / "statusline.txt"
        target.parent.mkdir(exist_ok=True)
        render(StatuslineZones(), target=target, colorize=False)
        assert target.read_text(encoding="utf-8") == ""

        rendered_empty = _run_shell(sandbox, statusline_file=target)
        refused = _run_shell(sandbox, statusline_file=target, autoload_env=None, config_db=sandbox / "absent.sqlite3")

        assert "model=Claude Opus" in _strip_ansi(rendered_empty.stdout)
        assert "model=Claude Opus" not in _strip_ansi(refused.stdout)
        assert "autoload" in refused.stdout


class TestStatuslineSettingsTierParity:
    """The shell's raw-``sqlite3`` reads interpret a stored row like the resolver does."""

    def _gate_renders(self, sandbox: Path, stored: object) -> bool:
        db = sandbox / "db.sqlite3"
        _seed_config_db(db, {"autoload": stored})
        target = sandbox / "zones" / "statusline.txt"
        target.parent.mkdir(exist_ok=True)
        render(StatuslineZones(anchors=["[acme] gate chip"]), target=target, colorize=False)

        result = _run_shell(sandbox, statusline_file=target, autoload_env=None, config_db=db)

        assert result.returncode == 0, result.stderr
        return "model=Claude Opus" in _strip_ansi(result.stdout)

    @pytest.mark.parametrize("stored", _AUTOLOAD_STORABLE)
    def test_the_autoload_gate_matches_the_registered_parser(self, sandbox: Path, *, stored: bool) -> None:
        assert self._gate_renders(sandbox, stored) is _parse_strict_bool(stored)

    @pytest.mark.parametrize("stored", _AUTOLOAD_REJECTED)
    def test_a_value_the_resolver_rejects_never_opens_the_gate(self, sandbox: Path, stored: object) -> None:
        with pytest.raises(ValueError, match="Invalid bool value"):
            _parse_strict_bool(stored)

        assert self._gate_renders(sandbox, stored) is False

    def test_the_chained_scripts_match_the_registered_list_parser(self, sandbox: Path) -> None:
        chained = sandbox / "extra-segment.sh"
        chained.write_text('#!/usr/bin/env bash\nprintf "%s" "chained-segment"\n', encoding="utf-8")
        chained.chmod(0o755)
        stored = [str(chained)]
        db = sandbox / "db.sqlite3"
        _seed_config_db(db, {"autoload": True, "statusline_chain": stored})
        target = sandbox / "zones" / "statusline.txt"
        target.parent.mkdir(exist_ok=True)
        render(StatuslineZones(anchors=["[acme] chain chip"]), target=target, colorize=False)

        result = _run_shell(sandbox, statusline_file=target, autoload_env=None, config_db=db)

        assert result.returncode == 0, result.stderr
        assert _parse_str_list(stored) == [str(chained)]
        assert "chained-segment" in _strip_ansi(result.stdout), result.stdout

    def test_an_empty_chain_setting_runs_nothing(self, sandbox: Path) -> None:
        db = sandbox / "db.sqlite3"
        _seed_config_db(db, {"autoload": True, "statusline_chain": []})
        target = sandbox / "zones" / "statusline.txt"
        target.parent.mkdir(exist_ok=True)
        render(StatuslineZones(anchors=["[acme] chain chip"]), target=target, colorize=False)

        result = _run_shell(sandbox, statusline_file=target, autoload_env=None, config_db=db)

        assert result.returncode == 0, result.stderr
        assert _parse_str_list([]) == []
        assert "[acme] chain chip" in _strip_ansi(result.stdout)


def test_the_consumer_and_its_dependencies_are_present() -> None:
    """Anti-vacuity: a missing script or ``jq`` would make every case above pass trivially."""
    assert _SCRIPT.is_file()
    assert shutil.which("jq"), "statusline.sh parses its stdin payload with jq"
    assert shutil.which("sqlite3"), "the settings tier under test IS the sqlite3 CLI read"


class TestStatuslineReadsTheProjectionWhenTheDbIsNotOnThisHost:
    """The Django PUBLISHER's projection is read by the Django-free shell CONSUMER.

    The shell tier resolves a HOST sqlite path. Once the control DB moved into a
    container-only volume (#4001) that path was absent, every ConfigSetting read returned
    empty, and an ABSENT store is indistinguishable from ``autoload = false`` — so the bar
    vanished and claimed "autoload disabled", which is a different and untrue statement.
    The Python tier already had this fallback (``cold_db.canonical_projection``); the shell
    did not, which is why ``t3 loop status`` kept working while the statusline went dark.

    This is a real both-tier lane: :class:`ProjectionPublisher` WRITES the artifact and the
    shell READS it, so a change to either side's shape fails here rather than in production.
    """

    def _publish(self, sandbox: Path, rows: dict[str, object]) -> None:
        """Write the projection the way production does — through the real publisher."""
        db = sandbox / "source.sqlite3"
        _seed_config_db(db, rows)
        # The publisher projects the loop/mode tables alongside settings, so the source
        # has to carry them. Empty is honest here: this lane is about the settings tier
        # reaching the shell, and an empty loop table projects as no loop state.
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS teatree_loop_state (name TEXT, status TEXT)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS teatree_loop_preset "
                "(name TEXT, defers_questions INT, pauses_self_pump INT, presence_sensitive INT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS teatree_loop_preset_override (preset_name TEXT, until TEXT, set_at TEXT)"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS teatree_loop_schedule (name TEXT, id INT, timezone TEXT)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS teatree_loop_schedule_slot "
                "(schedule_id INT, days TEXT, start_time TEXT, preset_name TEXT)"
            )
            conn.commit()
        finally:
            conn.close()
        data_dir = sandbox / "xdg" / "teatree"
        data_dir.mkdir(parents=True, exist_ok=True)
        ProjectionPublisher(db, data_dir).publish()
        # The consumer hardcodes this filename, so the name IS part of the seam: rename it
        # publisher-side and the shell silently reads nothing, which is the failure this
        # whole lane exists to catch.
        assert (data_dir / "host-projection.json").is_file(), "the publisher wrote the file the shell reads"

    def _render(self, sandbox: Path) -> str:
        target = sandbox / "zones" / "statusline.txt"
        target.parent.mkdir(exist_ok=True)
        render(StatuslineZones(anchors=["[acme] projection chip"]), target=target, colorize=False)
        # A path that does NOT exist: exactly the containerized host's situation.
        result = _run_shell(sandbox, statusline_file=target, autoload_env=None, config_db=sandbox / "absent.sqlite3")
        assert result.returncode == 0, result.stderr
        return _strip_ansi(result.stdout)

    def test_a_published_opt_in_opens_the_gate(self, sandbox: Path) -> None:
        self._publish(sandbox, {"autoload": True})
        assert "projection chip" in self._render(sandbox), "the publisher's value reached the shell"

    def test_no_projection_still_refuses(self, sandbox: Path) -> None:
        # Anti-vacuous: a fallback that ignored its input, or opened the gate whenever the
        # sqlite path was missing, would pass the test above and fail this one.
        assert "statusline off" in self._render(sandbox)

    def test_a_published_false_still_refuses(self, sandbox: Path) -> None:
        # The shell must READ the published value, not merely notice the file exists.
        self._publish(sandbox, {"autoload": False})
        assert "projection chip" not in self._render(sandbox)
