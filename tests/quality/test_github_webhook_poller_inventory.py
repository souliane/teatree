"""Pin the documented forge pollers to their shipped loop cadences."""

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_EXPECTED_ROWS = (
    "| `PrApprovalScanner` | `inbox` | 60 s | Approvals of PRs awaiting review. |",
    "| `ReviewerPrsScanner` | `review` | 300 s | Review requests and review state. |",
    "| `ReviewedPrHeadScanner` | `review` | 300 s | Live heads of reviewer tickets. |",
    "| `MyPrsScanner` | `ship` | 300 s | Authored PRs, CI, and mergeability. |",
    "| `MrConflictScanner` | `ship` | 300 s | Merge conflicts on authored PRs. |",
    "| `MrTriageScanner` | `ship`, `followup` | 300 s / 1,800 s | Review-request state of authored PRs. |",
    "| `UnauditedMergeScanner` | `ship` | 300 s | Merged-PR search and merge audit state. |",
    "| `PrSweepScanner` | `ship` | 300 s | Merge readiness and PR state. |",
    "| `TicketDispositionScanner` | `tickets` | 300 s | Tracked issue state, assignees, and labels. |",
    "| `TicketCompletionScanner` | `tickets` | 300 s | Completion state of post-ship issues. |",
    "| `TaskSweepScanner` | `tickets` | 300 s | Terminal state of task artifacts. |",
    "| `IssueIntakeScanner` | `issue_implementer` | 1,800 s | Open issues eligible for implementation. |",
    "| `IssueDispositionScanner` | `issue_disposition` | 300 s | Open issues eligible for dead-issue closure. |",
    "| `ReviewNagScanner` | `followup` | 1,800 s | Open review requests and PR conversation comments. |",
    "| `ReviewRequestResumeScanner` | `followup` | 1,800 s | Review-request state and PR conversation comments. |",
    "| `ReviewRequestMergeReactScanner` | `followup` | 1,800 s | Merged review requests awaiting a reaction. |",
    "| `BoardReconcileScanner` | `housekeeping` | 3,600 s | Forge truth for tracked tickets and PRs. |",
    "| `SelfUpdateScanner` | `housekeeping` | 3,600 s | Default-branch check-runs before self-update. |",
    "| `GitHubSyncBackend` | `followup sync` (on demand) | on demand | GitHub project-board items. |",
)


def test_github_poller_rows_and_cadences_match_shipped_loops() -> None:
    document = (_ROOT / "docs/blueprint/loop-topology.md").read_text(encoding="utf-8")
    section = document.partition("**GitHub webhook event inventory.**")[2].partition("**Forge relay identity.**")[0]
    header = "| Poller | Loop | Shipped cadence | Forge reads still in use |"
    assert header in section
    table = section.partition(header)[2].partition("\n\n")[0]
    rows = tuple(line for line in table.splitlines() if line.startswith("| `"))

    assert rows == _EXPECTED_ROWS

    shipped = tomllib.loads((_ROOT / "src/teatree/config/defaults.toml").read_text(encoding="utf-8"))["loops"]
    for row in rows:
        _, _, loop, cadence, _, _ = (cell.strip() for cell in row.split("|"))
        if cadence == "on demand":
            assert loop == "`followup sync` (on demand)"
        else:
            for loop_name, seconds in zip(
                re.findall(r"`([^`]+)`", loop), re.findall(r"([\d,]+) s", cadence), strict=True
            ):
                assert shipped[loop_name]["delay_seconds"] == int(seconds.replace(",", ""))
