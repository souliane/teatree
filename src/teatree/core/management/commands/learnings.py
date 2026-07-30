"""``t3 <overlay> learnings show|add|edit`` — durable per-repo knowledge store (#2892).

The project-scoped analogue of ``ticket context`` (#627): knowledge that
outlives a single ticket because it is true of the *repo*, not the issue —
house conventions, CI quirks, locale rules a fresh session would otherwise
re-discover on every ticket. DB-placed per #2892 (not gstack's
``learnings.jsonl``), keyed on the same repo-slug extraction #2293
introduced for the per-ticket context store.

Non-zero exits use ``raise SystemExit(N)`` — this runs under Django's
``call_command``; ``typer.Exit`` is the wrong primitive on that path.
"""

from typing import TypedDict

import click
from django_typer.management import TyperCommand, command, initialize

from teatree.core.models.project_learning import ProjectLearning
from teatree.utils.url_slug import project_slug_from_ref


class LearningsResult(TypedDict):
    repo_slug: str
    content: str


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 <overlay> learnings`` group root."""

    def _resolve_slug(self, repo_ref: str) -> str:
        slug = project_slug_from_ref(repo_ref)
        if not slug:
            self.stderr.write(f"  could not resolve a repo from {repo_ref!r}")
            raise SystemExit(1)
        return slug

    @command()
    def show(self, repo_ref: str) -> LearningsResult:
        """Print the repo's durable learnings store.

        ``repo_ref`` accepts a literal ``owner/repo`` slug or a full
        issue/PR/MR URL.
        """
        slug = self._resolve_slug(repo_ref)
        content = ProjectLearning.objects.content_for_slug(slug)
        self.stdout.write(content or "(empty)")
        return {"repo_slug": slug, "content": content}

    @command()
    def add(self, repo_ref: str, entry: str) -> LearningsResult:
        """Append a timestamped entry to the repo's durable learnings store.

        Append-only: parallel sessions never overwrite each other's notes.
        A blank entry is refused with a nonzero exit.
        """
        slug = self._resolve_slug(repo_ref)
        try:
            row = ProjectLearning.objects.record_for_slug(slug, entry)
        except ValueError as exc:
            self.stderr.write(f"  refused: {exc}")
            raise SystemExit(1) from exc
        self.stdout.write(f"  appended to learnings for {slug}")
        return {"repo_slug": slug, "content": row.content}

    @command()
    def edit(self, repo_ref: str) -> LearningsResult:
        """Open the repo's full learnings store in ``$EDITOR`` and replace it.

        Unlike ``add``, ``edit`` is a full-field rewrite. An aborted edit
        (editor exits without saving) leaves the store untouched.
        """
        slug = self._resolve_slug(repo_ref)
        row, _ = ProjectLearning.objects.get_or_create(repo_slug=slug)
        edited = click.edit(row.content)
        if edited is None:
            self.stdout.write(f"  edit aborted — learnings for {slug} unchanged")
            return {"repo_slug": slug, "content": row.content}
        row.content = edited
        row.save(update_fields=["content"])
        self.stdout.write(f"  learnings for {slug} replaced")
        return {"repo_slug": slug, "content": edited}
