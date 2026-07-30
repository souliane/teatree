"""Command-catalogue seam for the `command_search` discoverability tool.

`command_search` answers "which `t3` command do I run for X" — the recurring gap
where an agent guesses a subcommand that does not exist. Its data is the live
Typer command tree, which only :mod:`teatree.cli` can introspect. ``teatree.cli``
sits ABOVE ``teatree.mcp`` in the layer graph, so — exactly like the #550
skill-command-validity lane — the dependency is INVERTED: ``teatree.cli``
registers a provider at import time via
:func:`register_command_catalogue_provider`, and this low module (which the mcp
query layer can import) holds only the registration seam plus the pure ranking.

The default provider raises loud, so a caller that never registered one fails
with a clear message rather than silently searching an empty catalogue.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# The leaf-name segment is weighted above the rest of the path, which is
# weighted above the help summary, so `worktree provision` ranks the leaf whose
# name IS those tokens ahead of one that merely mentions them in prose.
_LEAF_WEIGHT = 3
_PATH_WEIGHT = 2
_SUMMARY_WEIGHT = 1


@dataclass(frozen=True)
class CommandRecord:
    """One `t3` leaf command projected for discoverability search.

    ``path`` is the full invocation (``t3 <overlay> worktree provision``),
    ``summary`` its one-line help, and ``emits_json`` whether it exposes a
    ``--json`` / ``--format`` machine-readable output the agent can parse.
    """

    path: str
    summary: str
    emits_json: bool

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "summary": self.summary, "emits_json": self.emits_json}


CatalogueProvider = Callable[[], list[CommandRecord]]


def _unregistered_provider() -> list[CommandRecord]:
    msg = (
        "command-catalogue provider not registered — teatree.cli must call "
        "register_command_catalogue_provider() at import time"
    )
    raise RuntimeError(msg)


_provider: CatalogueProvider = _unregistered_provider


def register_command_catalogue_provider(provider: CatalogueProvider) -> None:
    """Inject the catalogue builder (called by ``teatree.cli`` at import time)."""
    global _provider  # noqa: PLW0603 — the single registration seam for the inverted dependency
    _provider = provider


def build_command_catalogue() -> list[CommandRecord]:
    """The live command catalogue via the registered provider."""
    return _provider()


def _score(record: CommandRecord, tokens: list[str]) -> int:
    """Relevance of *record* to the query *tokens* (0 = no match).

    A token counts once per surface it hits: the leaf name (last path segment),
    the whole path, and the summary — highest-weighted surface first.
    """
    leaf = record.path.rsplit(" ", 1)[-1].lower()
    path = record.path.lower()
    summary = record.summary.lower()
    total = 0
    for token in tokens:
        if token in leaf:
            total += _LEAF_WEIGHT
        if token in path:
            total += _PATH_WEIGHT
        if token in summary:
            total += _SUMMARY_WEIGHT
    return total


def search_commands(query: str, *, catalogue: list[CommandRecord], limit: int) -> list[dict[str, Any]]:
    """Rank *catalogue* by relevance to *query*; return the best *limit* matches.

    Pure and CLI-free — the caller passes the catalogue (the registered provider
    supplies the live one). A blank query matches nothing. Ties break on the
    command path so the ordering is deterministic.
    """
    tokens = query.lower().split()
    if not tokens:
        return []
    scored = [(record, _score(record, tokens)) for record in catalogue]
    matches = sorted(
        ((record, score) for record, score in scored if score > 0),
        key=lambda pair: (-pair[1], pair[0].path),
    )
    return [record.to_dict() for record, _ in matches[:limit]]
