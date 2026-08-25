"""``t3 tool ratchet-prune`` — report and repair the reference ratchets' stale pins.

The local mirror of what CI asserts over
:mod:`teatree.quality.ref_baseline`'s two shrink-only ratchets, plus the one
mechanical repair that is safe to automate.

``--check`` (the default) exits non-zero when either ratchet is dirty in either
direction, so it answers "would this tree red the ratchet?" without a pytest run.
``--write`` deletes the STALE pins only — the ones the scanner no longer reports.
That direction is auto-repairable because deleting a pin makes the guard strictly
tighter; the other direction, an unresolved reference no pin covers, LOOSENS the
guard when banked, so it is reported and never written.

The staleness half is what reddened ``main`` in #4451: two PRs off a shared base,
one seeding a pin while the other rewrote the citation it named. Neither
``refs/pull/N/merge`` contained the other, so both were green alone and the merge
was red. The repair is a one-entry deletion — mechanical, and now one command.
"""

import json

import typer

from teatree.quality import ref_baseline
from teatree.quality.ref_baseline import RATCHETS, Baseline

_STALE_HINT = "Run `t3 tool ratchet-prune --write` to delete exactly these."
_NEW_HINT = "Fix the citation, or add a `skill-symbol-ref:` pragma on the citing line. These are never auto-banked."


def _rows(entries: Baseline) -> list[tuple[str, str, str]]:
    return [(name, path, ref) for name in RATCHETS for path, ref in sorted(entries[name])]


def _render(title: str, rows: list[tuple[str, str, str]], hint: str = "") -> str:
    body = "\n".join(f"  {name:<13} {path}  {ref}" for name, path, ref in rows)
    return f"{title}\n{body}\n{hint}".rstrip()


def ratchet_prune(
    *,
    write: bool = typer.Option(False, "--write", help="Delete the stale pins (the only auto-repairable direction)."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Report the reference ratchets' drift; with --write, delete the stale pins.

    Exits non-zero when either ratchet is dirty in either direction, matching the
    assertions in ``tests/teatree_quality/test_skill_symbol_refs.py``.
    """
    stale_rows = _rows(ref_baseline.prune(write=write) if write else ref_baseline.stale_entries())
    new_rows = _rows(ref_baseline.new_entries())

    if json_output:
        payload = {
            "written": write,
            "stale": [{"ratchet": r, "path": p, "ref": f} for r, p, f in stale_rows],
            "new": [{"ratchet": r, "path": p, "ref": f} for r, p, f in new_rows],
        }
        typer.echo(json.dumps(payload, indent=2))
    else:
        if write and stale_rows:
            typer.echo(_render(f"Deleted {len(stale_rows)} stale pin(s) from known_unresolved_refs.yaml:", stale_rows))
        elif write:
            typer.echo("ratchet-prune: no stale pins — known_unresolved_refs.yaml is unchanged.")
        elif stale_rows:
            typer.echo(_render(f"{len(stale_rows)} stale pin(s):", stale_rows, _STALE_HINT), err=True)
        if new_rows:
            typer.echo(
                _render(f"{len(new_rows)} unresolved reference(s) no pin covers:", new_rows, _NEW_HINT), err=True
            )
        if not write and not stale_rows and not new_rows:
            typer.echo("ratchet-prune: both reference ratchets are clean.")

    # --write already repaired the stale half, so only the un-repairable half still fails.
    if new_rows or (stale_rows and not write):
        raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    """Register this module's ``t3 tool`` command(s) onto *app* (called from ``cli/__init__``)."""
    app.command("ratchet-prune")(ratchet_prune)
