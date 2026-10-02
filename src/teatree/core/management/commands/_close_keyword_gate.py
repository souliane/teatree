"""Pre-push gate: reject auto-close keywords for overlays that forbid them.

Some overlays manage issue closure through the forge's linked-items API,
not through ``Closes #N`` / ``Fixes #N`` / ``Resolves #N`` auto-close
trailers (#1012). A trailer that slips into an MR description or a commit
body auto-closes the referenced issue the instant the MR merges, which then
fires the "ticket closed" cleanup in followup and breaks the lifecycle FSM.

This gate is overlay-scoped: it only enforces when the active overlay sets
``config.forbid_close_keywords``. teatree's own overlay leaves it at the
``False`` default, so teatree PRs that legitimately use ``Closes #N`` are
unaffected. It scans both the proposed MR description and every commit body
on the branch, and raises ``SystemExit`` with the offending line + suggested
rewrite when an auto-close keyword is found.

Detection uses ``teatree.core.runners.ship.CLOSE_KEYWORD_RE`` — the single
source of truth shared with ``sanitize_close_keywords`` so the gate and the
auto-rewrite stay in lockstep, including the colon form GitLab auto-closes
(#1090).
"""

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.core.runners.ship import CLOSE_KEYWORD_RE
from teatree.utils import git
from teatree.utils.run import CommandFailedError


def _offending_lines(text: str) -> list[str]:
    """Return each line of *text* that carries a forbidden auto-close keyword."""
    return [line.strip() for line in text.splitlines() if CLOSE_KEYWORD_RE.search(line)]


def _suggest_rewrite(line: str) -> str:
    """Rewrite the keyword to the allowlisted ``Relates to`` form."""
    return CLOSE_KEYWORD_RE.sub(lambda m: f"Relates to {m.group('ref')}", line)


def _scan_sources(worktree: Worktree) -> list[str]:
    """Raw author-intent text to scan: the MR description + every commit body.

    The MR description is *derived from* the branch's last commit message
    (see ``ship_preview``), so the raw last-commit message is the proposed
    description's source. It is scanned RAW — before ``sanitize_close_keywords``
    rewrites it — because that rewrite would otherwise mask the very trailer
    this gate must reject. A git failure (no real repo, base undetectable,
    detached HEAD) is *unverifiable*: skip that source rather than block on
    an unknown.
    """
    repo = (worktree.extra or {}).get("worktree_path", "") or worktree.repo_path
    branch = worktree.branch
    if not repo or not branch:
        return []
    sources: list[str] = []
    try:
        subject, body = git.last_commit_message(repo=repo)
        sources.append(f"{subject}\n\n{body}" if body else subject)
        base = f"origin/{git.default_branch(repo=repo)}"
        sources.extend(git.commit_messages(repo=repo, range_spec=f"{base}..{branch}"))
    except (CommandFailedError, RuntimeError, ValueError):
        return sources
    return sources


def run_close_keyword_gate(ticket: Ticket, worktree: Worktree) -> None:
    """Reject forbidden auto-close keywords for overlays that opt in.

    No-op when the active overlay does not set
    ``config.forbid_close_keywords``. Raises ``SystemExit(1)`` (the CLI
    gate convention) with the offending line(s) + suggested rewrite when
    a keyword is found in the MR description or any branch commit body.
    """
    _ = ticket  # signature parity with the other ``_run_ship_gates`` steps
    if not get_overlay().config.forbid_close_keywords:
        return

    sources = _scan_sources(worktree)

    offenders: list[str] = []
    for source in sources:
        offenders.extend(_offending_lines(source))
    if not offenders:
        return

    bullets = "\n".join(f"  - {line}\n    → suggest: {_suggest_rewrite(line)}" for line in offenders)
    msg = (
        "Refusing to ship: this overlay manages issue closure via forge "
        "linked-items, not auto-close trailers. The MR description or a "
        "commit body contains a forbidden auto-close keyword:\n"
        f"{bullets}\n"
        "Rewrite the trailer to `Relates to` (or `Refs`/`See`) and retry "
        "`pr create`."
    )
    raise SystemExit(msg)
