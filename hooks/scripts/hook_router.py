#!/usr/bin/env python3
"""Unified hook router — single Python process for all Claude Code lifecycle hooks.

Replaces five bash scripts that each spawned bash + jq per invocation.
In a 200-tool-call session with 3 hooks per call, this eliminates ~600
subprocess spawns.

Usage in hooks.json::

    "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/hook_router.py --event <EVENT>"

Reads JSON from stdin. Writes JSON to stdout when blocking (PreToolUse deny).
Exits 0 silently for passthrough.
"""

import argparse
import contextlib
import json
import os
import re
import subprocess  # noqa: S404
import sys
import tempfile
from pathlib import Path

STATE_DIR = Path(
    os.environ.get(
        "TEATREE_CLAUDE_STATUSLINE_STATE_DIR",
        os.environ.get("T3_HOOK_STATE_DIR", "/tmp/claude-statusline"),  # noqa: S108
    )
)

_FILE_PATH_TOOLS = {"Read", "Edit", "Write"}
_PATH_TOOLS = {"Grep", "Glob"}
_MR_TOOLS = {"mcp__glab__glab_mr_create", "mcp__glab__glab_mr_update"}

# Patterns that indicate workspace/infrastructure operations where the agent
# MUST use `t3` CLI instead of running underlying commands directly.
_T3_CLI_REMINDER_RE = re.compile(
    r"\b("
    r"worktree|setup|workspace|database|restore|migrate|runserver|"
    r"manage\.py|nx serve|docker compose|createdb|dropdb|"
    r"playwright|e2e|frontend|backend|dslr|pg_restore|pg_dump|"
    r"npm run|pipenv|pip install"
    r")\b",
    re.IGNORECASE,
)

_T3_CLI_REMINDER = (
    "MANDATORY: Use `t3` CLI for ALL workspace, server, database, and test operations. "
    "NEVER run underlying commands directly (manage.py, nx serve, docker compose, "
    "createdb, playwright, npm run, pipenv, pip install, dslr, etc.). "
    "If a `t3` command fails, fix the `t3` code — do not work around it."
)

# Commands that are legitimate t3 CLI invocations — never block these.
# `uv run t3 ...` is intentionally NOT whitelisted here: it is caught by the
# blocked-commands list below so agents switch to the globally-installed t3.
_T3_CMD_PREFIX_RE = re.compile(
    r"^(?:\w+=\S+\s+)*t3\s",
)

# Read-only commands that may mention infrastructure tools as arguments
# (e.g. grep for 'playwright', echo about manage.py) — never block these.
_READONLY_CMD_PREFIX_RE = re.compile(
    r"^(?:echo|printf|cat|grep|rg|awk|sed|head|tail|less|wc|file|#)",
)

# Forbidden command patterns → deny messages.  Each entry is
# (compiled regex matching the Bash command, human-readable deny reason).
_BLOCKED_COMMANDS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\.venv/bin/"),
        "BLOCKED: `.venv/bin/...` — use `uv run` instead so the resolved environment matches `pyproject.toml`.",
    ),
    (
        re.compile(r"manage\.py\s+runserver"),
        "BLOCKED: `manage.py runserver` — use `t3 <overlay> worktree start` instead.",
    ),
    (
        re.compile(r"manage\.py\s+migrate"),
        "BLOCKED: `manage.py migrate` — use `t3 <overlay> worktree provision` instead.",
    ),
    (
        re.compile(r"\bnx\s+serve\b"),
        "BLOCKED: `nx serve` — use `t3 <overlay> worktree start` instead.",
    ),
    (
        re.compile(r"\bdocker\s+compose\s+(?:up|start)\b"),
        "BLOCKED: `docker compose up/start` — use `t3 <overlay> worktree start` instead.",
    ),
    (
        re.compile(r"\b(?:createdb|dropdb)\b"),
        "BLOCKED: `createdb`/`dropdb` — use `t3 <overlay> db reset` instead.",
    ),
    (
        re.compile(r"\b(?:npx\s+)?playwright\s+test\b"),
        "BLOCKED: `playwright test` — use `t3 <overlay> e2e` instead.",
    ),
    (
        re.compile(r"\bnpm\s+run\b"),
        (
            "BLOCKED: `npm run` — use `t3 <overlay> run build-frontend` "
            "(rebuild dist) or `t3 <overlay> worktree start` (full stack) instead."
        ),
    ),
    (
        re.compile(r"\b(?:pipenv|pip)\s+install\b"),
        "BLOCKED: `pip/pipenv install` — use `t3 <overlay> worktree provision` instead.",
    ),
    (
        re.compile(r"\b(?:pg_restore|pg_dump)\b"),
        "BLOCKED: `pg_restore`/`pg_dump` — use `t3 <overlay> db refresh` instead.",
    ),
    (
        re.compile(r"\bdslr\s+(?:restore|import|snapshot|rename|export)\b"),
        (
            "BLOCKED: mutating `dslr` subcommand — use "
            "`t3 <overlay> db refresh --dslr-snapshot <name>` instead. "
            "Only `dslr list` and `dslr delete` are allowed."
        ),
    ),
    (
        re.compile(r"\bgit\s+\S+.*--no-verify\b"),
        "BLOCKED: `--no-verify` — fix the hook failure instead of bypassing it.",
    ),
    (
        re.compile(r"\bgit\s+\S+.*--no-gpg-sign\b"),
        "BLOCKED: `--no-gpg-sign` — do not bypass signing without explicit user approval.",
    ),
    (
        re.compile(r"\bsafety\s+(?:check|scan)\b"),
        "BLOCKED: `safety` — use `pip-audit` (or `uv audit`) instead.",
    ),
    (
        re.compile(r"\buv\s+run\s+(?:\S+\s+)*?t3(?:\s|$)"),
        (
            "BLOCKED: `uv run t3` — teatree is installed globally; call `t3` directly. "
            "If `t3` is missing on this machine, install teatree (`uv tool install teatree` "
            "or `uv tool install --editable <teatree-repo>`)."
        ),
    ),
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified hook router")
    parser.add_argument("--event", required=True, help="Hook event name")
    return parser.parse_args()


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _read_input() -> dict:
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        return {}


def _state_file(session_id: str, suffix: str) -> Path:
    return STATE_DIR / f"{session_id}.{suffix}"


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").strip().splitlines() if line]


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{line}\n")


# ── UserPromptSubmit ────────────────────────────────────────────────


def _build_skill_loader_input(prompt: str, session_id: str) -> dict:
    teatree_home = os.environ.get("HOME", "")
    source_root = Path(__file__).resolve().parents[2].parent

    active = _read_lines(_state_file(session_id, "active"))
    loaded = _read_lines(_state_file(session_id, "skills"))

    search_dirs = [str(source_root), f"{teatree_home}/.agents/skills", f"{teatree_home}/.claude/skills"]
    return {
        "prompt": prompt,
        "cwd": str(Path.cwd()),
        "active_repos": active,
        "loaded_skills": loaded,
        "skill_search_dirs": [d for d in search_dirs if d],
        "supplementary_config": os.environ.get("T3_SUPPLEMENTARY_SKILLS", f"{teatree_home}/.teatree-skills.yml"),
    }


def handle_user_prompt_submit(data: dict) -> None:
    """Detect intent and suggest skills via skill_loader.suggest_skills()."""
    session_id = data.get("session_id", "")
    prompt = data.get("prompt", "")
    if not session_id or not prompt:
        return

    _ensure_state_dir()
    pending = _state_file(session_id, "pending")
    pending.write_text("", encoding="utf-8")

    scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
    if not (scripts_dir / "lib" / "skill_loader.py").is_file():
        return

    loader_input = _build_skill_loader_input(prompt, session_id)

    sys.path.insert(0, str(scripts_dir))
    try:
        from lib.skill_loader import suggest_skills  # noqa: PLC0415

        result = suggest_skills(loader_input)
    except Exception:  # noqa: BLE001
        return
    finally:
        sys.path.pop(0)

    suggestions = result.get("suggestions", [])

    # Deterministic t3 CLI reminder — injected when prompt matches
    # workspace/infrastructure patterns, regardless of skill suggestions.
    t3_reminder = _T3_CLI_REMINDER if _T3_CLI_REMINDER_RE.search(prompt) else ""

    if not suggestions:
        if t3_reminder:
            print(t3_reminder)  # noqa: T201
        return

    skill_list = ", ".join(f"/{s}" for s in suggestions)
    pending.write_text("\n".join(suggestions) + "\n", encoding="utf-8")
    parts = [f"LOAD THESE SKILLS NOW (call the Skill tool for each, before doing anything else): {skill_list}."]
    if t3_reminder:
        parts.append(t3_reminder)
    print("\n".join(parts))  # noqa: T201


# ── UserPromptSubmit + PreToolUse: enforce-loop-registration ──────────

_LOOP_CADENCE_DEFAULT = 720
_LOOP_PROMPT = "Run `t3 loop tick` in Bash, then briefly report the tick summary."


def _tick_meta_stale() -> bool:
    xdg = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    meta = Path(xdg) / "teatree" / "tick-meta.json"
    if not meta.is_file():
        return True
    cadence = int(os.environ.get("T3_LOOP_CADENCE", _LOOP_CADENCE_DEFAULT) or _LOOP_CADENCE_DEFAULT)
    import time  # noqa: PLC0415

    age = int(time.time()) - int(meta.stat().st_mtime)
    return age > cadence * 2


def _session_has_loop(session_id: str) -> bool:
    crons_file = _state_file(session_id, "crons")
    if not crons_file.is_file():
        return False
    try:
        data = json.loads(crons_file.read_text(encoding="utf-8"))
        return bool(data.get("jobs"))
    except (json.JSONDecodeError, OSError):
        return False


def _cleanup_stale_pending(session_id: str) -> None:
    """Remove loop-pending files from old sessions."""
    for f in STATE_DIR.glob("*.loop-pending"):
        if f.stem != session_id:
            f.unlink(missing_ok=True)


def handle_enforce_loop_on_prompt(data: dict) -> None:
    """On first prompt, check if the fat loop needs registration."""
    session_id = data.get("session_id", "")
    if not session_id:
        return
    _ensure_state_dir()
    _cleanup_stale_pending(session_id)
    pending = _state_file(session_id, "loop-pending")
    if _session_has_loop(session_id):
        pending.unlink(missing_ok=True)
        return
    if not _tick_meta_stale():
        return
    cadence = int(os.environ.get("T3_LOOP_CADENCE", _LOOP_CADENCE_DEFAULT) or _LOOP_CADENCE_DEFAULT)
    minutes = max(1, cadence // 60)
    pending.write_text("1", encoding="utf-8")
    print(  # noqa: T201
        f"Session setup: the teatree background loop is not registered yet. "
        f"Please call CronCreate with "
        f'cron="*/{minutes} * * * *", prompt="{_LOOP_PROMPT}", recurring=true.'
    )


def handle_enforce_loop_registration(data: dict) -> bool:
    """Block Bash/Edit/Write until the background loop cron is registered."""
    tool_name = data.get("tool_name", "")
    if tool_name in {"CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "Skill", "ToolSearch"}:
        return False
    session_id = data.get("session_id", "")
    if not session_id:
        return False
    pending = _state_file(session_id, "loop-pending")
    if not pending.is_file():
        return False
    if _session_has_loop(session_id):
        pending.unlink(missing_ok=True)
        return False
    cadence = int(os.environ.get("T3_LOOP_CADENCE", _LOOP_CADENCE_DEFAULT) or _LOOP_CADENCE_DEFAULT)
    minutes = max(1, cadence // 60)
    reason = (
        f"The teatree background loop is not registered yet. "
        f"Please call CronCreate with "
        f'cron="*/{minutes} * * * *", prompt="{_LOOP_PROMPT}", recurring=true.'
    )
    json.dump({"permissionDecision": "deny", "permissionDecisionReason": reason}, sys.stdout)
    return True


# ── UserPromptSubmit: todo-freshness nudge ──────────────────────────

_TODO_FRESHNESS_NUDGE = (
    "Session housekeeping: keep the task/TODO list current. "
    "Reflect finished work as completed and surface any newly discovered work "
    "as its own task before continuing."
)


def handle_todo_freshness_nudge(data: dict) -> None:
    """Once per session, nudge keeping the task/TODO list current.

    Ordinary per-session housekeeping — fires in-session, never as a sub-agent
    and unrelated to the monitor/work-trigger loop. Idempotent via a
    per-session ``<session>.todo-nudged`` marker, mirroring the loop-pending
    precedent. Advisory only: prints additionalContext, never emits a deny,
    so it can never block tool use.
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return
    _ensure_state_dir()
    marker = _state_file(session_id, "todo-nudged")
    if marker.exists():
        return
    marker.write_text("1", encoding="utf-8")
    print(_TODO_FRESHNESS_NUDGE)  # noqa: T201


# ── PreToolUse: enforce-skill-loading ───────────────────────────────


def handle_enforce_skill_loading(data: dict) -> bool:
    """Block Bash/Edit/Write when suggested skills haven't been loaded."""
    session_id = data.get("session_id", "")
    if not session_id:
        return False

    pending_lines = _read_lines(_state_file(session_id, "pending"))
    if not pending_lines:
        return False

    loaded = set(_read_lines(_state_file(session_id, "skills")))
    unloaded = [f"/{s}" for s in pending_lines if s not in loaded]
    if not unloaded:
        return False

    reason = (
        f"SKILL LOADING ENFORCEMENT: You MUST load these skills first: {' '.join(unloaded)}. "
        "Call the Skill tool for each one BEFORE calling Bash/Edit/Write."
    )
    json.dump({"permissionDecision": "deny", "permissionDecisionReason": reason}, sys.stdout)
    return True


# ── PreToolUse: protect-default-branch ─────────────────────────────


_DEFAULT_PROTECTED_BRANCHES = {"main", "master"}


def _load_protected_branches() -> set[str]:
    """Return the merged set of protected branches from defaults + all overlays."""
    import tomllib  # noqa: PLC0415

    branches = set(_DEFAULT_PROTECTED_BRANCHES)
    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return branches
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return branches
    for overlay_cfg in config.get("overlays", {}).values():
        branches.update(overlay_cfg.get("protected_branches", []))
    return branches


def handle_protect_default_branch(data: dict) -> bool:
    """Block Edit/Write on files that live on a protected branch."""
    tool_name = data.get("tool_name", "")
    if tool_name not in _FILE_PATH_TOOLS:
        return False

    file_path = data.get("tool_input", {}).get("file_path", "")
    if not file_path:
        return False

    parent = str(Path(file_path).parent)
    try:
        branch = subprocess.check_output(  # noqa: S603
            ["git", "-C", parent, "--no-optional-locks", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S607
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False

    if branch in _load_protected_branches():
        json.dump(
            {
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"BLOCKED: file is on protected branch '{branch}'. "
                    "Create a worktree first with `t3 workspace ticket`."
                ),
            },
            sys.stdout,
        )
        return True
    return False


# ── PreToolUse: validate-mr-metadata ────────────────────────────────


def _extract_mr_fields(data: dict) -> tuple[str, str]:
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if "glab mr create" not in command and "glab mr update" not in command:
            return "", ""
        title_match = re.search(r"""--title\s+['"]([^'"]+)['"]""", command)
        desc_match = re.search(r"""--description\s+['"]([^'"]+)['"]""", command)
        return (title_match.group(1) if title_match else ""), (desc_match.group(1) if desc_match else "")

    if tool_name in _MR_TOOLS:
        return tool_input.get("title", ""), tool_input.get("description", "")

    return "", ""


def handle_validate_mr_metadata(data: dict) -> bool:
    """Validate MR title/description against project-specific rules."""
    validate_script = os.environ.get("T3_MR_VALIDATE_SCRIPT", "")
    if not validate_script or not Path(validate_script).is_file():
        return False

    title, description = _extract_mr_fields(data)
    if not title:
        return False

    try:
        subprocess.run(  # noqa: S603
            ["python3", validate_script, "--title", title, "--description", description],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except subprocess.CalledProcessError as exc:
        json.dump(
            {"permissionDecision": "deny", "permissionDecisionReason": exc.stdout or exc.stderr},
            sys.stdout,
        )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


# ── PostToolUse: track-active-repo ──────────────────────────────────


def _extract_file_path(data: dict) -> str:
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if tool_name in _FILE_PATH_TOOLS:
        return tool_input.get("file_path", "")
    if tool_name in _PATH_TOOLS:
        return tool_input.get("path", "")
    if tool_name == "Bash":
        match = re.search(r"/(Users|home)/[^ \"]+", tool_input.get("command", ""))
        return match.group() if match else ""
    return ""


def _resolve_repo_key(file_path: str, workspace: str) -> str | None:
    if not file_path.startswith(f"{workspace}/"):
        return None

    relative = file_path[len(workspace) + 1 :]
    parts = relative.split("/")
    first = parts[0]
    main_repo_dir = Path(workspace) / first

    if (main_repo_dir / ".git").is_dir():
        return first

    if len(parts) < 2:  # noqa: PLR2004
        return None
    repo_in_wt = parts[1]
    wt_dir = main_repo_dir / repo_in_wt
    if not (wt_dir / ".git").exists():
        return None
    try:
        branch = subprocess.check_output(  # noqa: S603
            ["git", "-C", str(wt_dir), "--no-optional-locks", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S607
            text=True,
            timeout=3,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return f"{branch}/{repo_in_wt}" if branch else None


def handle_track_active_repo(data: dict) -> None:
    """Track which repos the agent has touched during this session."""
    session_id = data.get("session_id", "")
    if not session_id:
        return

    file_path = _extract_file_path(data)
    if not file_path:
        return

    workspace = os.environ.get("T3_WORKSPACE_DIR", str(Path.home() / "workspace"))
    repo_key = _resolve_repo_key(file_path, workspace)
    if repo_key is None:
        return

    _ensure_state_dir()
    active = _state_file(session_id, "active")
    if repo_key not in set(_read_lines(active)):
        _append_line(active, repo_key)

    # MR cache invalidation
    if data.get("tool_name") == "Bash":
        command = data.get("tool_input", {}).get("command", "")
        if "git push" in command or "glab mr" in command:
            mr_cache = _state_file(session_id, "mr_refreshed")
            if mr_cache.is_file():
                mr_cache.unlink()


# ── PostToolUse + InstructionsLoaded: track-skill-usage ─────────────


def _skill_search_dirs() -> list[Path]:
    """Directories scanned to build the trigger index for closure resolution.

    ``T3_SKILL_SEARCH_DIRS`` (os.pathsep-separated) overrides the defaults —
    used by tests to point at a fixture skill tree. Otherwise: the plugin's
    own ``skills/`` directory plus the agent skill install locations.
    """
    override = os.environ.get("T3_SKILL_SEARCH_DIRS", "")
    if override:
        return [Path(d) for d in override.split(os.pathsep) if d]

    home = os.environ.get("HOME", "")
    candidates = [
        Path(__file__).resolve().parents[2] / "skills",
        Path(home) / ".agents" / "skills",
        Path(home) / ".claude" / "skills",
    ]
    return [d for d in candidates if d.is_dir()]


def _resolve_skill_closure(skills: list[str]) -> list[str]:
    """Expand *skills* to their ``requires:`` dependency closure.

    Uses the real trigger index (parsed from real SKILL.md frontmatter) and
    the real :func:`teatree.skill_deps.resolve_requires` resolver — a loaded
    skill's transitive dependencies are genuinely active and must be tracked.
    Unknown skills (framework skills with no trigger entry) pass through
    unchanged. On any resolution failure, fall back to the input skills so
    tracking never silently drops a genuinely-loaded skill.
    """
    if not skills:
        return []

    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added: list[str] = []
    for extra in (str(scripts_dir), str(src_dir)):
        if extra not in sys.path:
            sys.path.insert(0, extra)
            added.append(extra)
    try:
        from lib.skill_loader import build_trigger_index  # noqa: PLC0415

        from teatree.skill_deps import resolve_requires  # noqa: PLC0415

        index = build_trigger_index(_skill_search_dirs())
        return resolve_requires(skills, index)
    except Exception:  # noqa: BLE001
        return list(skills)
    finally:
        for extra in added:
            with contextlib.suppress(ValueError):
                sys.path.remove(extra)


def _record_skills(skills_file: Path, existing: set[str], skills: list[str]) -> None:
    """Append the resolved closure of *skills*, preserving order, deduped."""
    for name in _resolve_skill_closure(skills):
        if name and name not in existing:
            existing.add(name)
            _append_line(skills_file, name)


def handle_track_skill_usage(data: dict) -> None:
    """Track which skills are active this session, including their closure.

    A genuinely-loaded skill (Skill tool call or InstructionsLoaded entry)
    is expanded to its resolved ``requires:`` dependency closure before
    being recorded, so the statusline reflects the full active set — not
    just the explicitly tool-invoked name (#689). Suggested-but-not-loaded
    skills are never recorded here.
    """
    session_id = data.get("session_id", "")
    if not session_id:
        return

    _ensure_state_dir()
    skills_file = _state_file(session_id, "skills")
    existing = set(_read_lines(skills_file))

    # PostToolUse: single skill from tool_input
    skill_name = data.get("tool_input", {}).get("skill", "")
    if skill_name:
        _record_skills(skills_file, existing, [skill_name])
        return

    # InstructionsLoaded: array of skill objects or skill name strings
    loaded: list[str] = []
    for skill_obj in data.get("skills", []):
        if isinstance(skill_obj, dict):
            name = skill_obj.get("name", "")
        elif isinstance(skill_obj, str):
            name = skill_obj
        else:
            continue
        if name:
            loaded.append(name)
    _record_skills(skills_file, existing, loaded)


# ── PostToolUse: read-dedup ────────────────────────────────────────


def handle_read_dedup(data: dict) -> None:
    """Warn when a file is re-read without having changed since last read."""
    if data.get("tool_name") != "Read":
        return

    session_id = data.get("session_id", "")
    file_path = data.get("tool_input", {}).get("file_path", "")
    if not session_id or not file_path:
        return

    _ensure_state_dir()
    reads_file = _state_file(session_id, "reads")

    # Load existing reads: each line is "mtime\tpath"
    reads: dict[str, str] = {}
    for line in _read_lines(reads_file):
        parts = line.split("\t", 1)
        if len(parts) == 2:  # noqa: PLR2004
            reads[parts[1]] = parts[0]

    # Get current mtime
    try:
        current_mtime = str(Path(file_path).stat().st_mtime)
    except OSError:
        return

    prev_mtime = reads.get(file_path)
    if prev_mtime == current_mtime:
        print(  # noqa: T201
            f"TOKEN SAVINGS HINT: {file_path} was already read this session "
            "and hasn't changed. Use your cached knowledge of its contents "
            "instead of re-reading."
        )

    # Update the reads file (overwrite to keep latest mtime per path)
    reads[file_path] = current_mtime
    reads_file.write_text(
        "\n".join(f"{mtime}\t{path}" for path, mtime in reads.items()) + "\n",
        encoding="utf-8",
    )


# ── PreCompact: retro-before-compact ──────────────────────────────


def handle_pre_compact(data: dict) -> None:
    """Inject retro directive before compaction destroys session knowledge."""
    session_id = data.get("session_id", "")
    if not session_id:
        return

    skills_file = _state_file(session_id, "skills")
    loaded: set[str] = set()
    if skills_file.is_file():
        loaded = {line.strip() for line in skills_file.read_text(encoding="utf-8").splitlines() if line.strip()}

    lifecycle_skills = {"t3:code", "t3:debug", "t3:test", "t3:ship", "t3:review", "t3:ticket"}
    if not (loaded & lifecycle_skills):
        return

    json.dump(
        {
            "additionalContext": (
                "COMPACTION IMMINENT — lifecycle skills were active this session "
                f"({', '.join(sorted(loaded & lifecycle_skills))}). "
                "Run /t3:retro NOW to persist session learnings to memory before "
                "context is compressed. After retro completes, compaction will proceed."
            ),
        },
        sys.stdout,
    )


# ── PostCompact: recover-temp-files ───────────────────────────────


_T3_TEMP_PREFIX = "t3-snapshot-"
_TMP_DIR = Path(tempfile.gettempdir())


def _find_temp_files(session_id: str) -> list[Path]:
    """Find t3 temp files for this session in STATE_DIR and _TMP_DIR."""
    results: list[Path] = []
    session_glob = f"{_T3_TEMP_PREFIX}{session_id}-*.md"
    for search_dir in (STATE_DIR, _TMP_DIR):
        if search_dir.is_dir():
            results.extend(sorted(search_dir.glob(session_glob)))
    if _TMP_DIR.is_dir():
        for f in sorted(_TMP_DIR.glob(f"{_T3_TEMP_PREFIX}*.md")):
            if f not in results:
                results.append(f)
    return results


def handle_post_compact(data: dict) -> None:
    """Inject saved temp files back into context after compaction."""
    session_id = data.get("session_id", "")
    files = _find_temp_files(session_id)
    if not files:
        return

    parts: list[str] = []
    for f in files:
        try:
            content = f.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            parts.append(f"## {f.name}\n\n{content}")

    if not parts:
        return

    context = (
        "PRE-COMPACTION SNAPSHOTS RECOVERED — the following files were saved before "
        "context compaction. Read them to resume where you left off, then delete the "
        "temp files when done:\n\n" + "\n\n---\n\n".join(parts)
    )
    json.dump({"additionalContext": context}, sys.stdout)


_SESSION_END_ORPHAN_TIMEOUT = 4
_SESSION_END_ORPHAN_PREVIEW = 5


def _fetch_orphans() -> list[dict]:
    """Invoke ``t3 teatree workspace list-orphans`` and return its JSON, or ``[]``."""
    import shutil  # noqa: PLC0415

    t3_bin = shutil.which("t3")
    if not t3_bin:
        return []
    try:
        result = subprocess.run(  # noqa: S603
            [t3_bin, "teatree", "workspace", "list-orphans"],
            capture_output=True,
            text=True,
            timeout=_SESSION_END_ORPHAN_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _format_orphan_summary(orphans: list[dict]) -> str:
    """Return a one-line-per-orphan bullet list, truncated to _SESSION_END_ORPHAN_PREVIEW entries."""
    preview = orphans[:_SESSION_END_ORPHAN_PREVIEW]
    lines = [f"  - {o.get('repo', '?')} ({o.get('branch', '?')}, {o.get('ahead_count', 0)} ahead)" for o in preview]
    if len(orphans) > _SESSION_END_ORPHAN_PREVIEW:
        lines.append(f"  - …and {len(orphans) - _SESSION_END_ORPHAN_PREVIEW} more")
    return "\n".join(lines)


def handle_session_end(data: dict) -> None:
    """Suggest retro and surface orphan branches at session close."""
    session_id = data.get("session_id", "")
    if not session_id:
        return

    skills_file = STATE_DIR / f"{session_id}.skills"
    loaded: set[str] = set()
    if skills_file.is_file():
        loaded = {line.strip() for line in skills_file.read_text(encoding="utf-8").splitlines() if line.strip()}

    lifecycle_skills = {"t3:code", "t3:debug", "t3:test", "t3:ship", "t3:review", "t3:ticket"}
    retro_relevant = bool(loaded & lifecycle_skills)

    orphans = _fetch_orphans() if retro_relevant else []

    if not retro_relevant and not orphans:
        return

    parts: list[str] = []
    if retro_relevant:
        parts.append(
            "SESSION ENDING — lifecycle skills were loaded during this session "
            f"({', '.join(sorted(loaded & lifecycle_skills))}). "
            "Consider running /t3:retro to capture learnings before the session ends.",
        )
    if orphans:
        parts.append(
            f"ORPHAN BRANCHES DETECTED ({len(orphans)}) — branches with local work and no open PR:\n"
            f"{_format_orphan_summary(orphans)}\n"
            "Run `t3 teatree pr ensure-pr --branch <name>` to track them, "
            "or `t3 teatree workspace clean-all` to reap synced ones.",
        )

    json.dump({"additionalContext": "\n\n".join(parts)}, sys.stdout)


# ── PostToolUse: track-cron-jobs ──────────────────────────────────────


_LOOP_NAME_MAX = 20


def _clean_token(token: str) -> str:
    """Strip surrounding/trailing punctuation and backticks from a token."""
    return token.strip("`").strip(".,;:!?\"'()[]{}/").strip("`")


def _derive_loop_name(prompt: str) -> str:
    """Derive a short display name from a cron/loop prompt.

    - The canonical teatree loop prompt maps to a stable readable name.
    - Slash-command prompts use the command token.
    - Otherwise a short label is taken from the first meaningful word.

    Surrounding punctuation and backticks are always stripped.
    """
    prompt = prompt.strip()

    # 1. Canonical teatree loop prompt → stable name (it runs `t3 loop tick`).
    if prompt == _LOOP_PROMPT or prompt.startswith(_LOOP_PROMPT):
        return "tick"

    if prompt.startswith("!"):
        prompt = prompt[1:].strip()

    parts = prompt.split()
    if not parts:
        return "loop"

    # `t3 loop <subcommand>` shell form → the subcommand (e.g. `tick`).
    if parts[:2] == ["t3", "loop"] and len(parts) > 2:  # noqa: PLR2004
        return _clean_token(parts[2])[:_LOOP_NAME_MAX] or "loop"

    # 2. Slash-command form: a leading `/foo` or an embedded `/foo` token.
    #    `/loop 5m /babysit-prs` wraps the real command — use the last token.
    slash_tokens = [p for p in parts if p.startswith("/") and len(p) > 1]
    if slash_tokens:
        return _clean_token(slash_tokens[-1].split("/")[-1])[:_LOOP_NAME_MAX] or "loop"

    # 3. Prose: first meaningful word, punctuation/backticks stripped.
    for part in parts:
        cleaned = _clean_token(part)
        if cleaned:
            return cleaned[:_LOOP_NAME_MAX]
    return "loop"


def _load_crons(path: Path) -> dict:
    if not path.is_file():
        return {"jobs": {}, "wakeup": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"jobs": {}, "wakeup": None}


def _save_crons(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def handle_track_cron_jobs(data: dict) -> None:
    """Track CronCreate/CronDelete/ScheduleWakeup for statusline display."""
    tool_name = data.get("tool_name", "")
    if tool_name not in {"CronCreate", "CronDelete", "ScheduleWakeup"}:
        return

    session_id = data.get("session_id", "")
    if not session_id:
        return

    _ensure_state_dir()
    crons_file = _state_file(session_id, "crons")
    state = _load_crons(crons_file)
    if "jobs" not in state:
        state["jobs"] = {}

    import time  # noqa: PLC0415

    now = int(time.time())
    tool_input = data.get("tool_input", {})

    if tool_name == "CronCreate":
        prompt = tool_input.get("prompt", "")
        cron_expr = tool_input.get("cron", "")
        name = _derive_loop_name(prompt)
        job_id = data.get("tool_result", {}).get("id", "") or f"job-{now}"
        cadence = _cron_cadence_seconds(cron_expr)
        state["jobs"][job_id] = {
            "name": name,
            "cron": cron_expr,
            "cadence": cadence,
            "created_at": now,
        }
        _state_file(session_id, "loop-pending").unlink(missing_ok=True)
    elif tool_name == "CronDelete":
        job_id = tool_input.get("id", "")
        state["jobs"].pop(job_id, None)
    elif tool_name == "ScheduleWakeup":
        delay = int(tool_input.get("delaySeconds", 0))
        reason = tool_input.get("reason", "")
        state["wakeup"] = {
            "name": reason[:30] if reason else "loop",
            "next_epoch": now + delay,
        }

    _save_crons(crons_file, state)


def _cron_cadence_seconds(cron_expr: str) -> int | None:
    """Extract cadence in seconds from simple */N minute patterns."""
    parts = cron_expr.strip().split()
    if len(parts) != 5:  # noqa: PLR2004
        return None
    minute = parts[0]
    if minute.startswith("*/") and all(p == "*" for p in parts[1:]):
        try:
            return int(minute[2:]) * 60
        except ValueError:
            return None
    return None


# ── PreToolUse: block-direct-commands ────────────────────────────────


_REMOTE_DUMP_ENV_RE = re.compile(r"\bT3_ALLOW_REMOTE_DUMP\s*=\s*1\b")
_REMOTE_DUMP_DENY_REASON = (
    "BLOCKED: agents must never set `T3_ALLOW_REMOTE_DUMP=1`. "
    "Remote pg_dump over VPN requires explicit human action in a terminal — "
    "the agent cannot opt in. Ask the user to run the command themselves."
)


def _deny_match(command: str) -> str | None:
    """Return a deny reason for *command*, or None if it should pass through."""
    # Checked FIRST — even before t3/read-only bypass — because agents must
    # never opt in to remote pg_dump regardless of the surrounding command.
    if _REMOTE_DUMP_ENV_RE.search(command):
        return _REMOTE_DUMP_DENY_REASON
    stripped = command.lstrip()
    if _T3_CMD_PREFIX_RE.match(stripped) or _READONLY_CMD_PREFIX_RE.match(stripped):
        return None
    for pattern, reason in _BLOCKED_COMMANDS:
        if pattern.search(command):
            return reason + " If `t3` fails, fix the CLI — do not work around it."
    return None


def handle_block_direct_commands(data: dict) -> bool:
    """Block Bash commands that bypass the t3 CLI.

    Returns True when a deny was emitted (caller should stop the handler chain).
    """
    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command:
        return False
    reason = _deny_match(command)
    if reason is None:
        return False
    json.dump({"permissionDecision": "deny", "permissionDecisionReason": reason}, sys.stdout)
    return True


# ── PreToolUse: mirror-question-to-slack ─────────────────────────────


def _slack_config_from_toml() -> tuple[str, str] | None:
    """Return (bot_token_ref, user_id) from the first slack-enabled overlay."""
    import tomllib  # noqa: PLC0415

    config_path = Path.home() / ".teatree.toml"
    if not config_path.is_file():
        return None
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return None
    for overlay_cfg in (config.get("overlays") or {}).values():
        if overlay_cfg.get("messaging_backend") == "slack":
            ref = overlay_cfg.get("slack_token_ref", "")
            uid = overlay_cfg.get("slack_user_id", "")
            if ref and uid:
                return ref, uid
    return None


def _format_question_text(questions: list[dict]) -> str:
    lines: list[str] = []
    for q in questions:
        lines.append(f"*{q.get('question', '')}*")
        for i, opt in enumerate(q.get("options", []), 1):
            label = opt.get("label", "")
            desc = opt.get("description", "")
            lines.append(f"  {i}. {label}" + (f" — {desc}" if desc else ""))
    lines.append("\n_Reply with the number (e.g. `1`) or type your answer._")
    return "\n".join(lines)


def _slack_dm_cache_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / "teatree" / "slack_dm_channels.json"


def _read_dm_channel_cache(user_id: str) -> str:
    path = _slack_dm_cache_path()
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    cached = data.get(user_id)
    return cached if isinstance(cached, str) else ""


def _write_dm_channel_cache(user_id: str, channel: str) -> None:
    path = _slack_dm_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing[user_id] = channel
    path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _slack_open_dm(bot_token: str, user_id: str, *, timeout: float) -> str:
    import urllib.request  # noqa: PLC0415

    payload = json.dumps({"users": user_id}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/conversations.open",
        data=payload,
        headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=timeout).read())  # noqa: S310
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(resp, dict):
        return ""
    channel = resp.get("channel")
    if not isinstance(channel, dict):
        return ""
    cid = channel.get("id")
    return cid if isinstance(cid, str) else ""


def _slack_post_message(bot_token: str, channel: str, text: str, *, timeout: float) -> bool:
    """Post ``text`` to ``channel``. Return True iff Slack acknowledged success."""
    import urllib.request  # noqa: PLC0415

    payload = json.dumps({"channel": channel, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=timeout).read())  # noqa: S310
    except Exception:  # noqa: BLE001
        return False
    return bool(isinstance(resp, dict) and resp.get("ok") is True)


def _slack_post_dm(bot_token: str, user_id: str, text: str, *, timeout: float = 2.0) -> None:
    """Post ``text`` to ``user_id``'s DM. Resolves channel via cache when possible.

    Cache hit → single ``chat.postMessage`` call (sub-second on a normal
    connection, fits inside the 3s hook timeout). Cache miss or
    ``channel_not_found`` → open the DM, cache the channel id, retry.
    """
    cached = _read_dm_channel_cache(user_id)
    if cached and _slack_post_message(bot_token, cached, text, timeout=timeout):
        return
    channel = _slack_open_dm(bot_token, user_id, timeout=timeout)
    if not channel:
        return
    if _slack_post_message(bot_token, channel, text, timeout=timeout):
        _write_dm_channel_cache(user_id, channel)


def _perform_slack_post(slack_cfg: tuple[str, str], questions: list[dict]) -> None:
    """Resolve the bot token and post the question — runs synchronously.

    Synchronous so the Slack DM lands **before** the AskUserQuestion prompt
    renders in the terminal. The previous fork-and-detach variant caused
    the message to arrive *after* the user had already answered.
    """
    token_ref, user_id = slack_cfg
    result = subprocess.run(  # noqa: S603
        ["pass", "show", f"{token_ref}-bot"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    bot_token = result.stdout.strip() if result.returncode == 0 else ""
    if not bot_token:
        return
    _slack_post_dm(bot_token, user_id, _format_question_text(questions))


def _post_question_to_slack(data: dict) -> None:
    questions = data.get("tool_input", {}).get("questions", [])
    if not questions:
        return
    slack_cfg = _slack_config_from_toml()
    if slack_cfg is None:
        return
    _perform_slack_post(slack_cfg, questions)


def handle_mirror_question_to_slack(data: dict) -> bool:
    if data.get("tool_name") != "AskUserQuestion":
        return False
    _post_question_to_slack(data)
    return False


# ── Router ──────────────────────────────────────────────────────────

_HANDLERS: dict[str, list] = {
    "UserPromptSubmit": [
        handle_enforce_loop_on_prompt,
        handle_todo_freshness_nudge,
        handle_user_prompt_submit,
    ],
    "PreToolUse": [
        handle_enforce_loop_registration,
        handle_protect_default_branch,
        handle_enforce_skill_loading,
        handle_block_direct_commands,
        handle_validate_mr_metadata,
        handle_mirror_question_to_slack,
    ],
    "PostToolUse": [
        handle_track_active_repo,
        handle_track_skill_usage,
        handle_track_cron_jobs,
        handle_read_dedup,
    ],
    "InstructionsLoaded": [handle_track_skill_usage],
    "PreCompact": [handle_pre_compact],
    "PostCompact": [handle_post_compact],
    "SessionEnd": [handle_session_end],
}


def main() -> None:
    args = _parse_args()
    handlers = _HANDLERS.get(args.event, [])
    if not handlers:
        return

    data = _read_input()
    if not data:
        return

    for handler in handlers:
        # Handlers that return True emitted a deny — stop the chain to avoid
        # writing multiple JSON objects to stdout (which would be invalid JSON).
        if handler(data) is True:
            break


if __name__ == "__main__":
    main()
