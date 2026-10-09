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

from teatree.core.banned_terms_tree import BannedTermsUnsetError, TreeEnumerationError, scan_committed_tree

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
) -> None:
    """Scan every git-tracked file for committed banned terms.

    The brand list comes from the ``banned_term_registry`` DB row or
    ``$TEATREE_TERM_REGISTRY`` CI secret.
    """
    root = repo_root if repo_root is not None else Path.cwd()
    try:
        result = scan_committed_tree(root)
    except BannedTermsUnsetError as exc:
        # A genuinely-unset brand list (no config, no env, a missing key) is
        # refused LOUD (exit 2) — never a silent inert scan that hides a load
        # bug. An explicit empty leak class is refused as well.
        _console.print(f"[red]banned-terms scan-tree: MISCONFIGURED — {exc}[/]")
        raise typer.Exit(_MISCONFIGURED_EXIT_CODE) from exc
    except TreeEnumerationError as exc:
        # The second axis of the same invariant: a tree the scan could not
        # enumerate yields zero findings for a reason that has nothing to do
        # with the tree being clean (#4354).
        _console.print(f"[red]banned-terms scan-tree: MISCONFIGURED — {exc}[/]")
        raise typer.Exit(_MISCONFIGURED_EXIT_CODE) from exc

    if not result.findings:
        _console.print("[green]banned-terms scan-tree: clean (0 findings).[/]")
        return

    _console.print(f"[red]banned-terms scan-tree: {len(result.findings)} committed banned-term finding(s).[/]")
    for finding in result.findings:
        _console.print(f"  {finding.render()}")
    _console.print(
        "\nA banned term is committed to the tree. Scrub it "
        "(the diff-only gate cannot see it — it is not in any new diff)."
    )
    raise typer.Exit(_FINDINGS_EXIT_CODE)
