"""``t3 config`` — configuration and skill autoloading commands."""

import os
import sys
from pathlib import Path

import typer

config_app = typer.Typer(no_args_is_help=True, help="Configuration and autoloading.")


@config_app.command(name="check-update")
def check_update() -> None:
    """Check if a newer version of teatree is available."""
    from teatree.config import check_for_updates  # noqa: PLC0415

    message = check_for_updates(force=True)
    typer.echo(message or "You are up to date.")


@config_app.command(name="write-skill-cache")
def write_skill_cache() -> None:
    """Write overlay skill metadata + trigger index to XDG cache for hook consumption."""
    import django  # noqa: PLC0415

    from teatree.config import DATA_DIR, discover_active_overlay  # noqa: PLC0415

    discover_active_overlay()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    django.setup()

    from teatree.core.skill_cache import write_skill_metadata_cache  # noqa: PLC0415

    write_skill_metadata_cache()
    typer.echo(f"Wrote skill metadata to {DATA_DIR / 'skill-metadata.json'}")


@config_app.command()
def autoload() -> None:
    """List skill auto-loading rules from context-match.yml files."""
    from teatree.agents.skill_bundle import DEFAULT_SKILLS_DIR  # noqa: PLC0415

    skills_dir = DEFAULT_SKILLS_DIR
    if not skills_dir.is_dir():
        typer.echo(f"Skills directory not found: {skills_dir}")
        raise typer.Exit(code=1)

    found = False
    for skill in sorted(skills_dir.iterdir()):
        match_file = skill / "hook-config" / "context-match.yml"
        if not match_file.is_file():
            continue
        found = True
        typer.echo(f"\n{skill.name}:")
        typer.echo(match_file.read_text(encoding="utf-8").rstrip())

    if not found:
        typer.echo("No context-match.yml files found in any skill directory.")


@config_app.command()
def cache() -> None:
    """Show the XDG skill-metadata cache content."""
    import json as _json  # noqa: PLC0415

    from teatree.config import DATA_DIR  # noqa: PLC0415

    cache_path = DATA_DIR / "skill-metadata.json"
    if not cache_path.is_file():
        typer.echo(f"No cache found at {cache_path}")
        typer.echo("Run: t3 config write-skill-cache")
        raise typer.Exit(code=1)

    data = _json.loads(cache_path.read_text(encoding="utf-8"))
    typer.echo(f"Cache: {cache_path}")
    typer.echo(_json.dumps(data, indent=2))


@config_app.command()
def deps(skill: str) -> None:
    """Show resolved dependency chain for a skill."""
    import json as _json  # noqa: PLC0415

    from teatree.config import DATA_DIR  # noqa: PLC0415
    from teatree.skill_deps import resolve_all  # noqa: PLC0415

    cache_path = DATA_DIR / "skill-metadata.json"
    if not cache_path.is_file():
        typer.echo(f"No cache found at {cache_path}")
        typer.echo("Run: t3 config write-skill-cache")
        raise typer.Exit(code=1)

    data = _json.loads(cache_path.read_text(encoding="utf-8"))
    trigger_index = data.get("trigger_index", [])

    precomputed = data.get("resolved_requires", {})
    if precomputed and skill in precomputed:
        chain = precomputed[skill]
    else:
        resolved = resolve_all(trigger_index)
        chain = resolved.get(skill, [skill])

    typer.echo(" → ".join(chain))


@config_app.command(name="test-trigger")
def test_trigger(prompt: str) -> None:
    """Test which skill would be triggered for a given prompt."""
    import json as _json  # noqa: PLC0415

    from teatree import find_project_root as _find_root  # noqa: PLC0415
    from teatree.config import DATA_DIR  # noqa: PLC0415

    root = _find_root()
    scripts_lib = root / "scripts" / "lib" if root else Path(__file__).resolve().parent
    if str(scripts_lib) not in sys.path:
        sys.path.insert(0, str(scripts_lib))

    from skill_loader import detect_intent_detailed  # noqa: PLC0415  # ty: ignore[unresolved-import]

    cache_path = DATA_DIR / "skill-metadata.json"
    trigger_index: list[dict] | None = None
    if cache_path.is_file():
        data = _json.loads(cache_path.read_text(encoding="utf-8"))
        trigger_index = data.get("trigger_index", [])

    match = detect_intent_detailed(prompt, trigger_index=trigger_index)
    typer.echo(str(match))
