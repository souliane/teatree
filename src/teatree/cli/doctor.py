"""Doctor CLI commands — smoke-test hooks, imports, services."""

import os
import shutil
import sys
from pathlib import Path

import typer

doctor_app = typer.Typer(no_args_is_help=True, help="Smoke-test hooks, imports, services.")
_REQUIRED_TOOLS = ("direnv", "git", "jq")


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
            overlay_name = entry.name

            # New convention: skills/ directory
            project_skills = project / "skills"
            if project_skills.is_dir():
                results.extend(
                    (skill, skill.name) for skill in sorted(project_skills.iterdir()) if (skill / "SKILL.md").is_file()
                )

            # Legacy convention: overlay app dir with SKILL.md
            for subdir in sorted(project.iterdir()):
                if subdir.is_dir() and subdir.name != "skills" and (subdir / "SKILL.md").is_file():
                    results.append((subdir, overlay_name))
                    break  # one overlay skill per project
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
        """Verify editable status matches declared intent.

        When ``contribute = true`` in ``.teatree.toml`` and teatree is not
        installed as editable, auto-fixes by running
        ``uv pip install -e <teatree-repo>``.
        """
        problems: list[str] = []

        try:
            if "DJANGO_SETTINGS_MODULE" not in os.environ:
                from teatree.config import discover_active_overlay  # noqa: PLC0415

                active = discover_active_overlay()
                if active:
                    os.environ["DJANGO_SETTINGS_MODULE"] = "teatree.settings"
                else:
                    return problems  # no overlay, no settings to check

            import django  # noqa: PLC0415

            django.setup()
            from django.conf import settings as django_settings  # noqa: PLC0415
        except Exception:  # noqa: BLE001 — Django may not be installed
            return problems

        # Use contribute flag from config (takes precedence over Django settings)
        from teatree.config import load_config  # noqa: PLC0415

        contribute = load_config().user.contribute
        teatree_should_be_editable = contribute or getattr(django_settings, "TEATREE_EDITABLE", False)
        teatree_is_editable, _ = IntrospectionHelpers.editable_info("teatree")

        if teatree_should_be_editable and not teatree_is_editable:
            teatree_repo = DoctorService.find_teatree_repo()
            if teatree_repo:
                DoctorService.make_editable("teatree", teatree_repo)
            else:
                problems.append(
                    "contribute=true but teatree is not editable and local repo not found. "
                    "Fix: set T3_REPO env var or run `uv pip install -e <teatree-path>`",
                )
        elif not teatree_should_be_editable and teatree_is_editable:
            problems.append(
                "teatree is editable but TEATREE_EDITABLE is not set. "
                "You risk accidentally modifying framework code. "
                "Fix: set TEATREE_EDITABLE = True in settings.py if contributing, "
                "or remove the editable source.",
            )

        # Check overlay discoverability via entry points
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

        overlays = get_all_overlays()
        for overlay_inst in overlays.values():
            overlay_module = type(overlay_inst).__module__
            top_package = overlay_module.split(".", maxsplit=1)[0]
            from importlib.metadata import packages_distributions  # noqa: PLC0415

            dist_map = packages_distributions()
            dist_names = dist_map.get(top_package, [top_package])
            overlay_dist = dist_names[0] if dist_names else top_package

            overlay_should_be_editable = getattr(django_settings, "OVERLAY_EDITABLE", False)
            overlay_is_editable, _ = IntrospectionHelpers.editable_info(overlay_dist)

            if overlay_should_be_editable and not overlay_is_editable:
                problems.append(
                    f"OVERLAY_EDITABLE=True but overlay ({overlay_dist}) is not editable. "
                    "Agent changes to overlay code will be lost. "
                    "Fix: run `uv pip install -e .`",
                )
            elif not overlay_should_be_editable and overlay_is_editable:
                problems.append(
                    f"Overlay ({overlay_dist}) is editable but OVERLAY_EDITABLE is not set. "
                    "Fix: set OVERLAY_EDITABLE = True in settings.py if contributing.",
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
        pkg_root = Path(__file__).resolve().parents[4]
        if (pkg_root / ".git").is_dir() and (pkg_root / "pyproject.toml").is_file():
            return pkg_root
        return None

    @staticmethod
    def make_editable(package: str, repo_path: Path) -> None:
        import subprocess  # noqa: PLC0415, S404

        typer.echo(f"WARN  {package} is not editable (contribute=true). Installing from {repo_path}...")
        result = subprocess.run(  # noqa: S603
            ["uv", "pip", "install", "--quiet", "-e", str(repo_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            typer.echo(f"OK    {package} is now editable from {repo_path}")
        else:
            typer.echo(f"FAIL  Could not install {package} as editable: {result.stderr.strip()}")


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
        from importlib.metadata import PackageNotFoundError, distribution  # noqa: PLC0415

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
def repair() -> None:
    """Repair skill symlinks and verify installation health."""
    from teatree.agents.skill_bundle import DEFAULT_SKILLS_DIR  # noqa: PLC0415

    skills_dir = DEFAULT_SKILLS_DIR
    if not skills_dir.is_dir():
        typer.echo(f"Skills directory not found: {skills_dir}")
        raise typer.Exit(code=1)

    claude_skills = Path.home() / ".claude" / "skills"
    claude_skills.mkdir(parents=True, exist_ok=True)

    created, fixed = DoctorService.repair_symlinks(skills_dir, claude_skills)

    # Clean broken symlinks
    removed = 0
    for link in claude_skills.iterdir():
        if link.is_symlink() and not link.exists():
            link.unlink()
            removed += 1

    typer.echo(f"Skills: {created} created, {fixed} fixed, {removed} broken removed")
    typer.echo(f"Source: {skills_dir}")
    overlay_skills = DoctorService.collect_overlay_skills()
    if overlay_skills:
        typer.echo(f"Overlays: {len(overlay_skills)} overlay skill(s) managed")


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


@doctor_app.command(name="info")
def doctor_info() -> None:
    """Show t3 path, teatree/overlay sources, and editable status."""
    DoctorService.show_info()
