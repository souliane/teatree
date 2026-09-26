"""Pin what a ``claude_sdk`` session receives: its context, its turns, its prefix.

The owner guardrail on the cheap-lane work is that none of it may change the
subscription lane. That lane is not addressed by any of it — the changes live in
the loop's review-dispatch rules and in the metered harness — so the guarantee is
cheap to give and worth nothing unspoken: a later edit to
:func:`~teatree.agents._runner_options._build_options` or to
:func:`~teatree.agents.prompt.build_system_context` would move it silently, on a
lane with no cost signal to notice.

So this pins the three axes by name.

1. **Prefix** — the ``claude_code`` preset is APPENDED to, never replaced, and its
    per-run dynamic sections stay excluded. Prompt caching here is CLI-internal and
    exposes no ``cache_control`` surface, so prefix STABILITY is the only lever
    teatree has over the hit rate.
2. **Context** — the system context reaches the SDK verbatim, and
    ``build_system_context`` still leads with the stable framing rather than the
    per-task identity.
3. **Turns** — ``max_turns`` comes from ``resolve_agent_max_turns()`` and nothing
    else, so the per-run ceiling stays the operator's configured value.

Beside the named axes, the whole options object (minus the ambient ``env`` and
``debug_stderr``) and the rendered system context are hashed over a fixed task and
fixture skills, so a field no axis names cannot move either. A deliberate change
updates the matching constant in the same commit.
"""

import contextlib
import dataclasses
import hashlib
import inspect
import io
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from claude_agent_sdk.types import SystemPromptPreset
from django.test import TestCase

import hooks.scripts.hook_router as router
from hooks.scripts.session_start_skills import session_start_skill_context
from teatree.agents import permission_modes
from teatree.agents._runner_options import (
    _EXTERNAL_CONTACT_BUILTINS,
    SpawnOverrides,
    _build_options,
    resolve_agent_max_turns,
)
from teatree.agents.compaction_guard import CompactionGuard
from teatree.agents.model_tiering import resolve_spawn_model
from teatree.agents.prompt import build_system_context
from teatree.cli.agent import _launch_claude, agent
from teatree.cli.doctor import IntrospectionHelpers
from teatree.cli.loop.app import start_command
from teatree.config.settings import OverlayEntry
from teatree.core.models import ConfigSetting, Session, Task, Ticket
from teatree.core.overlay import OverlayBase, OverlayConfig, ProvisionStep
from teatree.core.overlay_metadata import OverlayMetadata
from tests._git_repo import make_git_repo, run_git

_SYSTEM_CONTEXT = "You are a TeaTree headless agent executing a task.\n\n[pinned marker]"

_OPTIONS_SHA256 = "e811342370449f9c0bad52d132bc3b4377e5a4b5dc3b1133dad2f34ca1351240"
_SYSTEM_CONTEXT_SHA256 = "04670799ff2364052a6b0933e4a4ff9b0aff6323c1bd9ec6d3cbf86e61af1207"

_SKILLS = ["pin-lifecycle", "pin-companion"]


class TestClaudeSdkOptionsPin(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def _options(self, *, phase: str = "coding"):
        session = Session.objects.create(ticket=self.ticket)
        task = Task.objects.create(ticket=self.ticket, session=session, phase=phase)
        return _build_options(task, _SYSTEM_CONTEXT, phase=phase, skills=[])

    def test_prefix_appends_to_the_claude_code_preset_with_dynamic_sections_excluded(self) -> None:
        prompt = self._options().system_prompt

        assert isinstance(prompt, dict | SystemPromptPreset)
        assert prompt["type"] == "preset"
        assert prompt["preset"] == "claude_code"
        assert prompt["exclude_dynamic_sections"] is True

    def test_context_reaches_the_sdk_verbatim(self) -> None:
        assert self._options().system_prompt["append"] == _SYSTEM_CONTEXT

    def test_system_context_still_leads_with_the_stable_framing(self) -> None:
        session = Session.objects.create(ticket=self.ticket)
        task = Task.objects.create(ticket=self.ticket, session=session, phase="coding")

        context = build_system_context(task, skills=[])

        assert context.startswith("You are a TeaTree headless agent executing a task.")

    def test_turn_ceiling_is_the_resolved_setting_and_nothing_else(self) -> None:
        assert self._options().max_turns == resolve_agent_max_turns()

    def test_headless_permission_mode_and_external_contact_denials_are_unchanged(self) -> None:
        options = self._options()

        assert options.permission_mode == permission_modes.UNATTENDED
        assert set(_EXTERNAL_CONTACT_BUILTINS) <= set(options.disallowed_tools)


def _canonical(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(json.dumps(_canonical(item), sort_keys=True) for item in value)
    if inspect.ismethod(value):
        return {"method": value.__func__.__qualname__, "bound_to": _canonical(value.__self__)}
    return _canonical_leaf(value)


def _canonical_leaf(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if inspect.isfunction(value):
        return f"{value.__module__}.{value.__qualname__}"
    return type(value).__qualname__


def _sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class TestClaudeSdkDispatchIsPinned(TestCase):
    def setUp(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "claude_sdk")
        ticket = Ticket.objects.create(pk=424_242, issue_url="https://example.com/org/repo/-/issues/7")
        session = Session.objects.create(ticket=ticket)
        self.task = Task.objects.create(pk=424_242, ticket=ticket, session=session, phase="coding")
        skills_dir = Path(tempfile.mkdtemp())
        for name in _SKILLS:
            (skills_dir / name).mkdir()
            (skills_dir / name / "SKILL.md").write_text(f"# {name}\n\nFixture body for {name}.\n", encoding="utf-8")
        self._skills_dir = skills_dir

    def _dispatch(self) -> tuple[str, dict[str, object]]:
        with (
            patch("teatree.agents.skill_injection.DEFAULT_SKILLS_DIR", self._skills_dir),
            patch("teatree.agents.skill_injection.harness_skills_dirs", return_value=[self._skills_dir]),
        ):
            system_context = build_system_context(
                self.task, skills=_SKILLS, lifecycle_skill="pin-lifecycle", stage_skills=[]
            ).replace(str(self._skills_dir), "<skills-dir>")
            options = _build_options(self.task, system_context, phase="coding", skills=_SKILLS)
        serialised = _canonical(options)
        assert isinstance(serialised, dict)
        for ambient in ("env", "debug_stderr"):
            serialised.pop(ambient)
        return system_context, serialised

    def test_the_system_context_is_unchanged(self) -> None:
        system_context, _ = self._dispatch()
        assert hashlib.sha256(system_context.encode()).hexdigest() == _SYSTEM_CONTEXT_SHA256

    def test_the_options_are_unchanged(self) -> None:
        _, options = self._dispatch()
        assert _sha256(options) == _OPTIONS_SHA256


_COMPACTION_SWITCHES = ("DISABLE_COMPACT", "DISABLE_AUTO_COMPACT", "autoCompactEnabled")

type _Launch = tuple[list[str], dict[str, str]]


def _carries_a_compaction_switch(launch: _Launch) -> bool:
    argv, env = launch
    return any(switch in env or any(switch in arg for arg in argv) for switch in _COMPACTION_SWITCHES)


class TestOnlyTheFactoryRunsUncompacted(TestCase):
    """The compaction switch reaches the factory's ``claude`` child and never an attended session."""

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        config_db = root / "db.sqlite3"
        with closing(sqlite3.connect(str(config_db))) as conn:
            conn.execute(
                "CREATE TABLE teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)"
            )
            conn.commit()
        self._env = {
            "DJANGO_SETTINGS_MODULE": "teatree.settings",
            "HOME": str(root / "home"),
            "PATH": "/pin/bin",
            "T3_CONFIG_DB": str(config_db),
        }

    def _launch(self, exec_target: str, launch: Callable[[], None], extra_env: dict[str, str]) -> _Launch:
        calls: list[_Launch] = []

        def record(_file: str, argv: list[str]) -> None:
            calls.append((list(argv), dict(os.environ)))

        with (
            patch.dict(os.environ, {**self._env, **extra_env}, clear=True),
            patch("shutil.which", return_value="/pin/bin/claude"),
            patch(exec_target, side_effect=record),
        ):
            launch()
        [call] = calls
        return call

    def _agent_session(self, extra_env: dict[str, str] | None = None) -> _Launch:
        def launch() -> None:
            _launch_claude(
                task="",
                project_root=Path("/pin/project"),
                context_lines=["You are working on a TeaTree project."],
                skills=["t3:code"],
                ask_user_which_skill=False,
            )

        with patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///pin/teatree")):
            return self._launch("teatree.cli.agent.os.execvp", launch, extra_env or {})

    def _loop_session(self) -> _Launch:
        with patch("teatree.cli.loop.app._stdin_is_terminal", return_value=True):
            return self._launch("teatree.cli.loop.app.os.execv", lambda: start_command(print_only=False), {})

    def _factory_task(self) -> Task:
        ticket = Ticket.objects.create()
        return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")

    def test_a_claude_child_dispatch_runs_with_compaction_off_on_top_of_its_credential(self) -> None:
        overrides = SpawnOverrides(env={"ANTHROPIC_API_KEY": "key-x"}, compaction_guard=CompactionGuard())

        options = _build_options(self._factory_task(), _SYSTEM_CONTEXT, phase="coding", skills=[], overrides=overrides)

        assert options.env == {"ANTHROPIC_API_KEY": "key-x", "DISABLE_COMPACT": "1"}
        assert "PreCompact" in (options.hooks or {})

    def test_a_dispatch_without_a_claude_child_gets_no_switch(self) -> None:
        options = _build_options(self._factory_task(), _SYSTEM_CONTEXT, phase="coding", skills=[])

        assert "DISABLE_COMPACT" not in options.env
        assert "PreCompact" not in (options.hooks or {})

    def test_the_attended_agent_session_carries_no_compaction_switch(self) -> None:
        assert not _carries_a_compaction_switch(self._agent_session())

    def test_the_attended_loop_session_carries_no_compaction_switch(self) -> None:
        assert not _carries_a_compaction_switch(self._loop_session())

    def test_a_switch_leaked_into_the_agent_session_env_fails_the_pin(self) -> None:
        leaked = self._agent_session({"DISABLE_COMPACT": "1"})

        assert not _carries_a_compaction_switch(self._agent_session())
        assert _carries_a_compaction_switch(leaked)

    def test_a_switch_leaked_into_the_loop_session_argv_fails_the_pin(self) -> None:
        with patch(
            "teatree.cli.loop.app._session_pin_flags", return_value=["--settings", '{"autoCompactEnabled": false}']
        ):
            leaked = self._loop_session()

        assert not _carries_a_compaction_switch(self._loop_session())
        assert _carries_a_compaction_switch(leaked)


_INTERACTIVE_PINS = {
    "agent": "2d82d45f5ce655a0f852021ae2c20a0b25d3a6a8d03460eb0fe8d1dd4a003b3b",
    "loop": "bbfc5d6f4b4b8bc5d3f524a985ffd41deb11738786f2462d8459c8967cd6eee9",
    "env": "3c24b712e780f1c444411f262b75abbe6f19542d79fffb2f831fbde71e8a7b63",
    "skill_context": "a54da20397a5927a8579a83249f45593593601ed91a8f912d01195ce19e96b5c",
}

_FACTORY_ONLY_MARKER = "factory-only-pin-marker"
_PIN_ROOT = "<pin-root>"
_PIN_PATH = "<pin-path>"
_PIN_OVERLAY = "t3-pin"
_PIN_REMOTE_PATTERN = "*example.com:pin/*"
_REAL_WHICH = shutil.which

type _Exec = tuple[list[str], dict[str, str]]


class _PinMetadata(OverlayMetadata):
    skill_path = "skills/t3-pin/SKILL.md"

    def get_skill_metadata(self):
        return {"skill_path": self.skill_path, "remote_patterns": [_PIN_REMOTE_PATTERN]}


class _PinOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["pin/project"]

    def get_provision_steps(self, worktree) -> list[ProvisionStep]:
        return []


def _which_claude(name: str, *args, **kwargs) -> str | None:
    return "/pin/bin/claude" if name == "claude" else _REAL_WHICH(name, *args, **kwargs)


def _read_or_empty(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


class TestInteractiveSessionsArePinned(TestCase):
    """The owner's attended sessions receive exactly these bytes; a factory-only value reaching them fails the pin.

    Each digest is composed by the public entry point from a fixture world: a git checkout, an empty config
    store, a skill-metadata cache and one installed overlay. Only the entry-point registry, the install-mode
    introspection and the ``claude`` binary lookup are fixtures; discovery, selection and composition run.
    """

    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp())
        self._project = make_git_repo(self._root / "project")
        run_git(self._project, "remote", "add", "origin", "git@example.com:pin/project.git")
        (self._project / "pyproject.toml").write_text('[project]\nname = "pin-project"\n', encoding="utf-8")
        self._config_db = self._root / "db.sqlite3"
        conn = sqlite3.connect(str(self._config_db))
        conn.execute("CREATE TABLE teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)")
        conn.commit()
        conn.close()
        cache = self._root / "data" / "teatree" / "skill-metadata.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(
            json.dumps(
                {"skill_path": _PinMetadata.skill_path, "remote_patterns": [_PIN_REMOTE_PATTERN], "skill_index": []}
            ),
            encoding="utf-8",
        )
        self._overlay = _PinOverlay()
        self._overlay.metadata = _PinMetadata()
        self._overlay.config = OverlayConfig(companion_skills=["t3:review"])
        self._git_path = str(Path(_REAL_WHICH("git") or "/usr/bin/git").parent)
        self._env = {
            "DJANGO_SETTINGS_MODULE": "teatree.settings",
            "HOME": str(self._root / "home"),
            "PATH": self._git_path,
            "T3_CONFIG_DB": str(self._config_db),
            "XDG_DATA_HOME": str(self._root / "data"),
        }

    def _seed_config(self, rows: Mapping[str, object]) -> None:
        with contextlib.closing(sqlite3.connect(str(self._config_db))) as conn:
            conn.executemany(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
                [(key, json.dumps(value)) for key, value in rows.items()],
            )
            conn.commit()

    def _scrub(self, value: str) -> str:
        replacements = (
            (str(self._root.resolve()), _PIN_ROOT),
            (str(self._root), _PIN_ROOT),
            (self._git_path, _PIN_PATH),
        )
        for real, placeholder in replacements:
            value = value.replace(real, placeholder)
        return value

    @contextlib.contextmanager
    def _world(self, extra_env: Mapping[str, str] | None = None) -> Iterator[None]:
        installed = [OverlayEntry(name=_PIN_OVERLAY, overlay_class=f"{__name__}:_PinOverlay")]
        with (
            patch.dict(os.environ, {**self._env, **(extra_env or {})}, clear=True),
            contextlib.chdir(self._project),
            patch("teatree.config.discover_overlays", return_value=installed),
            patch("teatree.core.overlay_loader._discover_overlays", return_value={_PIN_OVERLAY: self._overlay}),
        ):
            yield

    def _exec(
        self, exec_target: str, compose: Callable[[], object], extra_env: Mapping[str, str] | None = None
    ) -> _Exec:
        calls: list[_Exec] = []

        def record(_file: str, argv: list[str]) -> None:
            calls.append(
                ([self._scrub(arg) for arg in argv], {key: self._scrub(value) for key, value in os.environ.items()})
            )

        with (
            self._world(extra_env),
            patch("shutil.which", side_effect=_which_claude),
            patch(exec_target, side_effect=record),
        ):
            compose()
        [session] = calls
        return session

    def _agent_session(self, extra_env: Mapping[str, str] | None = None) -> _Exec:
        with patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///pin/teatree")):
            return self._exec("teatree.cli.agent.os.execvp", lambda: agent(task="", phase="", skill=[]), extra_env)

    def _loop_session(self) -> _Exec:
        with patch("teatree.cli.loop.app._stdin_is_terminal", return_value=True):
            return self._exec("teatree.cli.loop.app.os.execv", lambda: start_command(print_only=False))

    def _skill_context(self) -> dict[str, str]:
        state_dir = self._root / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "pin-prompt.t3-engaged").touch()
        prompt_output = io.StringIO()
        with self._world(), patch.object(router, "STATE_DIR", state_dir):
            with contextlib.redirect_stdout(prompt_output):
                router.handle_user_prompt_submit({"session_id": "pin-prompt", "prompt": "fix the flaky assertion"})
            session_start = session_start_skill_context("pin-start")
        return {
            "prompt_submit": prompt_output.getvalue(),
            "prompt_pending": _read_or_empty(state_dir / "pin-prompt.pending"),
            "session_start": session_start,
            "session_start_pending": _read_or_empty(state_dir / "pin-start.pending"),
        }

    def _digests(self, env: Mapping[str, str] | None = None) -> dict[str, str]:
        agent_argv, agent_env = self._agent_session(env)
        loop_argv, loop_env = self._loop_session()
        return {
            "agent": _sha256(agent_argv),
            "loop": _sha256(loop_argv),
            "env": _sha256({"agent": agent_env, "loop": loop_env}),
            "skill_context": _sha256(self._skill_context()),
        }

    def test_the_attended_sessions_are_unchanged(self) -> None:
        assert self._digests() == _INTERACTIVE_PINS

    def test_factory_only_settings_reach_no_attended_session(self) -> None:
        factory_only = {
            "agent_phase_models": {"coding": "cheap", "reviewing": "balanced"},
            "agent_skill_models": {"t3:code": "frontier"},
            "agent_honesty_model": "balanced",
            "agent_max_turns": 7,
            "subagent_spawn_ceiling": 3,
            "envelope_stop_gate_refusals": 1,
            "pydantic_ai_request_limit": 11,
            "pydantic_ai_max_tokens": 1234,
            "openai_compatible_lane": "bulk",
        }
        self._seed_config(factory_only)
        for key, value in factory_only.items():
            ConfigSetting.objects.set_value(key, value)

        assert self._digests() == _INTERACTIVE_PINS

    def test_a_factory_skill_planted_in_the_overlay_skill_metadata_fails_the_agent_pin(self) -> None:
        assert _sha256(self._agent_session()[0]) == _INTERACTIVE_PINS["agent"]

        self._overlay.metadata.skill_path = f"skills/{_FACTORY_ONLY_MARKER}/SKILL.md"

        assert _sha256(self._agent_session()[0]) != _INTERACTIVE_PINS["agent"]

    def test_the_factory_coding_model_planted_as_the_session_model_fails_the_loop_pin(self) -> None:
        assert _sha256(self._loop_session()[0]) == _INTERACTIVE_PINS["loop"]

        with self._world():
            factory_model = resolve_spawn_model("coding", skills=[])
        self._seed_config({"agent_session_model": factory_model})

        assert _sha256(self._loop_session()[0]) != _INTERACTIVE_PINS["loop"]

    def test_the_factory_lane_planted_in_the_launching_env_fails_the_env_pin(self) -> None:
        assert self._digests()["env"] == _INTERACTIVE_PINS["env"]

        assert self._digests({"T3_OPENAI_COMPATIBLE_LANE": "factory"})["env"] != _INTERACTIVE_PINS["env"]

    def test_a_factory_companion_planted_in_the_overlay_config_fails_the_skill_context_pin(self) -> None:
        assert _sha256(self._skill_context()) == _INTERACTIVE_PINS["skill_context"]

        self._overlay.config = OverlayConfig(companion_skills=["t3:review", _FACTORY_ONLY_MARKER])

        assert _sha256(self._skill_context()) != _INTERACTIVE_PINS["skill_context"]
