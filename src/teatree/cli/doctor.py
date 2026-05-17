"""Doctor CLI commands — smoke-test hooks, imports, services."""

import json
import os
import re
import shutil
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import typer

from teatree.cli.recommended_authorizations import authorizations, report_missing_authorizations
from teatree.utils.run import run_allowed_to_fail

doctor_app = typer.Typer(no_args_is_help=True, help="Smoke-test hooks, imports, services.")
doctor_app.command()(authorizations)
_REQUIRED_TOOLS = ("direnv", "git", "jq")
_CLAUDE_PLUGIN_ID = "t3@souliane"

# Agent runtimes that consume teatree skills.  ``t3 setup`` creates symlinks
# into each runtime's skills directory that already exists — missing dirs are
# skipped silently.  The Claude dir is always ensured by setup.
AGENT_SKILL_RUNTIMES: tuple[str, ...] = ("claude", "codex")


def agent_skill_dirs() -> list[tuple[str, Path]]:
    """Return (runtime_label, skills_dir) pairs, resolved against the current HOME."""
    return [(name, Path.home() / f".{name}" / "skills") for name in AGENT_SKILL_RUNTIMES]


_DEV_SOURCES_FILE = ".t3-dev-sources"


def _find_host_project_root() -> Path | None:
    """Walk up from cwd to find the host project (directory with manage.py + pyproject.toml)."""
    for directory in [Path.cwd(), *Path.cwd().parents]:
        if (directory / "manage.py").is_file() and (directory / "pyproject.toml").is_file():
            return directory
    return None


def _find_teatree_pyproject_from_cwd() -> Path | None:
    """Return the teatree repo rooted at cwd, if any.

    Walks up from cwd looking for a ``pyproject.toml`` whose ``[project].name`` is
    ``teatree``.  Lets dogfood worktrees override ``T3_REPO`` so that running
    ``t3`` from a worktree reinstalls editable from the worktree, not the main clone.
    """
    for directory in [Path.cwd(), *Path.cwd().parents]:
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            if re.search(r'^\s*name\s*=\s*"teatree"', pyproject.read_text(), re.MULTILINE):
                return directory
        except OSError:
            pass
        return None
    return None


def _patch_uv_source(pyproject: Path, package: str, repo_path: Path) -> bool:
    """Rewrite the ``[tool.uv.sources]`` entry for *package* to a local editable path."""
    text = pyproject.read_text(encoding="utf-8")
    # Match: package = { git = "...", branch = "..." } or package = { ... }
    pattern = rf"^({re.escape(package)}\s*=\s*)\{{[^}}]+\}}"
    relative = os.path.relpath(repo_path, pyproject.parent)
    replacement = rf'\g<1>{{ path = "{relative}", editable = true }}'
    new_text, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count == 0:
        return False
    pyproject.write_text(new_text, encoding="utf-8")
    return True


def _write_dev_sources_marker(marker: Path, package: str, repo_path: Path) -> None:
    """Append or update a line in the ``.t3-dev-sources`` marker file."""
    lines: list[str] = []
    if marker.is_file():
        lines = [ln for ln in marker.read_text(encoding="utf-8").splitlines() if not ln.startswith(f"{package}=")]
    lines.append(f"{package}={repo_path}")
    marker.write_text("\n".join(lines) + "\n", encoding="utf-8")


class DoctorService:
    """Health checks and repair for a TeaTree installation."""

    @staticmethod
    def show_info() -> None:
        """Display t3 entry point, teatree/overlay sources, and editable status."""
        from teatree.config import discover_active_overlay, discover_overlays  # noqa: PLC0415

        t3_bin = shutil.which("t3") or "not found on PATH"
        teatree_editable, _teatree_url = IntrospectionHelpers.editable_info("teatree")
        editable_label = " (editable)" if teatree_editable else ""
        typer.echo(f"t3 entry point:   {t3_bin}{editable_label}")
        typer.echo(f"Python:           {sys.executable}")
        typer.echo()

        IntrospectionHelpers.print_package_info("teatree", "teatree")

        active = discover_active_overlay()
        if active:
            typer.echo(f"Active overlay:   {active.name} ({active.overlay_class or '(cwd)'})")
        else:
            typer.echo("Active overlay:   (none)")

        installed = discover_overlays()
        if installed:
            typer.echo()
            typer.echo("Installed overlays:")
            for entry in installed:
                typer.echo(f"  {entry.name:<20}{entry.overlay_class or '(local)'}")
                if entry.project_path:
                    typer.echo(f"  {'':<20}{entry.project_path}")

        plugin = DoctorService.find_installed_claude_plugin()
        typer.echo()
        if plugin:
            typer.echo(f"Claude plugin:    {_CLAUDE_PLUGIN_ID} v{plugin['version']}")
            typer.echo(f"                  {plugin['installPath']} ({plugin['scope']} scope)")
        else:
            typer.echo(f"Claude plugin:    {_CLAUDE_PLUGIN_ID} (not installed)")

        existing = [(label, path) for label, path in agent_skill_dirs() if path.is_dir()]
        if existing:
            typer.echo()
            typer.echo("Skills installed to:")
            for _label, path in existing:
                count = sum(1 for link in path.iterdir() if link.is_symlink())
                typer.echo(f"  {path} ({count} t3-managed)")

    @staticmethod
    def find_installed_claude_plugin() -> dict[str, str] | None:
        """Return plugin version/installPath/scope, or None when not installed."""
        plugins_json = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
        if plugins_json.is_file():
            try:
                data = json.loads(plugins_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
            entries = (data.get("plugins") or {}).get(_CLAUDE_PLUGIN_ID) or []
            if entries:
                first = entries[0]
                return {
                    "version": first.get("version", ""),
                    "installPath": first.get("installPath", ""),
                    "scope": first.get("scope", ""),
                }
        # Legacy: symlink in plugins dir (pre-marketplace registration).
        link = Path.home() / ".claude" / "plugins" / "t3"
        if link.is_symlink():
            return {
                "version": "(legacy-symlink)",
                "installPath": str(link.resolve()),
                "scope": "symlink",
            }
        return None

    @staticmethod
    def collect_overlay_skills() -> list[tuple[Path, str]]:
        """Discover skill directories from registered overlay projects.

        Returns (target_dir, link_name) pairs for symlink creation.
        """
        from teatree.config import discover_overlays  # noqa: PLC0415

        results: list[tuple[Path, str]] = []
        for entry in discover_overlays():
            if not entry.project_path or not entry.project_path.is_dir():
                continue
            project = entry.project_path.expanduser()

            # New convention: skills/ directory
            project_skills = project / "skills"
            if project_skills.is_dir():
                results.extend(
                    (skill, skill.name) for skill in sorted(project_skills.iterdir()) if (skill / "SKILL.md").is_file()
                )

        return results

    @staticmethod
    def repair_symlinks(skills_dir: Path, claude_skills: Path) -> tuple[int, int]:
        """Create or fix symlinks for core and overlay skills. Returns (created, fixed)."""
        created = 0
        fixed = 0

        def _ensure(target: Path, link: Path) -> None:
            nonlocal created, fixed
            if link.is_symlink():
                if link.resolve() == target.resolve():
                    return
                link.unlink()
                fixed += 1
            elif link.exists():
                return  # real directory, don't touch
            link.symlink_to(target)
            created += 1

        for skill in sorted(skills_dir.iterdir()):  # pragma: no branch
            if (skill / "SKILL.md").is_file():
                _ensure(skill, claude_skills / skill.name)

        for target, link_name in DoctorService.collect_overlay_skills():
            _ensure(target, claude_skills / link_name)

        return created, fixed

    @staticmethod
    def check_editable_sanity() -> list[str]:
        """Verify editable status matches ``contribute = true`` in config.

        When ``contribute = true`` in ``~/.teatree.toml``, both teatree core
        and the active overlay should be editable.  Auto-fixes by running
        ``uv pip install -e <repo>``.
        """
        problems: list[str] = []

        from teatree.config import load_config  # noqa: PLC0415

        contribute = load_config().user.contribute

        # Teatree core
        teatree_is_editable, _ = IntrospectionHelpers.editable_info("teatree")
        if contribute and not teatree_is_editable:
            teatree_repo = DoctorService.find_teatree_repo()
            if teatree_repo:
                DoctorService.make_editable("teatree", teatree_repo)
            else:
                problems.append(
                    "contribute=true but teatree is not editable and local repo not found. "
                    "Fix: set T3_REPO env var or run `uv pip install -e <teatree-path>`",
                )

        # Overlays — resolve dist names from entry points metadata (no Django needed).
        import importlib.metadata  # noqa: PLC0415

        overlay_dists = [
            ep.dist.name if ep.dist else ep.name for ep in importlib.metadata.entry_points(group="teatree.overlays")
        ]

        for overlay_dist in overlay_dists:
            overlay_is_editable, _ = IntrospectionHelpers.editable_info(overlay_dist)
            if contribute and not overlay_is_editable:
                overlay_repo = DoctorService.find_overlay_repo(overlay_dist)
                if overlay_repo:
                    DoctorService.make_editable(overlay_dist, overlay_repo)
                else:
                    problems.append(
                        f"contribute=true but overlay ({overlay_dist}) is not editable and repo not found. "
                        f"Fix: run `uv pip install -e <{overlay_dist}-path>`",
                    )
            elif not contribute and overlay_is_editable:
                problems.append(
                    f"Overlay ({overlay_dist}) is editable but contribute=false. "
                    f"Fix: set contribute=true or run `uv pip install {overlay_dist}`.",
                )

        # Reverse check: teatree editable but contribute=false
        if not contribute and teatree_is_editable:
            problems.append(
                "teatree is editable but contribute=false in ~/.teatree.toml. "
                "You risk accidentally modifying framework code. "
                "Fix: set contribute=true or run `uv pip install teatree`.",
            )

        return problems

    @staticmethod
    def find_teatree_repo() -> Path | None:
        cwd_worktree = _find_teatree_pyproject_from_cwd()
        if cwd_worktree:
            return cwd_worktree
        env_path = os.environ.get("T3_REPO", "")
        if env_path:
            p = Path(env_path).expanduser()
            if (p / "pyproject.toml").is_file():
                return p
        from teatree import find_project_root  # noqa: PLC0415

        return find_project_root()

    @staticmethod
    def find_overlay_repo(dist_name: str) -> Path | None:
        """Find the overlay repo in the workspace directory."""
        from teatree.config import load_config  # noqa: PLC0415

        config = load_config()
        workspace = Path(config.user.workspace_dir).expanduser()

        # Check TOML overlay paths first — they're explicit and authoritative.
        for overlay_cfg in (config.raw.get("overlays") or {}).values():
            path_str = overlay_cfg.get("path", "")
            if path_str:
                candidate = Path(path_str).expanduser()
                if (candidate / "pyproject.toml").is_file():
                    return candidate

        # Fallback: scan workspace for dist_name directory.
        for candidate in [workspace / dist_name, *workspace.glob(f"*/{dist_name}")]:
            if (candidate / "pyproject.toml").is_file():
                return candidate
        return None

    @staticmethod
    def make_editable(package: str, repo_path: Path) -> None:
        """Install *package* as editable from *repo_path*, persisting through ``uv run``.

        ``uv pip install -e`` is ephemeral — ``uv run`` re-syncs from the lock file
        and overwrites it.  To persist, we patch ``[tool.uv.sources]`` in the host
        project's ``pyproject.toml`` and hide the change from git via
        ``--assume-unchanged``.  A gitignored ``.t3-dev-sources`` marker records the
        override so worktree cleanup can restore the original state.
        """
        typer.echo(f"WARN  {package} is not editable (contribute=true). Installing from {repo_path}...")

        project_root = _find_host_project_root()
        if project_root is None:
            result = run_allowed_to_fail(
                ["uv", "pip", "install", "--quiet", "-e", str(repo_path)],
                expected_codes=None,
            )
            if result.returncode == 0:
                typer.echo(f"OK    {package} is now editable from {repo_path} (ephemeral — no host project found)")
            else:
                typer.echo(f"FAIL  Could not install {package} as editable: {result.stderr.strip()}")
            return

        pyproject = project_root / "pyproject.toml"
        marker = project_root / ".t3-dev-sources"

        if _patch_uv_source(pyproject, package, repo_path):
            _write_dev_sources_marker(marker, package, repo_path)
            run_allowed_to_fail(
                ["git", "update-index", "--assume-unchanged", "pyproject.toml"],
                cwd=project_root,
                expected_codes=None,
            )
            result = run_allowed_to_fail(
                ["uv", "sync", "--quiet"],
                cwd=project_root,
                expected_codes=None,
            )
            if result.returncode == 0:
                typer.echo(f"OK    {package} is now editable from {repo_path} (persisted in .t3-dev-sources)")
            else:
                typer.echo(f"FAIL  uv sync failed after patching sources: {result.stderr.strip()}")
        else:
            typer.echo(
                f"WARN  {package} is not in host pyproject.toml sources. Fix: `uv tool install --editable {repo_path}`"
            )

    @staticmethod
    def restore_sources(project_root: Path) -> None:
        """Revert editable source overrides recorded in ``.t3-dev-sources``."""
        marker = project_root / ".t3-dev-sources"
        if not marker.is_file():
            return

        run_allowed_to_fail(
            ["git", "update-index", "--no-assume-unchanged", "pyproject.toml"],
            cwd=project_root,
            expected_codes=None,
        )
        run_allowed_to_fail(
            ["git", "checkout", "--", "pyproject.toml"],
            cwd=project_root,
            expected_codes=None,
        )
        marker.unlink(missing_ok=True)
        typer.echo("OK    Restored original [tool.uv.sources] from git")


class IntrospectionHelpers:
    """Package introspection — editable info, version display."""

    @staticmethod
    def print_package_info(dist_name: str, import_name: str, *, label: str = "") -> None:
        label = label or dist_name

        try:
            import importlib  # noqa: PLC0415

            mod = importlib.import_module(import_name)
            source_path = getattr(mod, "__file__", None) or ""
            source_dir = str(Path(source_path).parent) if source_path else "(unknown)"
        except ImportError:
            typer.echo(f"{label + ':':<18}not installed")
            typer.echo()
            return

        editable, url = IntrospectionHelpers.editable_info(dist_name)
        mode = "editable" if editable else "installed"
        typer.echo(f"{label + ':':<18}{source_dir}  ({mode})")
        if editable and url:  # pragma: no branch
            typer.echo(f"{'':18}{url}")
        typer.echo()

    @staticmethod
    def editable_info(dist_name: str) -> tuple[bool, str]:
        """Return (is_editable, source_url) for a distribution."""
        import json  # noqa: PLC0415

        try:
            dist = distribution(dist_name)
        except PackageNotFoundError:
            return False, ""

        direct_url = dist.read_text("direct_url.json")
        if not direct_url:
            return False, ""

        try:
            data = json.loads(direct_url)
        except (json.JSONDecodeError, AttributeError):
            return False, ""
        else:
            editable = data.get("dir_info", {}).get("editable", False)
            url = data.get("url", "")
            return editable, url


def _check_single_db() -> bool:
    """Warn if any ``db.sqlite3`` other than the canonical path exists under DATA_DIR."""
    from teatree.paths import CANONICAL_DB, DATA_DIR, find_stale_dbs  # noqa: PLC0415

    stale = list(find_stale_dbs(DATA_DIR, canonical=CANONICAL_DB))
    if not stale:
        return True
    for path in stale:
        typer.echo(f"WARN  Stale DB at {path} — canonical DB is {CANONICAL_DB}. Remove to silence.")
    return False


def _check_singletons() -> bool:
    """Clean up stale pid files for known singleton processes."""
    from teatree.utils.singleton import default_pid_path, read_pid  # noqa: PLC0415

    for name in ("teatree-worker", "slack-listener", "loop-tick"):
        path = default_pid_path(name)
        had_file = path.is_file()
        if read_pid(path) is None and had_file:
            typer.echo(f"OK    Cleared stale {name} pid file")
    return True


def _check_editable_sanity() -> bool:
    ok = True
    try:
        for problem in DoctorService.check_editable_sanity():
            typer.echo(f"WARN  {problem}")
            ok = False
    except Exception as exc:  # noqa: BLE001 — overlay loading can fail in many ways
        typer.echo(f"FAIL  Editable sanity check crashed: {exc.__class__.__name__}: {exc}")
        ok = False
    return ok


def _check_skills() -> bool:
    ok = True
    claude_skills = Path.home() / ".claude" / "skills"
    if claude_skills.is_dir():
        from teatree.skill_schema import validate_directory  # noqa: PLC0415

        errors, warnings = validate_directory(claude_skills)
        for warning in warnings:
            typer.echo(f"WARN  {warning}")
        for error in errors:
            typer.echo(f"FAIL  {error}")
            ok = False
        if not errors:
            skill_count = sum(1 for d in claude_skills.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())
            typer.echo(f"OK    {skill_count} skill(s) validated")
    return ok


def _read_json_safe(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _resolve_main_clone() -> Path | None:
    env_path = os.environ.get("T3_REPO", "")
    if env_path:
        candidate = Path(env_path).expanduser()
        if (candidate / "pyproject.toml").is_file():
            return candidate
    try:
        repo = DoctorService.find_teatree_repo()
    except OSError:
        return None
    if not repo:
        return None
    git = repo / ".git"
    if git.is_file():
        match = re.match(r"^gitdir:\s*(.+)$", git.read_text().strip())
        if match:
            main_git = Path(match.group(1)).parent.parent
            if main_git.name == ".git" and main_git.is_dir():
                return main_git.parent
    return repo


def _repair_marketplace_json(plugins_dir: Path, target: str, now: str) -> bool:
    path = plugins_dir / "known_marketplaces.json"
    data = _read_json_safe(path)
    mp_name = _CLAUDE_PLUGIN_ID.split("@", 1)[1]
    if data.get(mp_name, {}).get("installLocation") == target:
        return False
    data[mp_name] = {
        "source": {"source": "directory", "path": target},
        "installLocation": target,
        "lastUpdated": now,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _repair_installed_plugins(plugins_dir: Path, target: str, now: str) -> bool:
    path = plugins_dir / "installed_plugins.json"
    data = _read_json_safe(path)
    plugins = data.setdefault("plugins", {})
    entries = plugins.get(_CLAUDE_PLUGIN_ID, [])
    if entries and entries[0].get("installPath") == target:
        return False
    data.setdefault("version", 2)
    plugins[_CLAUDE_PLUGIN_ID] = [
        {
            "scope": "user",
            "installPath": target,
            "version": "local",
            "installedAt": entries[0].get("installedAt", now) if entries else now,
            "lastUpdated": now,
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _repair_enabled_plugins() -> bool:
    settings_path = Path.home() / ".claude" / "settings.json"
    resolved = settings_path.resolve() if settings_path.is_file() else settings_path
    data = _read_json_safe(resolved)
    enabled = data.setdefault("enabledPlugins", {})
    if enabled.get(_CLAUDE_PLUGIN_ID) is True:
        return False
    enabled[_CLAUDE_PLUGIN_ID] = True
    resolved.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _ensure_plugin_registered() -> bool:
    """Verify and auto-repair t3 plugin registration.

    Called at every ``t3 doctor check`` (and thus every Claude session start).
    Best-effort — never fails the check if the repo or filesystem is unavailable.
    """
    try:
        return _do_ensure_plugin_registered()
    except OSError:
        return True


def _do_ensure_plugin_registered() -> bool:
    repo = _resolve_main_clone()
    if not repo:
        return True

    from datetime import UTC, datetime  # noqa: PLC0415

    target = str(repo.resolve())
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    plugins_dir = Path.home() / ".claude" / "plugins"

    repaired = _repair_marketplace_json(plugins_dir, target, now)
    repaired = _repair_installed_plugins(plugins_dir, target, now) or repaired
    repaired = _repair_enabled_plugins() or repaired

    if repaired:
        typer.echo(f"OK    Auto-repaired {_CLAUDE_PLUGIN_ID} plugin → {target}")
    return True


@doctor_app.command()
def check() -> bool:
    """Verify imports, required tools, and editable-install sanity."""
    try:
        import django  # noqa: PLC0415, F401

        import teatree.core  # noqa: PLC0415, F401
    except ImportError as exc:
        typer.echo(f"FAIL  Import check: {exc}")
        return False

    ok = True
    for tool in _REQUIRED_TOOLS:
        if not shutil.which(tool):
            typer.echo(f"FAIL  Required tool not found: {tool}")
            ok = False

    ok = _check_editable_sanity() and ok
    ok = _check_skills() and ok
    ok = _check_single_db() and ok
    _check_singletons()
    report_missing_authorizations(typer.echo)
    _ensure_plugin_registered()

    if ok:
        typer.echo("All checks passed")
    return ok
