"""t3 setup — first-time and ongoing global skill installation."""

import json
import logging
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import typer

from teatree.cli.dep_drift_repair import repair_dep_drift as _repair_dep_drift
from teatree.cli.doctor import AGENT_SKILL_RUNTIMES, DoctorService, agent_skill_dirs
from teatree.cli.slack_dm_provisioning import provision_all_overlay_dm_channels
from teatree.cli.slack_provision import slack_provision
from teatree.cli.slack_setup import slack_bot_setup
from teatree.cli.slack_user_token_setup import slack_user_token_setup
from teatree.self_update import current_editable_source
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
    so setup targets (``uv tool install --editable`` and Claude plugin
    symlink) land on a stable path.
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

    Thin alias kept on the ``setup`` surface for its callers; the canonical
    implementation lives in :func:`teatree.self_update.current_editable_source`
    so the reinstall path shares one definition (``teatree.loop`` cannot import
    ``teatree.cli``).
    """
    return current_editable_source(uv_bin)


def _run_captured(args: list[str], cwd: Path | None = None) -> CompletedProcess[str]:
    """Run a subprocess, capturing stdout/stderr and never raising on non-zero exit."""
    return run_allowed_to_fail(args, cwd=cwd, expected_codes=None)


_PLUGIN_ID = f"{_PLUGIN_NAME}@{_MARKETPLACE_NAME}"


def _read_json(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _settings_path() -> Path:
    """Return the resolved settings.json path (follows symlinks)."""
    path = Path.home() / ".claude" / "settings.json"
    return path.resolve() if path.is_file() else path


def _register_installed_plugin(repo: Path) -> None:
    """Register t3 in installed_plugins.json with installPath pointing to the main clone."""
    plugins_json = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    data = _read_json(plugins_json)
    data.setdefault("version", 2)
    plugins = data.setdefault("plugins", {})

    target = str(repo.resolve())
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    existing = plugins.get(_PLUGIN_ID, [])
    if existing and existing[0].get("installPath") == target:
        return

    plugins[_PLUGIN_ID] = [
        {
            "scope": "user",
            "installPath": target,
            "version": "local",
            "installedAt": existing[0].get("installedAt", now) if existing else now,
            "lastUpdated": now,
        },
    ]
    _write_json(plugins_json, data)


def _enable_plugin() -> None:
    """Ensure t3@souliane is enabled in settings.json."""
    resolved = _settings_path()
    data = _read_json(resolved)
    plugins = data.setdefault("enabledPlugins", {})
    if plugins.get(_PLUGIN_ID) is True:
        return
    plugins[_PLUGIN_ID] = True
    _write_json(resolved, data)


def _cleanup_legacy_plugin() -> None:
    """Remove legacy symlink-based plugin setup from before marketplace-style registration."""
    plugins_dir = Path.home() / ".claude" / "plugins"
    link = plugins_dir / _PLUGIN_NAME
    if link.is_symlink():
        link.unlink()
        typer.echo(f"OK    Removed legacy plugin symlink: {link}")

    resolved = _settings_path()
    data = _read_json(resolved)
    enabled = data.get("enabledPlugins", {})
    legacy_keys = [k for k in enabled if k.startswith("/") and k.endswith(f"/{_PLUGIN_NAME}")]
    if legacy_keys:
        for key in legacy_keys:
            del enabled[key]
        _write_json(resolved, data)
        typer.echo(f"OK    Removed {len(legacy_keys)} legacy enabledPlugins path entry(ies).")

    cache_root = plugins_dir / "cache" / _MARKETPLACE_NAME / _PLUGIN_NAME
    if cache_root.is_dir():
        shutil.rmtree(cache_root)


def _ensure_marketplace_symlink(repo: Path) -> None:
    """Create ``plugins/t3 -> ..`` inside the repo for marketplace source resolution."""
    plugins_dir = repo / "plugins"
    link = plugins_dir / _PLUGIN_NAME
    if link.is_symlink():
        return
    plugins_dir.mkdir(exist_ok=True)
    link.symlink_to("..")


def _register_marketplace(repo: Path) -> None:
    """Ensure the ``souliane`` marketplace is registered in known_marketplaces.json."""
    _ensure_marketplace_symlink(repo)
    marketplaces_json = Path.home() / ".claude" / "plugins" / "known_marketplaces.json"
    data = _read_json(marketplaces_json)
    target = str(repo.resolve())
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    existing = data.get(_MARKETPLACE_NAME, {})
    if existing.get("installLocation") == target:
        return

    data[_MARKETPLACE_NAME] = {
        "source": {"source": "directory", "path": target},
        "installLocation": target,
        "lastUpdated": now,
    }
    _write_json(marketplaces_json, data)


def _install_claude_plugin(repo: Path) -> bool:
    """Register the t3 plugin pointing directly at the local main clone.

    Uses the same ``installed_plugins.json`` format as marketplace-installed
    plugins so Claude Code treats it identically (namespaced skills, visible
    in ``claude plugin list``).  The ``installPath`` points directly at the
    main clone — no cache copy, always live.
    """
    _cleanup_legacy_plugin()
    _register_marketplace(repo)
    _register_installed_plugin(repo)
    _enable_plugin()
    typer.echo(f"OK    Plugin {_PLUGIN_ID} registered (installPath: {repo.resolve()}).")
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


_APM_FAILURE_MARKERS = ("installation failed", "package failed", "package(s) failed")


def _apm_reported_failure(result: CompletedProcess[str]) -> bool:
    """Whether an apm-install run failed, accounting for apm's exit-0-on-error.

    apm writes its per-package diagnostics to *stdout* and (in some versions)
    exits 0 even when a package fails validation, so a bare returncode check
    misses the failure entirely.  Treat a non-zero exit OR a failure marker in
    either stream as failure.
    """
    if result.returncode != 0:
        return True
    combined = f"{result.stdout}\n{result.stderr}".lower()
    return any(marker in combined for marker in _APM_FAILURE_MARKERS)


def _run_apm_install(repo: Path) -> bool:
    """Run apm install -g --target claude from the teatree repo."""
    apm_path = shutil.which("apm")
    if not apm_path:
        typer.echo("WARN  apm not found — skipping APM dependency installation.")
        typer.echo("      Install: pip install apm-cli (or brew install microsoft/apm/apm)")
        return False

    result = _run_captured([apm_path, "install", "-g", "--target", "claude"], cwd=repo)
    if _apm_reported_failure(result):
        detail = result.stdout.strip() or result.stderr.strip() or "(no output)"
        typer.echo(f"WARN  apm install failed: {detail}")
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


def _managed_skill_roots(default_skills_dir: Path, overlay_targets: list[Path]) -> list[Path]:
    """Source directories teatree owns: the core skills dir + each overlay ``skills/`` root.

    A runtime symlink resolving under one of these roots was created by teatree
    and is safe to prune when its skill no longer exists in source — a user's
    own hand-placed skill never resolves here.
    """
    roots = [default_skills_dir.resolve()]
    roots.extend({target.parent.resolve() for target in overlay_targets})
    return roots


def _prune_stale_skill_links(
    runtime_skills: Path,
    expected_names: set[str],
    managed_roots: list[Path],
    workspace_dir: Path,
) -> int:
    """Remove teatree-managed runtime skill links no longer backed by source.

    A renamed or deleted upstream skill leaves a runtime link whose name is not
    in *expected_names*: either a broken symlink (target removed) or a symlink
    still resolving into a managed source root under the stale name. Both are
    pruned so the dropped skill stops resolving as available after ``t3 setup``
    (which ``t3 update`` re-runs). Contribute-mode workspace links and a user's
    own real skill directories are left untouched.
    """
    pruned = 0
    for link in sorted(runtime_skills.iterdir()):
        if link.name in expected_names or not link.is_symlink():
            continue
        if not link.exists():
            link.unlink()
            pruned += 1
            continue
        try:
            resolved = link.resolve()
        except OSError:
            continue
        if str(resolved).startswith(str(workspace_dir.resolve())):
            continue
        if any(_is_within(resolved, root) for root in managed_roots):
            link.unlink()
            pruned += 1
    return pruned


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _sync_skill_symlinks(
    runtime_skills: Path,
    workspace_dir: Path,
    *,
    sync_core: bool = True,
) -> tuple[int, int]:
    """Create or fix symlinks for core and overlay skills, pruning stale ones.

    When ``sync_core`` is ``False``, core skills are assumed to be delivered via
    a plugin; any existing core symlinks are pruned and only overlays are linked.
    Links for skills removed or renamed upstream — present in the runtime dir but
    absent from the current source — are pruned so they stop resolving.

    Returns ``(created, fixed)`` counts (pruned links are not counted).
    """
    from teatree.agents.skill_bundle import DEFAULT_SKILLS_DIR  # noqa: PLC0415

    created = 0
    fixed = 0

    core_skill_names = {s.name for s in DEFAULT_SKILLS_DIR.iterdir() if (s / "SKILL.md").is_file()}
    if sync_core:
        for skill in sorted(DEFAULT_SKILLS_DIR.iterdir()):
            if not (skill / "SKILL.md").is_file():
                continue
            c, f = _ensure_skill_link(skill, runtime_skills / skill.name, workspace_dir)
            created += c
            fixed += f
    else:
        _remove_core_skill_links(runtime_skills, DEFAULT_SKILLS_DIR)

    overlay_skills = DoctorService.collect_overlay_skills()
    expected_names: set[str] = set(core_skill_names) if sync_core else set()
    for target, link_name in overlay_skills:
        expected_names.add(link_name)
        if link_name in core_skill_names:
            continue
        c, f = _ensure_skill_link(target, runtime_skills / link_name, workspace_dir)
        created += c
        fixed += f

    managed_roots = _managed_skill_roots(DEFAULT_SKILLS_DIR, [target for target, _ in overlay_skills])
    _prune_stale_skill_links(runtime_skills, expected_names, managed_roots, workspace_dir)

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
    skip_plugin: bool = typer.Option(False, "--skip-plugin", help="Skip Claude CLI plugin registration."),
) -> None:
    """Install and configure teatree skills globally.

    Runs APM dependency install, syncs skill symlinks, and links the t3
    plugin into ``~/.claude/plugins/t3``.  Safe to run from a teatree
    worktree — the main clone is resolved via the worktree's ``.git``
    file so the global install stays anchored to a stable path.
    """
    if ctx.invoked_subcommand is not None:
        return
    repo = _validate_repo(_find_main_clone())
    typer.echo(f"Teatree repo: {repo}")

    _repair_dep_drift(repo)
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
        _install_claude_plugin(repo)

    # Per-overlay Slack-bot IM provisioning (#1342) — open
    # ``conversations.open`` once for every Slack-bot overlay that has no
    # ``slack_dm_channel_id`` cached yet, then persist the resulting channel
    # id back to ``~/.teatree.toml``. Without this step a freshly-registered
    # per-overlay bot has no IM with the user, ``messaging_from_overlay``
    # returns a backend that hits ``channel_not_found`` on first DM, and
    # the post silently falls back through whichever bot already had an IM
    # open — conflating per-overlay attribution.
    #
    # Re-derive the path from ``Path.home()`` (rather than importing the
    # frozen ``CONFIG_PATH``) so tests that ``monkeypatch.setattr("pathlib.Path.home", ...)``
    # see the redirected location and never reach the real filesystem.
    provision_all_overlay_dm_channels(
        config_path=Path.home() / ".teatree.toml",
        echo=typer.echo,
    )

    # Suggest (never apply) the recommended per-user auto-mode authorizations.
    # Teatree ships no classifier whitelist of its own — see
    # ``skills/setup/references/recommended-automode-authorizations.md``.
    from teatree.cli.recommended_authorizations import report_missing_authorizations  # noqa: PLC0415

    report_missing_authorizations(typer.echo)

    typer.echo("Done.")


setup_app.command("slack-bot")(slack_bot_setup)
setup_app.command("slack-user-token")(slack_user_token_setup)
setup_app.command("slack-provision")(slack_provision)
