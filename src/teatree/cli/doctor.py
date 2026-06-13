"""Doctor CLI commands — smoke-test hooks, imports, services.

Module size policy: this module hosts ``DoctorService`` + ``IntrospectionHelpers``
+ the ``check()`` Typer entry point. The ``_check_*`` helpers live in
``_doctor_checks`` and the plugin-registration repair helpers live in
``_doctor_plugin_repair`` (souliane/teatree#1270). Both private modules are
re-exported below so existing consumers (tests, sibling CLI modules) keep
their ``from teatree.cli.doctor import _x`` and ``teatree.cli.doctor._x``
access paths intact.
"""

import json
import os
import re
import shutil
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import typer

from teatree.cli._doctor_checks import (
    _check_account_switch,
    _check_agent_session_pins,
    _check_dream_staleness,
    _check_editable_sanity,
    _check_entrypoint_is_primary_clone,
    _check_legacy_overlay_alias,
    _check_mcp_connectivity,
    _check_single_db,
    _check_singletons,
    _check_skills,
    _check_stale_uv_venv,
)
from teatree.cli._doctor_plugin_repair import (
    _do_ensure_plugin_registered,
    _ensure_plugin_registered,
    _read_json_safe,
    _repair_enabled_plugins,
    _repair_installed_plugins,
    _repair_marketplace_json,
    _resolve_main_clone,
)
from teatree.cli.recommended_authorizations import authorizations, report_missing_authorizations
from teatree.utils.django_bootstrap import ensure_django
from teatree.utils.run import run_allowed_to_fail

doctor_app = typer.Typer(no_args_is_help=False, help="Smoke-test hooks, imports, services.")
doctor_app.command()(authorizations)
_REQUIRED_TOOLS = ("direnv", "git", "jq")
_CLAUDE_PLUGIN_ID = "t3@souliane"

# Agent runtimes that consume teatree skills.  ``t3 setup`` creates symlinks
# into each runtime's skills directory that already exists — missing dirs are
# skipped silently.  The Claude dir is always ensured by setup.
AGENT_SKILL_RUNTIMES: tuple[str, ...] = ("claude", "codex")

__all__ = (
    "AGENT_SKILL_RUNTIMES",
    "_CLAUDE_PLUGIN_ID",
    "_REQUIRED_TOOLS",
    "DoctorService",
    "IntrospectionHelpers",
    "PackageNotFoundError",
    "_check_account_switch",
    "_check_agent_session_pins",
    "_check_dream_staleness",
    "_check_editable_sanity",
    "_check_entrypoint_is_primary_clone",
    "_check_legacy_overlay_alias",
    "_check_mcp_connectivity",
    "_check_single_db",
    "_check_singletons",
    "_check_skills",
    "_check_stale_uv_venv",
    "_do_ensure_plugin_registered",
    "_ensure_plugin_registered",
    "_find_host_project_root",
    "_find_teatree_pyproject_from_cwd",
    "_patch_uv_source",
    "_read_json_safe",
    "_repair_enabled_plugins",
    "_repair_installed_plugins",
    "_repair_marketplace_json",
    "_resolve_main_clone",
    "_write_dev_sources_marker",
    "agent_skill_dirs",
    "check",
    "doctor_app",
)


def agent_skill_dirs() -> list[tuple[str, Path]]:
    """Return (runtime_label, skills_dir) pairs, resolved against the current HOME."""
    return [(name, Path.home() / f".{name}" / "skills") for name in AGENT_SKILL_RUNTIMES]


_DEV_SOURCES_FILE = ".t3-dev-sources"

# Files the editable-source override mutates and must keep out of the commit
# path: ``pyproject.toml`` carries the local-path source, and ``uv sync``
# rewrites ``uv.lock`` to record it.  Both are hidden from git via
# ``--assume-unchanged`` for the duration of the override and restored together.
_DEV_HIDDEN_FILES = ("pyproject.toml", "uv.lock")


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
                "If you are contributing to teatree, set contribute=true. "
                "If not, run "
                "`uv tool install --from git+https://github.com/souliane/teatree.git teatree` "
                "to drop the editable install.",
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
        ``--assume-unchanged``.  ``uv sync`` then rewrites ``uv.lock`` to record the
        local-path source; that lockfile mutation is hidden the same way so the
        dev-only editable state never leaks into a commit.  A gitignored
        ``.t3-dev-sources`` marker records the override so worktree cleanup can
        restore the original state.
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
            for tracked in _DEV_HIDDEN_FILES:
                if (project_root / tracked).is_file():
                    run_allowed_to_fail(
                        ["git", "update-index", "--assume-unchanged", tracked],
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
        """Revert editable source overrides recorded in ``.t3-dev-sources``.

        Unhides and restores both the patched ``pyproject.toml`` and the
        ``uv sync``-mutated ``uv.lock`` so neither carries dev-only editable
        state after cleanup.
        """
        marker = project_root / ".t3-dev-sources"
        if not marker.is_file():
            return

        for tracked in _DEV_HIDDEN_FILES:
            run_allowed_to_fail(
                ["git", "update-index", "--no-assume-unchanged", tracked],
                cwd=project_root,
                expected_codes=None,
            )
            run_allowed_to_fail(
                ["git", "checkout", "--", tracked],
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

    # Must precede _check_editable_sanity: under contribute=true that check can
    # auto-make-editable against the cwd worktree, creating the exact stale
    # worktree-anchored install this guard exists to catch (#1507).
    ok = _check_entrypoint_is_primary_clone() and ok
    ok = _check_editable_sanity() and ok
    ok = _check_skills() and ok
    ok = _check_single_db() and ok
    ok = _check_stale_uv_venv() and ok
    ok = _check_agent_session_pins() and ok

    # ``check`` is a plain Typer command in the Django-free CLI group, so
    # Django is not configured by the time the self-DB schema guard runs.
    # Without this the inspection hit ``ImproperlyConfigured`` and silently
    # WARNed, masking a stale runtime self-DB that locks out the merge path
    # (#126). Configure Django first so the guard reports the REAL state.
    ensure_django()

    from teatree.core.gates.schema_guard import doctor_check_self_db_migrations  # noqa: PLC0415

    ok = doctor_check_self_db_migrations() and ok

    # Pre-investigation stale-clone hard-fail gate (#948). Surfaces at
    # session start so a bug-investigation sub-agent cannot start root-
    # causing against a clone many commits behind ``origin/<default>``.
    # Distinct from #940 (post-implementation branch-currency on PR
    # branches); this is the *entry-point* gate before any investigation
    # reads source files. An offline/missing remote is a valid state —
    # ``doctor_check_clone_currency`` skips affected repos rather than
    # FAILing (same posture as schema_guard's DB-offline WARN).
    from teatree.cli.update import _collect_repos  # noqa: PLC0415
    from teatree.core.gates.clone_guard import doctor_check_clone_currency  # noqa: PLC0415

    ok = doctor_check_clone_currency(_collect_repos()) and ok

    # Idle-time dream consolidation staleness alarm (#1933). Runs after
    # ``ensure_django`` because it reads the ``DreamRunMarker`` row. A WARN
    # (not a hard FAIL): a stale dream cron means memories pile up unpromoted,
    # which the operator should fix, but it must not red the whole doctor run.
    _check_dream_staleness()

    # In-session `/login` account-switch recovery (#1916). Runs after
    # ``ensure_django`` because it builds messaging backends via the overlay
    # factory to live-probe connector reachability post cache-invalidation.
    ok = _check_account_switch() and ok

    # Enabled-MCP connectivity + declared-provider check (#2282). Runs after the
    # account-switch gate (which invalidates the backend cache on a `/login`), so
    # the live `claude mcp list` probe reflects the post-recovery state. An
    # enabled-but-disconnected MCP is a hard FAIL; `claude` absent degrades to a WARN.
    ok = _check_mcp_connectivity() and ok

    _check_singletons()
    _check_legacy_overlay_alias()
    report_missing_authorizations(typer.echo)
    _ensure_plugin_registered()

    if ok:
        typer.echo("All checks passed")
    return ok


@doctor_app.callback(invoke_without_command=True)
def _doctor_default(ctx: typer.Context) -> None:
    """Run ``check`` when ``t3 doctor`` is invoked with no subcommand (#2065)."""
    if ctx.invoked_subcommand is None:
        raise typer.Exit(code=0 if check() else 1)
