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
import json
import os
import re
import subprocess  # noqa: S404
import sys
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
_T3_CMD_PREFIX_RE = re.compile(
    r"^(?:\w+=\S+\s+)*(?:uv\s+run\s+)?t3\s",
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
        re.compile(r"manage\.py\s+runserver"),
        "BLOCKED: `manage.py runserver` — use `t3 <overlay> lifecycle start` instead.",
    ),
    (
        re.compile(r"manage\.py\s+migrate"),
        "BLOCKED: `manage.py migrate` — use `t3 <overlay> lifecycle setup` instead.",
    ),
    (
        re.compile(r"\bnx\s+serve\b"),
        "BLOCKED: `nx serve` — use `t3 <overlay> lifecycle start` instead.",
    ),
    (
        re.compile(r"\bdocker\s+compose\s+(?:up|start)\b"),
        "BLOCKED: `docker compose up/start` — use `t3 <overlay> lifecycle start` instead.",
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
        "BLOCKED: `npm run` — use `t3 <overlay> run frontend` instead.",
    ),
    (
        re.compile(r"\b(?:pipenv|pip)\s+install\b"),
        "BLOCKED: `pip/pipenv install` — use `t3 <overlay> lifecycle setup` instead.",
    ),
    (
        re.compile(r"\b(?:pg_restore|pg_dump|dslr)\b"),
        "BLOCKED: `pg_restore`/`pg_dump`/`dslr` — use `t3 <overlay> db refresh` instead.",
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


def handle_track_skill_usage(data: dict) -> None:
    """Track which skills have been invoked in this session."""
    session_id = data.get("session_id", "")
    if not session_id:
        return

    _ensure_state_dir()
    skills_file = _state_file(session_id, "skills")
    existing = set(_read_lines(skills_file))

    # PostToolUse: single skill from tool_input
    skill_name = data.get("tool_input", {}).get("skill", "")
    if skill_name:
        if skill_name not in existing:
            _append_line(skills_file, skill_name)
        return

    # InstructionsLoaded: array of skill objects or skill name strings
    for skill_obj in data.get("skills", []):
        if isinstance(skill_obj, dict):
            name = skill_obj.get("name", "")
        elif isinstance(skill_obj, str):
            name = skill_obj
        else:
            continue
        if name and name not in existing:
            existing.add(name)
            _append_line(skills_file, name)


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


# ── PostCompact: recover-temp-files ───────────────────────────────


_T3_TEMP_PREFIX = "t3-snapshot-"


def _find_temp_files(session_id: str) -> list[Path]:
    """Find t3 temp files for this session in STATE_DIR and /tmp."""
    results: list[Path] = []
    session_glob = f"{_T3_TEMP_PREFIX}{session_id}-*.md"
    for search_dir in (STATE_DIR, Path("/tmp")):  # noqa: S108
        if search_dir.is_dir():
            results.extend(sorted(search_dir.glob(session_glob)))
    # Also pick up any sessionless t3-snapshot files in /tmp (legacy retro pattern)
    tmp = Path("/tmp")  # noqa: S108
    if tmp.is_dir():
        for f in sorted(tmp.glob(f"{_T3_TEMP_PREFIX}*.md")):
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


def handle_session_end(data: dict) -> None:
    """Suggest retro when lifecycle skills were loaded during the session."""
    session_id = data.get("session_id", "")
    if not session_id:
        return

    skills_file = STATE_DIR / f"{session_id}.skills"
    if not skills_file.is_file():
        return

    loaded = {line.strip() for line in skills_file.read_text(encoding="utf-8").splitlines() if line.strip()}

    lifecycle_skills = {"t3:code", "t3:debug", "t3:test", "t3:ship", "t3:review", "t3:ticket"}
    if not loaded & lifecycle_skills:
        return

    json.dump(
        {
            "additionalContext": (
                "SESSION ENDING — lifecycle skills were loaded during this session "
                f"({', '.join(sorted(loaded & lifecycle_skills))}). "
                "Consider running /t3:retro to capture learnings before the session ends."
            ),
        },
        sys.stdout,
    )


# ── PreToolUse: block-direct-commands ────────────────────────────────


def handle_block_direct_commands(data: dict) -> bool:
    """Block Bash commands that bypass the t3 CLI.

    Returns True when a deny was emitted (caller should stop the handler chain).
    """
    if data.get("tool_name") != "Bash":
        return False

    command = data.get("tool_input", {}).get("command", "")
    if not command:
        return False

    stripped = command.lstrip()

    # Never block legitimate t3 invocations.
    if _T3_CMD_PREFIX_RE.match(stripped):
        return False

    # Never block read-only commands that may mention tools in arguments.
    if _READONLY_CMD_PREFIX_RE.match(stripped):
        return False

    for pattern, reason in _BLOCKED_COMMANDS:
        if pattern.search(command):
            suffix = " If `t3` fails, fix the CLI — do not work around it."
            json.dump(
                {"permissionDecision": "deny", "permissionDecisionReason": reason + suffix},
                sys.stdout,
            )
            return True

    return False


# ── Router ──────────────────────────────────────────────────────────

_HANDLERS: dict[str, list] = {
    "UserPromptSubmit": [handle_user_prompt_submit],
    "PreToolUse": [
        handle_protect_default_branch,
        handle_enforce_skill_loading,
        handle_block_direct_commands,
        handle_validate_mr_metadata,
    ],
    "PostToolUse": [handle_track_active_repo, handle_track_skill_usage, handle_read_dedup],
    "InstructionsLoaded": [handle_track_skill_usage],
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
