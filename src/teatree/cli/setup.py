"""t3 setup — first-time and ongoing global skill installation."""

import json
import logging
import shutil
import subprocess  # noqa: S404
from pathlib import Path

import typer

from teatree.cli.doctor import DoctorService

# Skills that conflict with teatree's multi-repo architecture.
# Always excluded — not user-configurable.  Users can add extra
# exclusions via ``excluded_skills`` in ``~/.teatree.toml``.
CORE_EXCLUDED_SKILLS = ["using-superpowers", "using-git-worktrees"]

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


def _run_apm_install(repo: Path) -> bool:
    """Run apm install -g --target claude from the teatree repo."""
    apm_path = shutil.which("apm")
    if not apm_path:
        typer.echo("WARN  apm not found — skipping APM dependency installation.")
        typer.echo("      Install: pip install apm-cli (or brew install microsoft/apm/apm)")
        return False

    result = subprocess.run(  # noqa: S603
        [apm_path, "install", "-g", "--target", "claude"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(repo),
    )
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
def run() -> None:
    """Install and configure teatree skills globally.

    Must be run from the teatree main clone (not a worktree).
    For consumers without a local clone: use ``apm install -g souliane/teatree``.
    """
    repo = _validate_repo(_find_main_clone())
    typer.echo(f"Teatree repo: {repo}")

    _run_apm_install(repo)

    stripped = _strip_apm_hooks(Path.home() / ".claude" / "settings.json")
    if stripped:
        typer.echo(f"OK    Stripped {stripped} APM-injected hook(s) from settings.json.")

    from teatree.config import load_config  # noqa: PLC0415

    config = load_config()

    claude_skills = Path.home() / ".claude" / "skills"
    claude_skills.mkdir(parents=True, exist_ok=True)

    all_excluded = list(dict.fromkeys(CORE_EXCLUDED_SKILLS + config.user.excluded_skills))
    removed = _remove_excluded_skills(claude_skills, all_excluded)
    if removed:
        typer.echo(f"OK    Removed {removed} excluded skill(s).")

    workspace_dir = Path(config.user.workspace_dir).expanduser()
    created, fixed = _sync_skill_symlinks(claude_skills, workspace_dir)
    typer.echo(f"OK    Skills: {created} created, {fixed} fixed.")

    broken = _clean_broken_symlinks(claude_skills)
    if broken:
        typer.echo(f"OK    Removed {broken} broken symlink(s).")

    typer.echo("Done.")
