"""Banned-terms CLI — the full-tree backstop scan (#1570).

``t3 banned-terms scan-tree`` enumerates every git-tracked file and scans
its committed CONTENT for the operator brand list AND the built-in
conflated-terminology gate, exiting non-zero with the offending
``file:line`` list. It is the backstop the diff-only posting gate cannot
provide: a committed banned term never appears in a post-landing diff. CI
runs this on push-to-main and on a schedule.
"""

import json
from pathlib import Path

import typer
from rich.console import Console

from teatree.core.banned_terms_tree import BannedTermsUnsetError, migrate_registry, scan_committed_tree

banned_terms_app = typer.Typer(no_args_is_help=True, help="Banned-terms backstop scans.")
_console = Console()

_FINDINGS_EXIT_CODE = 1
_MISCONFIGURED_EXIT_CODE = 2


@banned_terms_app.callback()
def _root() -> None:
    """Banned-terms backstop scans.

    A callback keeps ``scan-tree`` a named subcommand even though it is
    currently the only command — Typer otherwise collapses a single
    command into the group root.
    """


@banned_terms_app.command(name="scan-tree")
def scan_tree(
    repo_root: Path | None = typer.Option(
        None,
        "--repo-root",
        help="Repository root to scan (defaults to the current directory).",
    ),
    *,
    require_brands: bool = typer.Option(
        False,
        "--require-brands",
        help="HARD-FAIL (exit 2) on an explicit-empty brand list (`banned_brands = []`), "
        "instead of warning and exiting 0. A genuinely-unset list always fails loud "
        "regardless of this flag; --require-brands additionally rejects the deliberate "
        "empty list. CI passes it; local dev omits it.",
    ),
    allow_unset: bool = typer.Option(
        False,
        "--allow-unset",
        help="EXPLICIT opt-in: treat a genuinely-unset brand list as INERT (run the "
        "always-on terminology pass only, exit 0) instead of failing loud (exit 2). "
        "Fail-closed BY DEFAULT — the fork-PR CI step passes it (a fork cannot read "
        "the brand secret); push/schedule omit it so a missing secret stays a LOUD "
        "refusal on main. Replaces the dead T3_BANNED_TERMS_CONFIG file fallback.",
    ),
) -> None:
    """Scan every git-tracked file for committed banned terms.

    The brand list is DB-home: ``$TEATREE_BANNED_BRANDS`` (a CI secret), the
    consolidated ``banned_term_registry``, or the canonical ``banned_brands``
    ``ConfigSetting`` row.
    """
    root = repo_root if repo_root is not None else Path.cwd()
    try:
        result = scan_committed_tree(root, allow_unset=allow_unset)
    except BannedTermsUnsetError as exc:
        # A genuinely-unset brand list (no config, no env, a missing key) is
        # refused LOUD (exit 2) — never a silent inert scan that hides a load
        # bug. An explicit empty list does not raise; it flows to the
        # INERT warning below.
        _console.print(f"[red]banned-terms scan-tree: MISCONFIGURED — {exc}[/]")
        raise typer.Exit(_MISCONFIGURED_EXIT_CODE) from exc

    if not result.brands_configured:
        if require_brands:
            _console.print(
                "[red]banned-terms scan-tree: MISCONFIGURED — brand backstop INERT under "
                "--require-brands: banned_brands is unpopulated.[/] "
                "Configure the TEATREE_BANNED_BRANDS secret (or [teatree].banned_brands) "
                "with the curated brand subset so the full-tree scan actually runs."
            )
            raise typer.Exit(_MISCONFIGURED_EXIT_CODE)
        _console.print(
            "[yellow]banned-terms scan-tree: WARNING — brand backstop INERT: "
            "banned_brands is unpopulated[/] "
            "([yellow]populate [teatree].banned_brands (or the TEATREE_BANNED_BRANDS "
            "secret) with the curated brand subset to activate the full-tree scan[/])."
        )

    if not result.findings:
        if result.brands_configured:
            _console.print("[green]banned-terms scan-tree: clean (0 findings).[/]")
        else:
            _console.print("[green]banned-terms scan-tree: no terminology findings.[/]")
        return

    _console.print(f"[red]banned-terms scan-tree: {len(result.findings)} committed banned-term finding(s).[/]")
    for finding in result.findings:
        _console.print(f"  {finding.render()}")
    _console.print(
        "\nA banned term is committed to the tree. Scrub it "
        "(the diff-only gate cannot see it — it is not in any new diff)."
    )
    raise typer.Exit(_FINDINGS_EXIT_CODE)


@banned_terms_app.command(name="migrate-registry")
def migrate_registry_command() -> None:
    """Produce the consolidated ``banned_term_registry`` from the three legacy sources.

    Reads the current ``banned_terms`` + ``banned_brands`` + allowlist, class-tags
    them (``banned_brands`` → ``leak``, ``banned_terms`` → ``prose_collider``,
    the allowlist → ``allow``), and SELF-VERIFIES the result reproduces every
    effective term the old config yields. On success it prints the JSON registry
    value to set at cutover (``t3 <overlay> config_setting set
    banned_term_registry '<json>'``, PR 2 — this command never writes it). If the
    migration would drop or change ANY term it FAILS LOUD (exit 2) with the diff.
    """
    result = migrate_registry()
    verification = result.verification
    if not verification.ok:
        _console.print(
            "[red]migrate-registry: MIGRATION IS LOSSY — refusing.[/] "
            "The registry would not reproduce the old term set:"
        )
        for line in verification.failure_reason().splitlines():
            _console.print(f"  {line}")
        raise typer.Exit(_MISCONFIGURED_EXIT_CODE)

    counts = ", ".join(f"{term_class}={len(terms)}" for term_class, terms in result.registry.items())
    _console.print(f"[green]migrate-registry: lossless — the registry reproduces every effective term ({counts}).[/]")
    # Plain, un-styled JSON so the operator can copy it verbatim into config_setting.
    typer.echo(json.dumps(result.registry, indent=2, sort_keys=True))
    _console.print(
        "\nSet it at cutover (PR 2): "
        "[cyan]t3 <overlay> config_setting set banned_term_registry '<the JSON above>'[/], "
        "then remove the T3_BANNED_TERMS / TEATREE_BANNED_BRANDS secrets."
    )
