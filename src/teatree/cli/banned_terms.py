"""Banned-terms CLI — the full-tree backstop scan (#1570).

``t3 banned-terms scan-tree`` enumerates every git-tracked file and scans
its committed CONTENT for the operator brand list AND the built-in
conflated-terminology gate, exiting non-zero with the offending
``file:line`` list. It is the backstop the diff-only posting gate cannot
provide: a committed banned term never appears in a post-landing diff. CI
runs this on push-to-main and on a schedule.
"""

from pathlib import Path

import typer
from rich.console import Console

from teatree.core.banned_terms_tree import BannedTermsUnsetError, scan_committed_tree

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
    config: Path | None = typer.Option(
        None,
        "--config",
        help="Override the ~/.teatree.toml term-list config (else resolved as the gate does).",
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
) -> None:
    """Scan every git-tracked file for committed banned terms."""
    root = repo_root if repo_root is not None else Path.cwd()
    try:
        result = scan_committed_tree(root, config_path=config)
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
