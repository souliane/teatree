"""Shared CLI table helper — one ``rich.table.Table`` renderer for every command.

``print_table`` is the single seam the record-list management commands render
through, so ``t3 <overlay> tasks list`` / ``loop status`` / ``queue`` / … all
produce the same aligned table. It writes through a ``Console(file=stream)`` so
a command can direct the table at ``self.stderr`` (the PR-30 machine-output
seam keeps stdout a pure JSON channel) while a bare ``print_table`` call still
lands on the terminal.

Dumb-terminal / CI safe: a redirected or captured stream has no terminal width,
so rich would otherwise fall back to 80 columns and crush wide columns. A
generous fixed width for the piped case keeps every column rendered untruncated;
a real terminal keeps its own width and rich degrades colour on a non-TTY.
"""

from collections.abc import Sequence
from typing import IO, Literal

from rich.console import Console
from rich.table import Table
from rich.text import Text

Justify = Literal["left", "center", "right"]

# A redirected/captured stream reports no width; rich then defaults to 80 and
# crushes wide columns (#2092). Give piped output a generous fixed width.
_PIPE_WIDTH = 160


def print_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    title: str = "",
    stream: IO[str] | None = None,
    justify: Sequence[Justify] | None = None,
) -> None:
    """Render *rows* under *headers* as an aligned table to *stream*.

    ``justify`` sets per-column alignment (``left`` when unspecified); a shorter
    list left-justifies the trailing columns. An empty *rows* renders the title
    (if any) plus a dim ``(no rows)`` line rather than an empty frame.
    """
    console = Console(file=stream, width=_PIPE_WIDTH) if stream is not None else Console()
    if title:
        # A bold line above the frame — never ``Table(title=...)``, whose centred
        # title wraps to the (often narrow) table width and mangles the text.
        console.print(f"[bold]{title}[/]")
    if not rows:
        console.print("[dim](no rows)[/dim]")
        return

    table = Table(show_lines=False)
    for index, header in enumerate(headers):
        column_justify = justify[index] if justify is not None and index < len(justify) else "left"
        table.add_column(str(header), justify=column_justify)
    for row in rows:
        # ``Text`` renders each cell literally — rich's default treats a cell
        # string as console markup, so a value carrying ``[…]`` (a markdown ref
        # like ``[repo#7](url)``) would be silently eaten as a style tag.
        table.add_row(*(Text(str(cell)) for cell in row))
    console.print(table)


__all__ = ["Justify", "print_table"]
