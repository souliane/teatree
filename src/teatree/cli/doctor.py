"""Doctor CLI commands — smoke-test hooks, imports, services."""

import json
import os
import re
import shutil
import sys
from importlib.metadata import PackageNotFoundError, distribution, packages_distributions
from pathlib import Path

import typer

doctor_app = typer.Typer(no_args_is_help=True, help="Smoke-test hooks, imports, services.")
_REQUIRED_TOOLS = ("direnv", "git", "jq")
_CLAUDE_PLUGIN_ID = "t3@souliane"

# Agent runtimes that consume teatree skills.  ``t3 setup`` creates symlinks
# into each runtime's skills directory that already exists — missing dirs are
# skipped silently.  The Claude dir is always ensured by setup.
AGENT_SKILL_RUNTIMES: tuple[str, ...] = ("claude", "codex")


def agent_skill_dirs() -> list[tuple[str, Path]]:
    """Return (runtime_label, skills_dir) pairs, resolved against the current HOME."""
    return [(name, Path.home() / f".{name}" / "skills") for name in AGENT_SKILL_RUNTIMES]


def _resolve_overlay_dists(overlays: dict) -> list[str]:
    """Map overlay instances to their distribution package names."""
    dist_map = packages_distributions()
    result: list[str] = []
    for overlay_inst in overlays.values():
        top_package = type(overlay_inst).__module__.split(".", maxsplit=1)[0]
        dist_names = dist_map.get(top_package, [top_package])
        result.append(dist_names[0] if dist_names else top_package)
    return result


_DEV_SOURCES_FILE = ".t3-dev-sources"


def _find_host_project_root() -> Path | None:
    """Walk up from cwd to find the host project (directory with manage.py + pyproject.toml)."""
    for directory in [Path.cwd(), *Path.cwd().parents]:
        if (directory / "manage.py").is_file() and (directory / "pyproject.toml").is_file():
            return directory
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
        if not plugins_json.is_file():
            return None
        try:
            data = json.loads(plugins_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        entries = (data.get("plugins") or {}).get(_CLAUDE_PLUGIN_ID) or []
        if not entries:
            return None
        first = entries[0]
        return {
            "version": first.get("version", ""),
            "installPath": first.get("installPath", ""),
            "scope": first.get("scope", ""),
        }

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

        # Overlays — resolve dist names once, check both directions
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

        overlay_dists = _resolve_overlay_dists(get_all_overlays())

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
        env_path = os.environ.get("T3_REPO", "")
        if env_path:
            p = Path(env_path).expanduser()
            if (p / "pyproject.toml").is_file():
                return p
        # Auto-detect from package location (editable or source checkout)
        from teatree import find_project_root  # noqa: PLC0415

        return find_project_root()

    @staticmethod
    def find_overlay_repo(dist_name: str) -> Path | None:
        """Find the overlay repo in the workspace directory."""
        from teatree.config import load_config  # noqa: PLC0415

        workspace = Path(load_config().user.workspace_dir).expanduser()
        # Try common layouts: workspace/<dist>, workspace/*/<dist>
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
        import subprocess  # noqa: PLC0415, S404

        typer.echo(f"WARN  {package} is not editable (contribute=true). Installing from {repo_path}...")

        project_root = _find_host_project_root()
        if project_root is None:
            # Fallback: ephemeral pip install (will be overwritten by uv run)
            result = subprocess.run(  # noqa: S603
                ["uv", "pip", "install", "--quiet", "-e", str(repo_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                typer.echo(f"OK    {package} is now editable from {repo_path} (ephemeral — no host project found)")
            else:
                typer.echo(f"FAIL  Could not install {package} as editable: {result.stderr.strip()}")
            return

        pyproject = project_root / "pyproject.toml"
        marker = project_root / ".t3-dev-sources"

        if _patch_uv_source(pyproject, package, repo_path):
            # Record the override in the gitignored marker
            _write_dev_sources_marker(marker, package, repo_path)
            # Hide pyproject.toml from git
            subprocess.run(
                ["git", "update-index", "--assume-unchanged", "pyproject.toml"],
                cwd=project_root,
                capture_output=True,
                check=False,
            )
            # Sync to apply
            result = subprocess.run(
                ["uv", "sync", "--quiet"],
                cwd=project_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                typer.echo(f"OK    {package} is now editable from {repo_path} (persisted in .t3-dev-sources)")
            else:
                typer.echo(f"FAIL  uv sync failed after patching sources: {result.stderr.strip()}")
        else:
            typer.echo(f"FAIL  Could not patch [tool.uv.sources] for {package}")

    @staticmethod
    def restore_sources(project_root: Path) -> None:
        """Revert editable source overrides recorded in ``.t3-dev-sources``."""
        import subprocess  # noqa: PLC0415, S404

        marker = project_root / ".t3-dev-sources"
        if not marker.is_file():
            return

        # Un-hide pyproject.toml first
        subprocess.run(
            ["git", "update-index", "--no-assume-unchanged", "pyproject.toml"],
            cwd=project_root,
            capture_output=True,
            check=False,
        )
        # Restore pyproject.toml from git
        subprocess.run(
            ["git", "checkout", "--", "pyproject.toml"],
            cwd=project_root,
            capture_output=True,
            check=False,
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


@doctor_app.command()
def check() -> bool:
    """Verify imports, required tools, and editable-install sanity."""
    ok = True

    try:
        import django  # noqa: PLC0415, F401

        import teatree.core  # noqa: PLC0415, F401
    except ImportError as exc:
        typer.echo(f"FAIL  Import check: {exc}")
        return False

    for tool in _REQUIRED_TOOLS:
        if not shutil.which(tool):
            typer.echo(f"FAIL  Required tool not found: {tool}")
            ok = False

    for problem in DoctorService.check_editable_sanity():
        typer.echo(f"WARN  {problem}")
        ok = False

    # Validate SKILL.md frontmatter.
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

    if ok:
        typer.echo("All checks passed")
    return ok
