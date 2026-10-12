"""Shared test-infra for the merge void: a landed ticket, a stubbed forge, a fake terminal, a human operator.

State is built through the real seams (``reconcile_merged`` on a keystone audit, then
``plan_void`` + ``apply_void``), so a test reads what the command would have written.
"""

import io
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from teatree.core.evidence.forge_content import PrContentSnapshot
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.merge.void_merge import apply_void, plan_void
from teatree.core.models import MergeVoid, PullRequest, Ticket
from teatree.utils.agent_spawn_marker import DISPATCHED_AGENT_ENV
from teatree.utils.env import patched_environ
from tests._loop_principal_env import pinned_loop_principal
from tests.factories import MergeAuditFactory, MergeClearFactory

SLUG = "example-org/example-repo"
PR_URL = f"https://github.com/{SLUG}/pull/41"
ISSUE_URL = f"https://github.com/{SLUG}/issues/7"
MERGED_SHA = "a" * 40
CONTENT_HEAD = "b" * 40
TIP = "c" * 40
MERGE_BASE = "e" * 40
ABSENT = PrContentSnapshot(
    merged=True,
    merge_commit=MERGED_SHA,
    content_head=CONTENT_HEAD,
    merge_base=MERGE_BASE,
    default_branch="main",
    default_tip=TIP,
    base_blobs={"src/a.py": "a0", "src/b.py": "b0"},
    head_blobs={"src/a.py": "a1", "src/b.py": "b1"},
    merge_blobs={"src/a.py": "a9", "src/b.py": "b9"},
    tip_blobs={"src/a.py": "a9", "src/b.py": "b9"},
)


class Terminal(io.StringIO):
    def __init__(self, typed: str = "", *, tty: bool = True) -> None:
        super().__init__(typed)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def terminal(typed: str = "yes\n", *, tty: bool = True) -> dict[str, Terminal]:
    return {"stdin": Terminal(typed, tty=tty), "stdout": Terminal(tty=tty)}


@contextmanager
def human_operator() -> Iterator[None]:
    """Hermetic against a test run by a dispatched agent or inside a loop tick."""
    with pinned_loop_principal(), patched_environ({}, remove=(DISPATCHED_AGENT_ENV,)):
        yield


@contextmanager
def stub_forge(*snapshots: PrContentSnapshot | Exception) -> Iterator[MagicMock]:
    probe = MagicMock(side_effect=list(snapshots or (ABSENT, ABSENT)))
    with patch.object(CodeHostQuery, "pr_content_snapshot", probe):
        yield probe


def landed_ticket(
    *, from_state: str = Ticket.State.CODED, issue_url: str = ISSUE_URL, pr_url: str = PR_URL, with_row: bool = True
) -> Ticket:
    """A ticket walked ``from_state -> merged`` by ``reconcile_merged`` on the keystone's audit."""
    pr_id = int(pr_url.rsplit("/", 1)[-1])
    ticket = Ticket.objects.create(overlay="t3-teatree", state=from_state, issue_url=issue_url)
    clear = MergeClearFactory(ticket=ticket, slug=SLUG, pr_id=pr_id, reviewed_sha=CONTENT_HEAD)
    MergeAuditFactory(clear=clear, merged_sha=MERGED_SHA, repo_slug=SLUG)
    if with_row:
        PullRequest.objects.create(
            ticket=ticket, overlay="t3-teatree", url=pr_url, repo=SLUG, iid=str(pr_id), state=PullRequest.State.OPEN
        )
        PullRequest.objects.record_forge_merge(slug=SLUG, pr_id=pr_id)
    ticket.reconcile_merged()
    ticket.save()
    return ticket


def void_pr(pr_url: str = PR_URL) -> MergeVoid:
    """Void *pr_url* the way the command does, as a human answering yes at a terminal."""
    with human_operator(), stub_forge():
        plan = plan_void(pr_url)
        return apply_void(plan, reason="history rewrite dropped the change", actor="alice", **terminal())
