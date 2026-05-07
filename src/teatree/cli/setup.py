"""t3 setup — first-time and ongoing global skill installation."""

import json
import logging
import os
import re
import shutil
import tomllib
from pathlib import Path

import typer

from teatree.cli.doctor import AGENT_SKILL_RUNTIMES, DoctorService, agent_skill_dirs
from teatree.cli.slack_setup import slack_bot_setup
from teatree.utils.run import CompletedProcess, run_allowed_to_fail

# Re-exported here so external callers and tests see a single import path for
# setup-adjacent knobs; the canonical definition lives in ``doctor`` to keep
# ``setup → doctor`` imports one-directional.
__all__ = ["AGENT_SKILL_RUNTIMES", "agent_skill_dirs"]

# Skills that conflict with teatree's multi-repo architecture.
# Always excluded — not user-configurable.  Users can add extra
# exclusions via ``excluded_skills`` in ``~/.teatree.toml``.
CORE_EXCLUDED_SKILLS = ["using-superpowers", "using-git-worktrees"]

_PLUGIN_NAME = "t3"
_MARKETPLACE_NAME = "souliane"


logger = logging.getLogger(__name__)

setup_app = typer.Typer(
    help="First-time setup and global skill management.",
    invoke_without_command=True,
)


def _find_main_clone() -> Path | None:
    """Find the teatree main clone, resolving worktrees to their main clone.

    The ``T3_REPO`` env var (set in the user's ``~/.teatree`` shell config)
    wins over cwd heuristics so that ``t3 setup`` run from a worktree still
    targets the configured main clone.  When unset, fall back to
    ``DoctorService.find_teatree_repo`` (cwd → ``find_project_root``); if
    that returns a worktree, follow its ``.git`` file back to the main clone
    so setup targets (``uv tool install --editable`` and Claude marketplace
    registration) land on a stable path.
    """
    env_path = os.environ.get("T3_REPO", "")
    if env_path:
        candidate = Path(env_path).expanduser()
        if (candidate / "pyproject.toml").is_file() and (candidate / ".git").is_dir():
            return candidate

    repo = DoctorService.find_teatree_repo()
    if not repo:
        return None
    git = repo / ".git"
    if git.is_dir():
        return repo
    if git.is_file():
        match = re.match(r"^gitdir:\s*(.+)$", git.read_text().strip())
        if not match:
            return None
        # `.git` points to `<main-clone>/.git/worktrees/<name>`; step back up to main clone.
        main_clone_git = Path(match.group(1)).parent.parent
        if main_clone_git.name == ".git" and main_clone_git.is_dir():
            return main_clone_git.parent
    return None


def _current_editable_source(uv_bin: str) -> Path | None:
    """Return the editable source recorded in uv's teatree tool receipt, or None.

    Returns None when teatree isn't installed as a uv tool, when it's installed
    non-editable (regular PyPI-style install), or when the receipt is
    unparsable.  ``~/.local/share/uv/tools/teatree/uv-receipt.toml`` looks like::

        [tool]
        requirements = [{ name = "teatree", editable = "/path/to/clone" }]
    """
    result = _run_captured([uv_bin, "tool", "dir"])
    if result.returncode != 0 or not result.stdout.strip():
        return None
    receipt = Path(result.stdout.strip()) / "teatree" / "uv-receipt.toml"
    if not receipt.is_file():
        return None
    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return None
    for req in data.get("tool", {}).get("requirements", []):
        if req.get("name") == "teatree":
            editable = req.get("editable")
            return Path(editable) if editable else None
    return None


def _run_captured(args: list[str], cwd: Path | None = None) -> CompletedProcess[str]:
    """Run a subprocess, capturing stdout/stderr and never raising on non-zero exit."""
    return run_allowed_to_fail(args, cwd=cwd, expected_codes=None)


def _register_claude_marketplace(claude_bin: str, repo: Path) -> bool:
    """Register the teatree repo as a local Claude Code marketplace (idempotent)."""
    result = _run_captured([claude_bin, "plugin", "marketplace", "add", str(repo)])
    return result.returncode == 0 or "already" in result.stderr.lower()


def _install_claude_plugin(repo: Path, *, scope: str) -> bool:
    """Register the marketplace and install the t3 plugin via the Claude CLI."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        typer.echo("WARN  Claude CLI not found — skipping plugin install.")
        typer.echo("      Install: https://github.com/anthropics/claude-code")
        return False

    if not _register_claude_marketplace(claude_bin, repo):
        typer.echo("WARN  Failed to register local marketplace — skipping plugin install.")
        return False

    plugin_id = f"{_PLUGIN_NAME}@{_MARKETPLACE_NAME}"
    result = _run_captured([claude_bin, "plugin", "install", plugin_id, "--scope", scope])
    if result.returncode != 0:
        typer.echo(f"WARN  Claude plugin install failed: {result.stderr.strip()}")
        return False
    typer.echo(f"OK    Installed {plugin_id} via Claude CLI ({scope} scope).")
    return True


def _uv_tool_bin_dir(uv_bin: str) -> Path | None:
    """Return the directory ``uv tool`` installs binaries into, or None on error."""
    result = _run_captured([uv_bin, "tool", "dir", "--bin"])
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).expanduser()


def _print_path_hint(bin_dir: Path | None) -> None:
    """Print a shell-rc instruction when the uv tool bin dir is not on PATH."""
    target = bin_dir or Path.home() / ".local" / "bin"
    typer.echo(f"NOTE  `{target}` is not on your PATH.")
    typer.echo(f'      Add to your shell rc (~/.zshrc or ~/.bashrc): export PATH="{target}:$PATH"')


def _ensure_t3_installed(repo: Path) -> bool:
    """Ensure a healthy global ``t3`` via ``uv tool install``.

    Steady-state: if ``t3`` is on PATH and the editable source recorded by uv
    still exists, leave the install alone.  This preserves intentional
    worktree-dogfood installs (see #397 Part 3) and non-editable installs.

    Repair path: when the editable source has been deleted (e.g. the worktree
    it was installed from got cleaned up), reinstall editable from *repo* so
    the global ``t3`` is re-anchored at a stable main clone.
    """
    uv_bin = shutil.which("uv")
    t3_on_path = shutil.which("t3") is not None

    if t3_on_path and uv_bin:
        source = _current_editable_source(uv_bin)
        if source is None or source.is_dir():
            return True
        typer.echo(f"NOTE  Global `t3` editable source missing: {source}")
        typer.echo(f"      Re-anchoring at main clone {repo}.")
    elif t3_on_path:
        return True

    if not uv_bin:
        typer.echo("WARN  `t3` not on PATH and `uv` is missing — skipping global install.")
        typer.echo("      Install uv: https://docs.astral.sh/uv/getting-started/installation/")
        return False

    result = _run_captured([uv_bin, "tool", "install", "--force", "--editable", str(repo)])
    if result.returncode != 0:
        typer.echo(f"WARN  `uv tool install` failed: {result.stderr.strip()}")
        return False
    typer.echo("OK    Installed `t3` globally via `uv tool install --editable`.")
    if not shutil.which("t3"):
        _print_path_hint(_uv_tool_bin_dir(uv_bin))
    return True


def _run_apm_install(repo: Path) -> bool:
    """Run apm install -g --target claude from the teatree repo."""
    apm_path = shutil.which("apm")
    if not apm_path:
        typer.echo("WARN  apm not found — skipping APM dependency installation.")
        typer.echo("      Install: pip install apm-cli (or brew install microsoft/apm/apm)")
        return False

    result = _run_captured([apm_path, "install", "-g", "--target", "claude"], cwd=repo)
    if result.returncode != 0:
        typer.echo(f"WARN  apm install failed: {result.stderr.strip()}")
        return False
    typer.echo("OK    APM dependencies installed globally.")
    return True


def _strip_apm_hooks(settings_path: Path) -> int:
    """Remove hook entries injected by APM (_apm_source key) from settings.json."""
    if not settings_path.is_file():
        return 0

    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0

    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return 0

    removed = 0
    keys_to_check = list(hooks.keys())
    for key in keys_to_check:
        entries = hooks[key]
        if isinstance(entries, list):
            original_len = len(entries)
            hooks[key] = [e for e in entries if not (isinstance(e, dict) and "_apm_source" in e)]
            removed += original_len - len(hooks[key])
            if not hooks[key]:
                del hooks[key]

    if not hooks and "hooks" in data:
        del data["hooks"]

    if removed > 0:
        settings_path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")

    return removed


def _remove_excluded_skills(claude_skills: Path, excluded: list[str]) -> int:
    """Remove excluded skill symlinks/directories from ~/.claude/skills/."""
    removed = 0
    for name in excluded:
        if "/" in name or name.startswith("."):
            logger.warning("Ignoring suspicious excluded_skills entry: %r", name)
            continue
        skill_path = claude_skills / name
        if skill_path.is_symlink():
            skill_path.unlink()
            removed += 1
        elif skill_path.is_dir():
            shutil.rmtree(skill_path)
            removed += 1
    return removed


def _ensure_skill_link(
    target: Path,
    link: Path,
    workspace_dir: Path,
) -> tuple[int, int]:
    """Ensure *link* points to *target*, respecting contribute-mode workspace links.

    Returns ``(created, fixed)`` counts.
    """
    if link.is_symlink():
        try:
            resolved = link.resolve()
            if str(resolved).startswith(str(workspace_dir)):
                return 0, 0  # Contribute-mode link, don't touch
        except OSError:
            pass
        if link.resolve() == target.resolve():
            return 0, 0  # Already correct
        link.unlink()
        link.symlink_to(target)
        return 0, 1
    if link.exists():
        return 0, 0  # Real directory, don't touch
    link.symlink_to(target)
    return 1, 0


def _remove_core_skill_links(runtime_skills: Path, core_skills_dir: Path) -> int:
    """Remove symlinks in *runtime_skills* whose names match a core skill.

    Used by runtimes where core skills are delivered via a plugin (e.g. Claude),
    to prune duplicates inherited from earlier symlink-based installs.
    """
    removed = 0
    for skill in sorted(core_skills_dir.iterdir()):
        if not (skill / "SKILL.md").is_file():
            continue
        link = runtime_skills / skill.name
        if link.is_symlink():
            link.unlink()
            removed += 1
    return removed


def _sync_skill_symlinks(
    runtime_skills: Path,
    workspace_dir: Path,
    *,
    sync_core: bool = True,
) -> tuple[int, int]:
    """Create or fix symlinks for core and overlay skills.

    When ``sync_core`` is ``False``, core skills are assumed to be delivered via
    a plugin; any existing core symlinks are pruned and only overlays are linked.

    Returns ``(created, fixed)`` counts (pruned links are not counted).
    """
    from teatree.agents.skill_bundle import DEFAULT_SKILLS_DIR  # noqa: PLC0415

    created = 0
    fixed = 0

    if sync_core:
        for skill in sorted(DEFAULT_SKILLS_DIR.iterdir()):
            if not (skill / "SKILL.md").is_file():
                continue
            c, f = _ensure_skill_link(skill, runtime_skills / skill.name, workspace_dir)
            created += c
            fixed += f
    else:
        _remove_core_skill_links(runtime_skills, DEFAULT_SKILLS_DIR)

    for target, link_name in DoctorService.collect_overlay_skills():
        c, f = _ensure_skill_link(target, runtime_skills / link_name, workspace_dir)
        created += c
        fixed += f

    return created, fixed


def _clean_broken_symlinks(claude_skills: Path) -> int:
    """Remove broken symlinks from the skills directory."""
    broken = 0
    for link in claude_skills.iterdir():
        if link.is_symlink() and not link.exists():
            link.unlink()
            broken += 1
    return broken


def _validate_repo(repo: Path | None) -> Path:
    """Validate the teatree repo is a main clone with apm.yml. Raises typer.Exit on failure."""
    if not repo:
        typer.echo("ERROR Teatree main clone not found.")
        typer.echo("      Set T3_REPO env var or install teatree in editable mode.")
        typer.echo("      Consumers without a local clone: use `apm install -g souliane/teatree`.")
        raise typer.Exit(code=1)

    if not (repo / "apm.yml").is_file():
        typer.echo(f"ERROR apm.yml not found at {repo}")
        raise typer.Exit(code=1)

    return repo


@setup_app.callback()
def run(
    ctx: typer.Context,
    *,
    claude_scope: str = typer.Option("user", help="Claude plugin install scope: user or project."),
    skip_plugin: bool = typer.Option(False, "--skip-plugin", help="Skip Claude CLI plugin registration."),
) -> None:
    """Install and configure teatree skills globally.

    Runs APM dependency install, syncs skill symlinks, and registers the
    t3 plugin with Claude Code.  Safe to run from a teatree worktree — the
    main clone is resolved via the worktree's ``.git`` file so the global
    install stays anchored to a stable path.  Consumers without a local
    clone can bootstrap via ``apm install -g souliane/teatree``.
    """
    if ctx.invoked_subcommand is not None:
        return
    repo = _validate_repo(_find_main_clone())
    typer.echo(f"Teatree repo: {repo}")

    _ensure_t3_installed(repo)

    _run_apm_install(repo)

    stripped = _strip_apm_hooks(Path.home() / ".claude" / "settings.json")
    if stripped:
        typer.echo(f"OK    Stripped {stripped} APM-injected hook(s) from settings.json.")

    from teatree.config import load_config  # noqa: PLC0415

    config = load_config()

    all_excluded = list(dict.fromkeys(CORE_EXCLUDED_SKILLS + config.user.excluded_skills))
    workspace_dir = Path(config.user.workspace_dir).expanduser()

    # Ensure the Claude skills dir exists so overlay symlinks have a target.
    # Core skills reach Claude via the t3 plugin, not via this directory.
    claude_skills = Path.home() / ".claude" / "skills"
    claude_skills.mkdir(parents=True, exist_ok=True)

    for label, skills_dir in agent_skill_dirs():
        if not skills_dir.is_dir():
            continue
        removed = _remove_excluded_skills(skills_dir, all_excluded)
        if removed:
            typer.echo(f"OK    {label}: removed {removed} excluded skill(s).")

        sync_core = label != "claude"
        created, fixed = _sync_skill_symlinks(skills_dir, workspace_dir, sync_core=sync_core)
        suffix = "" if sync_core else " (core skills via plugin)"
        typer.echo(f"OK    {label}: {created} created, {fixed} fixed{suffix}.")

        broken = _clean_broken_symlinks(skills_dir)
        if broken:
            typer.echo(f"OK    {label}: removed {broken} broken symlink(s).")

    if not skip_plugin:
        _install_claude_plugin(repo, scope=claude_scope)

    typer.echo("Done.")


setup_app.command("slack-bot")(slack_bot_setup)
