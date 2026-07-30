"""``ProjectLearning`` — durable per-repo knowledge store, DB-placed (#2892).

Distinct from ``Ticket.context`` (#627: per-TICKET) and from
``ConsolidatedMemory`` (teatree's own skill-improvement rules, global to the
tool): this model holds operational knowledge scoped to a *target repo* the
overlay works on (``owner/repo``), so it survives across every ticket on
that repo — the DB-placed analogue of gstack's per-project
``learnings.jsonl`` (#550 item 3, epic #2557).

Keyed on the repo-slug extraction #2293 introduced
(:func:`teatree.utils.url_slug.project_slug_from_ref`), so this store shares
its identity mechanism with the per-ticket context cache rather than
inventing a second one.
"""

from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone


class ProjectLearningManager(models.Manager["ProjectLearning"]):
    def content_for_slug(self, repo_slug: str) -> str:
        """The durable learnings text for *repo_slug*, or "" when none recorded."""
        row = self.filter(repo_slug=repo_slug).first()
        return row.content if row is not None else ""

    def record_for_slug(self, repo_slug: str, entry: str) -> "ProjectLearning":
        """Get-or-create the row for *repo_slug* and append *entry* to it."""
        row, _ = self.get_or_create(repo_slug=repo_slug)
        row.append_learning(entry)
        return row


class ProjectLearning(models.Model):
    """One durable knowledge store per repo, keyed on its collision-free slug."""

    repo_slug = models.CharField(max_length=300, unique=True, db_index=True)
    content = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[ProjectLearningManager] = ProjectLearningManager()

    class Meta:
        db_table = "teatree_project_learning"
        ordering: ClassVar = ["repo_slug"]

    def __str__(self) -> str:
        return f"project-learning<{self.repo_slug}>"

    def append_learning(self, entry: str) -> str:
        r"""Append a timestamped block to the durable per-repo knowledge store.

        Mirrors ``Ticket.append_context`` (#627): append-only so parallel
        sessions never clobber each other's notes rather than overwriting,
        and refuses a blank entry — an empty note carries no durable
        knowledge and would just add noise. Returns the full updated content.
        """
        text = entry.strip()
        if not text:
            msg = "learning entry is empty"
            raise ValueError(msg)
        stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M")
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            updated = f"{locked.content}\n\n[{stamp}] {text}"
            self.content = updated
            type(self).objects.filter(pk=self.pk).update(content=updated)
        return updated
