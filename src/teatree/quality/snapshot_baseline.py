"""Deterministic engine for the visual-baseline attestation gate.

A Playwright visual-regression baseline lives under a ``__snapshots__/``
directory (``e2e/foo.spec.ts-snapshots/`` for the file-scoped variant, or a
project ``__snapshots__/`` dir). A commit that adds or rewrites one of those
PNG/`.txt`/`.snap` baselines silently changes what "looks correct" means — the
next run compares against the new image, so a regression baked into the new
baseline reads green forever. The only thing that separates a legitimate
baseline update from a regression frozen into the reference is that a human (or
an agent) *looked at the rendered result and attested it is correct*.

This module is the pure, greppable core the ``check_snapshot_baseline`` prek
hook consumes: it decides, from the staged path list alone, which files are
visual baselines. The attestation lookup (a green + posted
:class:`~teatree.core.models.e2e_mandatory_run.E2eMandatoryRun` for the ticket)
and the git/Django I/O live in the hook, so this stays a pure function tested
in isolation. Nothing here reaches the DB, the filesystem, or git.
"""

import re
from collections.abc import Iterable

#: A file is a visual baseline when a path segment is ``__snapshots__`` OR the
#: segment ends in ``-snapshots`` (Playwright's per-spec ``<spec>-snapshots/``
#: convention). ``(?:^|/)`` anchors the segment start; ``[^/]*-snapshots/``
#: catches ``login.spec.ts-snapshots/`` without matching ``my-snapshots-util``.
_BASELINE_PATH_RE = re.compile(r"(?:^|/)(?:__snapshots__|[^/]*-snapshots)/")


def is_snapshot_baseline(path: str) -> bool:
    """True when *path* is a Playwright visual-regression baseline file.

    Matches any file nested under a ``__snapshots__/`` directory or a
    ``<spec>-snapshots/`` directory at any depth. A source file that merely
    mentions "snapshot" in its name (``snapshot_warmer.py``) does not match —
    the marker is a whole path *segment*, not a substring.
    """
    return bool(_BASELINE_PATH_RE.search(path))


def snapshot_baselines(paths: Iterable[str]) -> list[str]:
    """Return the subset of *paths* that are visual baselines, order-preserving."""
    return [path for path in paths if is_snapshot_baseline(path)]


def block_message(baselines: list[str], *, ticket_ref: str, record_command: str) -> str:
    """The refusal shown when baselines are staged with no visual attestation.

    Names every offending baseline plus the exact ``record-e2e-run`` command
    that mints the attestation, so the operator can unblock without guessing —
    a baseline change is only sanctioned once its rendered result was verified
    and that verification was posted (the same green + posted evidence the
    mandatory-E2E gate consumes).
    """
    files = "\n".join(f"  - {path}" for path in baselines)
    return (
        f"Snapshot-baseline gate: this commit adds/rewrites a visual "
        f"regression baseline for ticket {ticket_ref} with no recorded visual "
        f"verification:\n\n"
        f"{files}\n\n"
        f"A new baseline silently redefines what 'looks correct' — a regression "
        f"frozen into the reference reads green forever. Verify the rendered "
        f"result, post the evidence, and attest it (the green + posted E2E run "
        f"the mandatory-E2E gate already consumes):\n"
        f"  {record_command}\n"
        f"then re-commit. To sanction a genuinely-verified baseline change without "
        f"the attestation, set ALLOW_SNAPSHOT_BASELINE='<reason>' on the commit."
    )
