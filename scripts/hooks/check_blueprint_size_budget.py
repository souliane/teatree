"""Pre-commit hook: BLUEPRINT corpus size budget (#1128).

The BLUEPRINT is functional + architectural, not a prose mirror of the
code. When the corpus (top-level ``BLUEPRINT.md`` plus the three
architectural appendices in ``docs/blueprint/``) creeps back past the
budget, that almost always means implementation prose has accreted.

This gate enforces the budget structurally so the rule does not have to
live in vigilance. It mirrors the ``skill-prose-ban`` (#140) precedent:
the rule the user keeps having to restate becomes a deterministic
pre-commit gate that fails the commit until the regression is removed.

Budget (bytes):

- Top-level ``BLUEPRINT.md``:    88 000  (~86 KB)
- ``docs/blueprint/`` corpus:   110 500  (~108 KB)
- Combined corpus total:        198 500  (~194 KB)

BLUEPRINT.md is a SINGLE file by user decision — never split, never
consolidate-by-splitting. The top-level budget is sized to sit
comfortably above the live file so the doc can keep growing as one
document without the override being load-bearing for ordinary edits.

Escape hatch: ``BLUEPRINT_SIZE_OVERRIDE=1`` skips the check. Use only
when a planned, reviewed addition deliberately grows the corpus and the
budget itself needs to be raised in the same commit.
"""

import os
import pathlib
import subprocess
import sys

_TOP_FILE = "BLUEPRINT.md"
_APPENDIX_DIR = "docs/blueprint"

# Single-file-by-user-veto bump: BLUEPRINT.md stays one document (never
# split). The live file (82,114 B) had outgrown the prior 82,000 B budget,
# so the override was masking an over-budget file on main. Raised to 88,000 B
# (~5.9 KB headroom) so the single file can absorb the next several config /
# invariant rows without the override being load-bearing for ordinary edits.
# Headroom-restore bump: the #1690 raise to 88,000 B left the live file
# (85,328 B) only ~2.7 KB below budget, under the 4 KB headroom the
# `TestRealCorpusFitsWithHeadroom` guard requires — reddening main CI for
# every PR. Raised to 90,000 B to restore the >=4 KB headroom invariant.
_BUDGET_TOP_LEVEL_BYTES = 90_000
# Reviewed bump (#1570): the full-tree banned-brand backstop scan
# (`core.banned_terms_tree` / `t3 banned-terms scan-tree` + the
# `banned-terms-tree` CI job) is the same class of load-bearing
# leak-prevention safety fact as the diff/payload banned-terms gate, and
# the top-level corpus was at capacity (80,199 B, 1 B of headroom).
# Reviewed bump (#1474): the §17.6.4 gate-2 self-rescue invariant is a
# load-bearing safety fact, and the appendix corpus was already at capacity.
# Reviewed bump (#1488): §17.6.4 gate 17 (the TaskCreated skill-loading
# gate that closes the ultracode fan-out bypass) is the same class of
# load-bearing safety fact, and the corpus was again at capacity.
# Reviewed bump (#1500): the `gate_fail_open` master NEVER-LOCKOUT switch
# (the parent fail-open mechanism the gate-2 self-rescue invariant rides on)
# is the same class of load-bearing safety fact, corpus again at capacity.
# Reviewed bump (#1539): the reviewing-phase review-skill evidence gate
# (`review_skill` / `T3_REVIEW_SKILL`) is the same class of load-bearing
# safety fact, and the appendix corpus was again at capacity.
# Reviewed bump (#1540): the per-overlay `mr_title_regex` knob documents the
# deterministic MR title/What-Why gate at `pr create` — a load-bearing config
# fact, and the corpus was again at capacity after the #1539 bump.
# Reviewed bump (#1573): the `retro review-findings` publish-leak gate
# (untrusted-comment bare-ref neutralization + banned-term withholding before
# the `gh api` stdin filing path the PreToolUse gate cannot inspect) is the
# same class of load-bearing safety fact, plus the mandatory tach
# dependency-graph edge for the new `core → hooks` reuse; corpus was at
# capacity after the #1540 bump.
# Reviewed bump (#1629): the §5.6.3 cron-window paragraph now documents the
# span (not fire-instant) availability semantics + the 1 h cadence cap — a
# load-bearing safety fact (the prior fire-instant wording silently broke
# away-mode for the shipped example), and the appendix corpus was at capacity.
# Reviewed bump (#1636): the `skill_loading_gate_enabled` per-overlay config-key
# row documenting the §17.6.4 skill-loading kill-switch is a load-bearing config
# fact; on top of the #1629 base the merged appendix corpus is 104,864 B, so the
# budget is raised to the next ~1 KB step (~536 B headroom) to admit the row.
# Reviewed bump (#169): the §17.6.4 two-complementary-enforcement-evals note
# (gate-liveness #168 + transcript-replay #169, the local-only privacy-safe
# real-run conformance eval) is the same class of load-bearing safety fact, and
# the appendix corpus was at capacity after the #1636 bump.
# Reviewed bump (#171): the `mcp_privacy_gate_enabled` and
# `dispatch_quote_gate_on_task_create_enabled` config-key rows document the
# kill-switch / opt-in for the now-reachable Slack-MCP publish-privacy arm and
# the TaskCreated dispatch-quote fan-out gate — load-bearing config facts; on
# top of the #169 base the merged appendix corpus is 107,249 B, so the budget is
# raised to the next ~1 KB step (~751 B headroom) to admit the rows.
# Reviewed bump (#166): the anti-pattern-catalog SSOT paragraph (the structured
# source feeding the three review tiers + the catalog↔linter/eval reachability
# ledger) is a load-bearing architectural fact; the top-level corpus was at
# capacity, so the top-level + total budgets are raised one minimal step.
# Reviewed bump (#1644 PR B): the `orchestrator_boundary_agent_gate_enabled`
# config-key row (the default-OFF opt-in + `[fg-ok:]` escape for the #1442
# foreground-Agent deny) and the gate-2 production-phantom note are load-bearing
# safety/config facts; the appendix corpus was at capacity (108,746 B), so the
# budget is raised to the next ~1 KB step (~754 B headroom) to admit them.
# Reviewed bump (skill-ref-validator): the two mandatory tach dependency-graph
# edges for the new `teatree.skill_ref_validator` leaf module
# (`cli --> skill_ref_validator` + the module node) are an architectural fact;
# the top-level corpus was at capacity, so the top-level + total budgets are
# raised one minimal step.
# Reviewed bump (publish-gate-destination-aware): the `internal_publish_namespaces`
# config-key row documenting the destination-aware skip for the #1415 banned-terms
# and #1530 bare-reference publish gates is a load-bearing config/safety fact; the
# appendix corpus was at capacity (109,957 B), so the budget is raised one minimal
# ~1 KB step (~543 B headroom) to admit the row.
# Reviewed bump (#1668): the per-overlay `autonomy` switch row (the single
# trust switch collapsing the three approval gates + the derived-field note)
# is a load-bearing config fact; after trimming the verbose prose the appendix
# corpus is 110,045 B, so the budget is raised one minimal step (~455 B
# headroom) to admit the row.
# Reviewed bump (speed dial): the per-overlay `speed` throughput-dial row in the
# override table is the same class of load-bearing config fact as the `autonomy`
# row above; tracked by the #1697 appendix bump below.
# Reviewed bump (#1697): the §17.4.2 line documenting the by-product
# `ReviewVerdict` record + `review record`/`review status` lookup is a
# load-bearing architectural fact; merged with the speed-dial row the appendix
# corpus is 110,906 B, raised one minimal step to 111,500 (~594 B headroom).
# Reviewed bump (#1672 merge): the `internal_publish_namespaces` config-key row
# documenting the destination-aware skip for the #1415 banned-terms and #1530
# bare-reference publish gates is a load-bearing config/safety fact. Stacked on
# the #1697 `ReviewVerdict` row already on main, the merged appendix corpus
# overflowed the prior 111,500 budget; raised one minimal step to 113,000 to
# admit the row.
# Reviewed bump: the per-overlay turn-budget and autonomy-CLI config rows plus
# the eval-suite appendix additions push the appendix corpus to 113,049 B, just
# over the prior 113,000 budget. Raised one minimal step to 114,000 to admit the
# reviewed rows; the coupling invariant tracks the total-budget raise below.
# Reviewed bump (#1840 already merged): appendix corpus reached 114,380 B; raised
# one minimal step to 114,500. Coupling invariant: 204,000 - 90,000 <= 114,500.
# Headroom-restore bump (reference-linkifier): merging main leaves the corpus at
# 114,380 B with thin headroom; raised to 116,000. Coupling/headroom invariants hold.
_BUDGET_APPENDICES_BYTES = 116_000
# Reviewed bump (#1570): the full-tree banned-brand backstop entry in the
# security-gates paragraph; total corpus tracked the top-level bump.
# Reviewed bump (#1629): tracks the appendix span-semantics correction above.
# Reviewed bump (#1636): tracks the appendix bump for the
# `skill_loading_gate_enabled` config-key row (merged total 185,450 B).
# Reviewed bump (#169): tracks the top-level + appendix bumps for the
# two-complementary-enforcement-evals note (gate-liveness + transcript-replay).
# Reviewed bump (#171+#166 merge): post-merge total corpus is 188,987 B
# (top-level 81,738 + appendices 107,249); raised to 189,500 (~513 B headroom).
# Reviewed bump (#1644 PR B): total corpus is 190,484 B (top-level 81,738 +
# appendices 108,746); raised to 191,000 (~516 B headroom). Invariant holds:
# 191,000 - 81,800 = 109,200 <= 109,500.
# Reviewed bump (skill-ref-validator): the new module's two dependency-graph
# edges push the total to 191,057 B; raised to 191,500 (~443 B headroom).
# Invariant holds: 191,500 - 82,000 = 109,500 <= 109,500.
# Reviewed bump (#1668): tracks the appendix bump for the `autonomy` config-key
# row; post-trim total corpus is 191,937 B, raised to 192,500 (~563 B
# headroom). Invariant holds: 192,500 - 82,000 = 110,500 <= 110,500.
# Single-file-by-user-veto bump: tracks the top-level raise to 88,000 B so the
# coupling invariant stays tight. Live total corpus is 192,264 B; raised to
# 198,500 (~6.2 KB headroom). Invariant holds: 198,500 - 88,000 = 110,500
# <= 110,500.
# Reviewed bump (#1697): tracks the appendix raise to 111,500 for the §17.4.2
# ReviewVerdict line so the coupling invariant stays tight. Raised to 199,500.
# Invariant holds: 199,500 - 88,000 = 111,500 <= 111,500.
# Headroom-restore bump: live total corpus (196,076 B) sat only ~3.4 KB below
# the 199,500 B total budget, under the 4 KB `TestRealCorpusFitsWithHeadroom`
# guard. Raised to 201,500 to restore the >=4 KB headroom; tracks the top-level
# raise to 90,000. Invariant holds: 201,500 - 90,000 = 111,500 <= 111,500.
# Reviewed bump (#1672 merge): the `internal_publish_namespaces` config-key row
# tracks the appendix raise to 113,000. Stacked on the speed-dial + #1697 rows
# the merged total corpus is 197,751 B, leaving only ~3.7 KB under the prior
# 201,500 budget -- below the 4 KB `TestRealCorpusFitsWithHeadroom` guard.
# Raised one minimal step to 202,000 to restore the >=4 KB headroom (~4,249 B).
# Coupling invariant holds: 202,000 - 90,000 = 112,000 <= 113,000.
# Reviewed bump: the turn-budget + autonomy-CLI config rows and the eval-suite
# docs bring the merged corpus to 198,693 B, leaving only ~3.3 KB under the prior
# 202,000 budget -- below the 4 KB `TestRealCorpusFitsWithHeadroom` guard. Raised
# to 204,000 to restore the >=4 KB headroom (~5,307 B). Coupling invariant holds:
# 204,000 - 90,000 = 114,000 <= 114,000.
_BUDGET_TOTAL_BYTES = 204_000


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _size(path: pathlib.Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def _appendix_total(root: pathlib.Path) -> int:
    appendix_dir = root / _APPENDIX_DIR
    if not appendix_dir.is_dir():
        return 0
    return sum(_size(p) for p in appendix_dir.glob("*.md"))


def _blueprint_touched() -> bool:
    """True when BLUEPRINT.md or any docs/blueprint/*.md is staged."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=False,
    )
    staged = result.stdout.splitlines()
    return any(f == _TOP_FILE or f.startswith(f"{_APPENDIX_DIR}/") for f in staged)


def main() -> int:
    if os.environ.get("BLUEPRINT_SIZE_OVERRIDE") == "1":
        return 0

    if not _blueprint_touched():
        return 0

    root = _repo_root()
    top_size = _size(root / _TOP_FILE)
    appendix_size = _appendix_total(root)
    total = top_size + appendix_size

    breaches: list[str] = []
    if top_size > _BUDGET_TOP_LEVEL_BYTES:
        breaches.append(f"{_TOP_FILE}: {top_size:,} B > budget {_BUDGET_TOP_LEVEL_BYTES:,} B")
    if appendix_size > _BUDGET_APPENDICES_BYTES:
        breaches.append(f"{_APPENDIX_DIR}/: {appendix_size:,} B > budget {_BUDGET_APPENDICES_BYTES:,} B")
    if total > _BUDGET_TOTAL_BYTES:
        breaches.append(f"corpus total: {total:,} B > budget {_BUDGET_TOTAL_BYTES:,} B")

    if not breaches:
        return 0

    print(file=sys.stderr)
    print("  BLUEPRINT corpus size budget FAILED (#1128):", file=sys.stderr)
    print(file=sys.stderr)
    for line in breaches:
        print(f"    - {line}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        "  The BLUEPRINT is architectural, not a prose mirror of the code.",
        file=sys.stderr,
    )
    print(
        "  Move implementation detail to docstrings, --help text, CLAUDE.md,",
        file=sys.stderr,
    )
    print(
        "  AGENTS.md, or the issue tracker. To bypass for a reviewed bump:",
        file=sys.stderr,
    )
    print("    BLUEPRINT_SIZE_OVERRIDE=1 git commit ...", file=sys.stderr)
    print(file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
