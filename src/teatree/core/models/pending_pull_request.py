"""The durable obligation an ``ensure-pr`` deferral leaves behind (#792 follow-up).

``ensure-pr`` runs in the git PRE-push hook, where the remote ref is absent (a
first push) or still lags the in-flight push. Creating the PR there aborts the
very push that would make it creatable, so the create is deferred — and git has
no client-side post-push hook, so the deferral had no drain: it exited 0 having
observed work and stored nothing, and the branch shipped with no PR at all.

A deferral now owes a row here. :class:`~teatree.loop.scanners.pending_pr.PendingPrDrainScanner`
re-runs the idempotent ``ensure-pr`` path once per dispatch tick and discharges
the row when the PR exists (or the branch turns out to need none); a row that
survives :data:`MAX_DRAIN_ATTEMPTS` drains is a LOUD ``t3 doctor check`` FAIL,
never a silent retry, and ``t3 <overlay> pr discharge-pending <id>`` is the
operator's escape hatch when a branch genuinely needs no PR.

``spec`` is the ``PullRequestSpec`` the deferring caller had already built. It is
DIAGNOSTIC only — the drain re-runs the whole ``ensure-pr`` path, which rebuilds
the spec from the worktree, so a stored one is never replayed. What it buys is a
doctor FAIL that names the intended PR (:attr:`PendingPullRequest.intended_title`)
instead of a bare branch. It stays empty when the deferral preceded the spec.
"""

from pathlib import Path
from typing import ClassVar, TypedDict

from django.db import models
from django.utils import timezone

#: Failed drains an obligation may accumulate before the doctor calls it stuck. The
#: dispatch loop ticks every 5 minutes — a quarter-hour past the in-flight push.
MAX_DRAIN_ATTEMPTS = 3


class SerializedPrSpec(TypedDict):
    """A ``PullRequestSpec`` as it round-trips through the ``spec`` JSON column.

    Declared here rather than imported: ``teatree.core.models`` is a leaf under
    ``teatree.core.backend_protocols``, so the model layer cannot reach the
    dataclass. Field parity with it is pinned by the model's own test.
    """

    repo: str
    branch: str
    title: str
    description: str
    target_branch: str
    labels: list[str]
    assignee: str
    reviewers: list[str]
    draft: bool


class RelativeRepoPathError(ValueError):
    def __init__(self, repo_path: str) -> None:
        super().__init__(
            f"repo_path must be absolute (got {repo_path!r}) — a relative path names "
            f"a different checkout for every reader of the obligation",
        )


class PendingPullRequestManager(models.Manager["PendingPullRequest"]):
    def owe(
        self,
        *,
        repo_path: str,
        branch: str,
        reason: str,
        spec: SerializedPrSpec | None = None,
    ) -> "PendingPullRequest":
        """Record (or refresh) the PR owed for *branch*, keyed on ``(repo_path, branch)``.

        ``repo_path`` must be ABSOLUTE: the row is written by the pre-push hook in
        the worktree's cwd and read back by the dispatch loop and the doctor in
        theirs, so a relative path would name a different checkout on every read.

        A re-deferral updates what is owed but never resets ``drain_attempts``:
        the counter is what ages the obligation into a doctor FAIL, so a branch
        that re-defers every tick must not look permanently fresh.
        """
        if not Path(repo_path).is_absolute():
            raise RelativeRepoPathError(repo_path)
        row, created = self.get_or_create(
            repo_path=repo_path,
            branch=branch,
            defaults={"reason": reason, "spec": spec or {}},
        )
        if created:
            return row
        row.reason = reason
        if spec:
            row.spec = spec
        row.save(update_fields=["reason", "spec"])
        return row

    def discharge(self, *, repo_path: str, branch: str) -> None:
        self.filter(repo_path=repo_path, branch=branch).delete()

    def retire_absent(self) -> list[tuple[int, str, str]]:
        """Drop every obligation whose checkout is gone, returning ``(pk, branch, repo_path)`` each.

        Not a relaxation of the no-orphan invariant — a terminal state it was missing.
        A deleted checkout cannot be pushed from, so ``ensure-pr`` can never discharge
        the row: it retried ~12,000 times across 16 of these and turned
        ``t3 doctor check`` into 16 permanent FAILs that buried the five real findings
        (#4577). Nothing work-bearing goes with it — a branch that WAS pushed stays
        covered by the orphan-branch guard on the remote, and one that was not is
        already gone with the directory, row or no row.
        """
        gone = [row for row in self.all() if not Path(row.repo_path).exists()]
        self.filter(pk__in=[row.pk for row in gone]).delete()
        return [(row.pk, row.branch, row.repo_path) for row in gone]

    def overdue(self) -> models.QuerySet["PendingPullRequest"]:
        return self.filter(drain_attempts__gte=MAX_DRAIN_ATTEMPTS)


class PendingPullRequest(models.Model):
    repo_path = models.CharField(max_length=1024)
    branch = models.CharField(max_length=255)
    reason = models.CharField(max_length=255, blank=True, default="")
    spec = models.JSONField(default=dict, blank=True)
    deferred_at = models.DateTimeField(default=timezone.now)
    drain_attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    objects: ClassVar[PendingPullRequestManager] = PendingPullRequestManager()

    class Meta:
        db_table = "teatree_pending_pull_request"
        ordering: ClassVar = ["-deferred_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["repo_path", "branch"], name="uniq_pending_pr_repo_branch"),
        ]

    def __str__(self) -> str:
        return f"pending-pr<{self.pk}:{self.branch}@{self.repo_path}>"

    @property
    def is_overdue(self) -> bool:
        return self.drain_attempts >= MAX_DRAIN_ATTEMPTS

    @property
    def intended_title(self) -> str:
        title = self.spec.get("title") if isinstance(self.spec, dict) else None
        return str(title or "")

    def record_failed_drain(self, *, error: str = "") -> None:
        """Count one drain that could not discharge the obligation.

        An ``F`` increment rather than a read-modify-write: the drain and a
        concurrent pre-push re-deferral both touch this row, and a lost update
        here would hold the obligation permanently below the doctor threshold.
        """
        type(self).objects.filter(pk=self.pk).update(
            drain_attempts=models.F("drain_attempts") + 1,
            last_attempt_at=timezone.now(),
            last_error=error,
        )
        self.refresh_from_db(fields=["drain_attempts", "last_attempt_at", "last_error"])
