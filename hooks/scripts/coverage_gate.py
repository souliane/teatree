"""Per-diff coverage gate (§17.6 gate 12) — trigger, target-repo scope, argv/finding.

A bare sibling of ``hook_router.py`` (the shrink-only god-module): the coverage
gate's pure helpers live here so the router stays under its module-health cap.

Two concerns. First, the merge-class TRIGGER (:func:`is_merge_class_command`):
the verb is detected on the quote/heredoc-stripped skeleton
(:func:`mr_cli_fields.strip_quoted_and_heredoc`), so a command that merely
MENTIONS ``gh pr create`` / ``glab mr create`` inside a quoted argument, a
commit message, or a heredoc body (a python script, a doc snippet) never fires
the gate — only a real create/un-draft invocation does.

Second, resolving the repo the GATED forge command actually publishes.
``handle_block_uncovered_diff`` shells ``t3 tool diff-coverage``; that CLI
defaults ``--repo`` to its own cwd. The cold PreToolUse hook inherits the
SESSION's cwd, which — when the gated command ships a DIFFERENT worktree via a
leading ``cd <worktree>`` — is not the PR's worktree at all. Measuring the
session cwd then flags uncovered lines from an unrelated worktree's diff. The
gate must measure the worktree the command runs in: its own leading ``cd``
(anchored on the ambient cwd when relative), else the ambient cwd, walked up to
the enclosing repo root. And when the command names an EXPLICIT target repo
(``-R``/``--repo``, an api endpoint) that is NOT the measured repo's own slug,
the measured diff is some OTHER repo's unrelated work — a publish to repo X
must never be gated on uncommitted symbols in repo Y
(:func:`measured_repo_is_publish_target`). Resolution is fail-open: any
uncertainty yields the ambient cwd (or ``None``) / a skip, so the gate never
wedges a create (#122).
"""

import importlib
import json
import re
import shutil
from pathlib import Path

from hooks.scripts.forge_api_detect import _is_api_create_endpoint_write
from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.mr_cli_fields import extract_mr_target_repo, strip_quoted_and_heredoc

# The moment a PR moves toward review/merge: ``gh pr ready`` (un-drafting) or a
# non-draft ``gh pr create`` / ``glab mr create`` / an api POST to a PR/MR
# collection endpoint. ``gh pr ready --undo`` (return-to-draft, the gate's own
# remediation) and ``--draft`` creation are excluded.
_GH_PR_READY_RE = re.compile(r"\bgh\s+pr\s+ready\b")
_PR_MR_CREATE_RE = re.compile(r"\b(?:gh\s+pr\s+create|glab\s+mr\s+create)\b")
_FORGE_API_RE = re.compile(r"\b(?:gh|glab)\s+api\b")
_DRAFT_FLAG_RE = re.compile(r"(?:^|\s)(?:--draft|--undo)\b")


def is_merge_class_command(command: str) -> bool:
    """Whether ``command`` REALLY moves a PR toward review/merge.

    The verb regexes run on the quote/heredoc-stripped skeleton so a mere
    MENTION of ``glab mr create`` inside a quoted argument or a heredoc body (a
    commit message, a python script fed via ``<<EOF``) is not a trigger — the
    false-fire that gated an unrelated read-only script on the session repo's
    uncommitted diff. The api-endpoint classification
    (:func:`_is_api_create_endpoint_write`) still reads the ORIGINAL command:
    its endpoint/method arguments legitimately live inside quotes, which the
    skeleton strips.
    """
    skeleton = strip_quoted_and_heredoc(command)
    if _GH_PR_READY_RE.search(skeleton) or _PR_MR_CREATE_RE.search(skeleton):
        return not _DRAFT_FLAG_RE.search(skeleton)
    if _FORGE_API_RE.search(skeleton) and _is_api_create_endpoint_write(command):
        return not _DRAFT_FLAG_RE.search(skeleton)
    return False


def _slugs_name_same_repo(a: str, b: str) -> bool:
    """Whether two repo slugs name the same repo, host-qualification-symmetric.

    Either side may be bare (``owner/repo``) or host-qualified
    (``host/owner/repo``, the form ``slug_for_cwd`` returns); the shorter form
    must equal the trailing segments of the longer, case-insensitively, and at
    least ``owner/repo`` (two segments) must overlap — a single-segment value
    cannot identify a repo.
    """
    sa = [seg for seg in a.lower().removesuffix(".git").split("/") if seg]
    sb = [seg for seg in b.lower().removesuffix(".git").split("/") if seg]
    overlap = min(len(sa), len(sb))
    if overlap < 2:  # noqa: PLR2004 — owner/repo needs two path segments
        return False
    return sa[-overlap:] == sb[-overlap:]


def measured_repo_is_publish_target(command: str, repo_dir: Path | None) -> bool:
    """Whether *repo_dir* (the repo about to be measured) IS the command's publish target.

    ``False`` — the caller must SKIP the measurement — only when the command
    names an EXPLICIT literal target repo (``-R``/``--repo``, an api endpoint —
    :func:`mr_cli_fields.extract_mr_target_repo`) that provably is NOT
    *repo_dir*'s own repo (its git-remote slug). A cross-repo ship (`glab mr
    create -R other-org/other-repo` issued from an unrelated clone) otherwise
    measures the SESSION repo's uncommitted diff and denies the create on
    symbols the published repo never sees (§17.6.3 mis-scope).

    ``True`` everywhere else: no explicit target (the cwd repo IS the target —
    the established scope), an unexpanded ``$`` in the target, or any crash
    keeps the established cwd-scoped measurement, whose own deny path stays
    fail-open on a broken environment (#122). The one asymmetry: an explicit
    target with an UNRESOLVABLE *repo_dir* slug returns ``False`` (skip) —
    the measurement cannot be proven to be about the published repo.
    """
    if repo_dir is None:
        return True
    target = extract_mr_target_repo(command)
    if not target or "$" in target:
        return True
    try:
        with teatree_src_on_path():
            repo_visibility = importlib.import_module("teatree.hooks._repo_visibility")
            measured = repo_visibility.slug_for_cwd(repo_dir)
    except Exception:  # noqa: BLE001 — cold hook must stay crash-proof; keep the established scope
        return True
    if not measured:
        return False
    return _slugs_name_same_repo(target, measured)


# Byte-identical to ``teatree.utils.diff_coverage.UNREFERENCED_SYMBOL_IMPORT_HINT``; this
# cold-import-safe sibling cannot import ``teatree`` at module top, so the string is
# duplicated and pinned equal by a drift-guard test (test_block_uncovered_diff_hook.py).
_UNREFERENCED_SYMBOL_IMPORT_HINT = (
    "    workaround: this check reads a changed test's import statements only — a "
    "`module.symbol(...)` call does not count as a reference; when the symbol is already "
    "exercised, add `from module import symbol` to a changed test to make the reference visible"
)


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
        rows.extend(
            (
                f"  new production symbols not referenced by any changed test: {sorted(symbols)}",
                _UNREFERENCED_SYMBOL_IMPORT_HINT,
            )
        )
    return "\n".join(rows)
