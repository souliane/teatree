"""Per-diff coverage gate (§17.6 gate 12) — target-repo resolution + argv/finding.

A bare sibling of ``hook_router.py`` (the shrink-only god-module): the coverage
gate's pure helpers live here so the router stays under its module-health cap.

The concern this owns is resolving the repo the GATED forge command actually
targets. ``handle_block_uncovered_diff`` shells ``t3 tool diff-coverage`` on a
merge-class ``gh pr create`` / ``glab mr create`` / ``gh pr ready``; that CLI
defaults ``--repo`` to its own cwd. The cold PreToolUse hook inherits the
SESSION's cwd, which — when the gated command ships a DIFFERENT worktree via a
leading ``cd <worktree>`` — is not the PR's worktree at all. Measuring the
session cwd then flags uncovered lines from an unrelated worktree's diff. The
gate must measure the worktree the command runs in: its own leading ``cd``
(anchored on the ambient cwd when relative), else the ambient cwd, walked up to
the enclosing repo root. Resolution is fail-open: any uncertainty yields the
ambient cwd (or ``None``), so the gate never wedges a create.
"""

import importlib
import json
import shutil
from pathlib import Path

from hooks.scripts.managed_repo import teatree_src_on_path


def coverage_gate_repo_dir(command: str, cwd: str | None) -> Path | None:
    """Return the repo root whose diff the gated forge command should be measured against.

    The gated ``gh pr create`` / ``glab mr create`` / ``gh pr ready`` runs in its
    own leading ``cd <dir>`` (a cross-worktree ship), NOT the session cwd the cold
    hook inherits. Resolving that ``cd`` — anchored on the ambient *cwd* when
    relative — and walking up to the enclosing repo root keeps the coverage
    measurement on the PR's OWN worktree, never a sibling worktree's stray diff.

    Fail-open: when neither a leading ``cd`` nor an ambient *cwd* resolves (or the
    ``teatree`` src bootstrap the ``cd`` parser needs is unavailable), returns the
    ambient *cwd* if any, else ``None`` — the caller then runs cwd-relative as
    before, so a broken environment never denies a create.
    """
    ambient = Path(cwd) if cwd else None
    try:
        with teatree_src_on_path():
            # ``teatree.hooks._commit_repo_dir`` is a private module reached from a
            # cold-hook sibling; import it dynamically so the parsers stay a single
            # source of truth without a static private-name import.
            commit_repo_dir = importlib.import_module("teatree.hooks._commit_repo_dir")
            cd_dir = commit_repo_dir.leading_cd_dir(command)
            if cd_dir is not None:
                parsed = Path(cd_dir)
                target = parsed if parsed.is_absolute() or ambient is None else ambient / parsed
            else:
                target = ambient
            if target is None:
                return None
            return commit_repo_dir.git_root_for_dir(target) or target
    except Exception:  # noqa: BLE001 — cold hook must stay crash-proof; degrade to the ambient cwd
        return ambient


def diff_coverage_argv(repo_dir: Path | None) -> list[str] | None:
    """Return the ``t3 tool diff-coverage --json`` argv keyed to *repo_dir*, or ``None``.

    ``None`` when ``t3`` is not on PATH (the gate then fails open). ``--repo`` is
    appended only when *repo_dir* resolved, so a cwd-relative run (the historical
    behaviour) is preserved when no target could be pinned.
    """
    t3_bin = shutil.which("t3")
    if t3_bin is None:
        return None
    argv = [t3_bin, "tool", "diff-coverage", "--json"]
    if repo_dir is not None:
        argv += ["--repo", str(repo_dir)]
    return argv


def diff_coverage_finding(stdout: str) -> str | None:
    """Return a deny reason iff *stdout* is a report JSON with ``passes`` false.

    The fail-open discriminator (#122). ``t3 tool diff-coverage --json`` emits
    exactly ``{"passes": ..., "uncovered": [...], "unreferenced_symbols": [...]}``
    on a successful measurement. A crash (e.g. the dev-only ``coverage`` module
    missing from the installed ``t3`` env) produces a traceback on stderr and no
    parseable JSON on stdout — so anything that is not a well-formed report with
    ``passes is False`` is "not a finding" and the caller fails open.

    Returns the human-readable finding summary when there IS a genuine finding,
    else ``None`` (clean, crashed, or unparsable).
    """
    try:
        report = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(report, dict) or report.get("passes") is not False:
        return None
    rows = [
        f"  uncovered new lines in {entry.get('path')}: {entry.get('lines')}"
        for entry in (report.get("uncovered") or [])
        if isinstance(entry, dict)
    ]
    symbols = report.get("unreferenced_symbols") or []
    if symbols:
        rows.append(f"  new production symbols not referenced by any changed test: {sorted(symbols)}")
    return "\n".join(rows)
