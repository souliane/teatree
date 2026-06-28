"""``t3 <overlay> memory recall`` — surface cold-tier memory rules relevant to a query (#2746).

The agent-facing read seam for the cold-tier recall mechanism: given a query, it scores
the project's cold ``MEMORY_ARCHIVE.md`` index (the rules PR1/#2723 archived out of the
session-loaded hot ``MEMORY.md``) and prints the top relevant entries. The same pure core
the ``UserPromptSubmit`` hook uses (``teatree.loops.dream.recall``), exposed as a CLI for
manual lookup and inspection.

Non-zero exits use ``raise SystemExit(N)`` — this runs under Django's ``call_command``;
``typer.Exit`` is the wrong primitive on that path (see /t3:teatree "Management Command
Patterns"). Every ``typer.Option``-annotated param carries a default.
"""

from pathlib import Path
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.loops.dream import recall


def _default_memory_dir() -> Path:
    """The current project's memory dir under ``~/.claude/projects/<cwd-slug>/memory``.

    The harness names a project dir from its cwd with ``/`` → ``-`` (the same slug
    the hook derives); the memory dir is its ``memory/`` child.
    """
    slug = str(Path.cwd()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug / "memory"


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 <overlay> memory`` group root."""

    @command()
    def recall(
        self,
        query: Annotated[str, typer.Argument(help="The text whose relevant cold-tier rules to surface.")],
        memory_dir: Annotated[
            str,
            typer.Option("--memory-dir", help="Memory dir to search; defaults to the current project's."),
        ] = "",
        limit: Annotated[
            int,
            typer.Option("--limit", help="Max number of cold-tier rules to surface."),
        ] = recall.RECALL_LIMIT,
    ) -> None:
        """Print the cold-tier memory rules most relevant to *query* (top *limit*).

        Resolves the memory dir from ``--memory-dir`` else the current project's
        default, scores the cold index, and echoes one line per hit — or a single
        "no relevant cold-tier entries" line (exit 0) when nothing clears the
        relevance floor. A missing memory dir / cold index is reported as an error
        (exit 1) so a mistyped ``--memory-dir`` is loud, not a silent empty result.
        """
        resolved = Path(memory_dir).expanduser() if memory_dir else _default_memory_dir()
        if not (resolved / recall.COLD_INDEX_NAME).is_file():
            self.stderr.write(f"  no cold-tier index ({recall.COLD_INDEX_NAME}) under {resolved}")
            raise SystemExit(1)
        hits = recall.recall_cold_memory(resolved, query, limit=limit)
        if not hits:
            self.stdout.write("  no relevant cold-tier entries")
            return
        for hit in hits:
            self.stdout.write(f"  - {hit.name} — {hit.signature}" if hit.signature else f"  - {hit.name}")
