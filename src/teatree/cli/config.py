"""``t3 config`` — configuration and skill autoloading commands."""

import typer

from teatree.utils.django_bootstrap import ensure_django

config_app = typer.Typer(no_args_is_help=True, help="Configuration and autoloading.")


@config_app.command(name="check-update")
def check_update() -> None:
    """Check if a newer version of teatree is available."""
    from teatree.config import check_for_updates  # noqa: PLC0415

    message = check_for_updates(force=True)
    typer.echo(message or "You are up to date.")


@config_app.command()
def show(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Read-only view of config: text-file intent vs DB regenerable cache (#628).

    The intent section is ``~/.teatree.toml`` resolved — the user-authored
    source of truth. The derived section is DB / data-dir state that can be
    deleted and rebuilt from the text files; every entry is flagged
    regenerable so the cache-vs-intent invariant is visible. Reads only.
    """
    import json as _json  # noqa: PLC0415

    from teatree.cli.config_view import build_config_view, render_config_view  # noqa: PLC0415

    view = build_config_view()
    if json_output:
        typer.echo(_json.dumps(view.to_dict()))
        return
    typer.echo(render_config_view(view))


@config_app.command(name="write-skill-cache")
def write_skill_cache() -> None:
    """Write overlay skill metadata + trigger index to XDG cache for hook consumption."""
    from teatree.config import discover_active_overlay  # noqa: PLC0415
    from teatree.paths import DATA_DIR  # noqa: PLC0415

    discover_active_overlay()
    ensure_django()

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

    from teatree.paths import DATA_DIR  # noqa: PLC0415

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

    from teatree.paths import DATA_DIR  # noqa: PLC0415
    from teatree.skill_support.deps import resolve_all  # noqa: PLC0415

    cache_path = DATA_DIR / "skill-metadata.json"
    if not cache_path.is_file():
        typer.echo(f"No cache found at {cache_path}")
        typer.echo("Run: t3 config write-skill-cache")
        raise typer.Exit(code=1)

    data = _json.loads(cache_path.read_text(encoding="utf-8"))
    skill_index = data.get("skill_index", [])

    precomputed = data.get("resolved_requires", {})
    if precomputed and skill in precomputed:
        chain = precomputed[skill]
    else:
        resolved = resolve_all(skill_index)
        chain = resolved.get(skill, [skill])

    typer.echo(" → ".join(chain))
