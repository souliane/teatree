"""t3 setup — first-time and ongoing global skill installation."""

import json
import logging
import shutil
from pathlib import Path

import typer

from teatree.cli.doctor import AGENT_SKILL_RUNTIMES, DoctorService, agent_skill_dirs
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
    """Find the teatree main clone (not a worktree)."""
    repo = DoctorService.find_teatree_repo()
    if not repo:
        return None
    # Main clone has .git as a directory; worktrees have .git as a file
    if not (repo / ".git").is_dir():
        return None
    return repo


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


def _ensure_t3_installed(repo: Path) -> bool:
    """Install ``t3`` globally via ``uv tool install`` when it's not on PATH.

    Returns True when the binary is (already, or now) available.  When ``uv``
    itself is missing, prints guidance and returns False rather than raising.
    """
    if shutil.which("t3"):
        return True

    uv_bin = shutil.which("uv")
    if not uv_bin:
        typer.echo("WARN  `t3` not on PATH and `uv` is missing — skipping global install.")
        typer.echo("      Install uv: https://docs.astral.sh/uv/getting-started/installation/")
        return False

    result = _run_captured([uv_bin, "tool", "install", "--editable", str(repo)])
    if result.returncode != 0:
        typer.echo(f"WARN  `uv tool install` failed: {result.stderr.strip()}")
        return False
    typer.echo("OK    Installed `t3` globally via `uv tool install --editable`.")
    if not shutil.which("t3"):
        typer.echo("NOTE  Ensure `~/.local/bin` is on your PATH (see `uv tool dir --bin`).")
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


def _sync_skill_symlinks(claude_skills: Path, workspace_dir: Path) -> tuple[int, int]:
    """Create or fix symlinks for core and overlay skills.

    Returns ``(created, fixed)`` counts.
    """
    from teatree.agents.skill_bundle import DEFAULT_SKILLS_DIR  # noqa: PLC0415

    created = 0
    fixed = 0

    for skill in sorted(DEFAULT_SKILLS_DIR.iterdir()):
        if not (skill / "SKILL.md").is_file():
            continue
        c, f = _ensure_skill_link(skill, claude_skills / skill.name, workspace_dir)
        created += c
        fixed += f

    for target, link_name in DoctorService.collect_overlay_skills():
        c, f = _ensure_skill_link(target, claude_skills / link_name, workspace_dir)
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
        candidate = DoctorService.find_teatree_repo()
        if candidate and not (candidate / ".git").is_dir():
            typer.echo(f"ERROR Running from a git worktree ({candidate}).")
            typer.echo("      Run t3 setup from the main clone instead.")
        else:
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
    *,
    claude_scope: str = typer.Option("user", help="Claude plugin install scope: user or project."),
    skip_plugin: bool = typer.Option(False, "--skip-plugin", help="Skip Claude CLI plugin registration."),
) -> None:
    """Install and configure teatree skills globally.

    Runs APM dependency install, syncs skill symlinks, and registers the
    t3 plugin with Claude Code.  Must be run from the teatree main clone
    (not a worktree).  Consumers without a local clone can bootstrap via
    ``apm install -g souliane/teatree``.
    """
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

    # The Claude skills dir is always ensured (it's where the plugin looks for skills).
    # Other runtimes are opt-in by the presence of their home directory.
    claude_skills = Path.home() / ".claude" / "skills"
    claude_skills.mkdir(parents=True, exist_ok=True)

    for label, skills_dir in agent_skill_dirs():
        if not skills_dir.is_dir():
            continue
        removed = _remove_excluded_skills(skills_dir, all_excluded)
        if removed:
            typer.echo(f"OK    {label}: removed {removed} excluded skill(s).")

        created, fixed = _sync_skill_symlinks(skills_dir, workspace_dir)
        typer.echo(f"OK    {label}: {created} created, {fixed} fixed.")

        broken = _clean_broken_symlinks(skills_dir)
        if broken:
            typer.echo(f"OK    {label}: removed {broken} broken symlink(s).")

    if not skip_plugin:
        _install_claude_plugin(repo, scope=claude_scope)

    typer.echo("Done.")
