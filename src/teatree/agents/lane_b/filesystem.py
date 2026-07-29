"""File System capability — read/write/edit/search jailed to the worktree root.

The four tools are teatree-owned ``FunctionToolset`` functions (adopting
pydantic_ai's native toolset primitive rather than hand-rolling a toolset
framework). Every path argument is resolved through :func:`resolve_within` before
any I/O, so a ``../`` traversal or an absolute path outside the jail root is
refused with a :class:`PathTraversalError` the model sees as a tool error — the
capability can only ever touch files under the dispatch's own worktree.
"""

from pathlib import Path

from pydantic_ai.toolsets.function import FunctionToolset

from teatree.agents.lane_b.tool_names import TOOL_EDIT, TOOL_GREP, TOOL_READ, TOOL_WRITE

_MAX_READ_BYTES = 1_000_000
_MAX_SEARCH_HITS = 200


class PathTraversalError(ValueError):
    """A tool path resolved outside its jail root — refused before any I/O."""


def resolve_within(root: Path, candidate: str) -> Path:
    """Resolve *candidate* under *root*, refusing any escape from the jail.

    A relative path is joined onto *root*; an absolute path is accepted only when
    it is already inside *root*. The fully-resolved (symlink- and ``..``-collapsed)
    path must remain within the fully-resolved *root*, so neither ``../`` nor a
    symlink pointing outside can escape. Raises :class:`PathTraversalError`
    otherwise — the single normalization boundary every File System tool passes.
    """
    root_resolved = root.resolve()
    raw = Path(candidate)
    joined = raw if raw.is_absolute() else root_resolved / raw
    resolved = joined.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        msg = f"path {candidate!r} resolves outside the worktree root {str(root_resolved)!r}"
        raise PathTraversalError(msg)
    return resolved


def build_filesystem_toolset(root: Path, *, allow_write: bool = True) -> FunctionToolset[None]:
    """Assemble the File System ``FunctionToolset`` jailed to *root*.

    *allow_write* is ``False`` for a read-only phase so the write/edit tools are
    never even registered (belt-and-braces with the phase-scoped filter): a
    read-only dispatch's toolset carries no mutation surface at all.
    """
    toolset: FunctionToolset[None] = FunctionToolset()

    def read_file(path: str) -> str:
        """Read a UTF-8 text file under the worktree, returning its content."""
        data = resolve_within(root, path).read_bytes()[:_MAX_READ_BYTES]
        return data.decode("utf-8", errors="replace")

    def search_files(pattern: str, glob: str = "**/*") -> list[str]:
        """Return worktree file paths whose text contains *pattern* (substring)."""
        return _search(root, pattern, glob)

    # Exposed under the skill/SDK vocabulary (Read/Grep/Write/Edit) so a skill
    # instruction naming ``Read`` maps to the actual tool; the pythonic function
    # names stay descriptive. See :mod:`teatree.agents.lane_b.tool_names`.
    toolset.add_function(read_file, takes_ctx=False, name=TOOL_READ)
    toolset.add_function(search_files, takes_ctx=False, name=TOOL_GREP)
    if allow_write:
        _add_write_tools(toolset, root)
    return toolset


def _search(root: Path, pattern: str, glob: str) -> list[str]:
    """Substring-search worktree files, capped at :data:`_MAX_SEARCH_HITS`."""
    hits: list[str] = []
    for candidate in sorted(root.rglob(glob.removeprefix("**/") or "*")):
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if pattern in text:
            hits.append(str(candidate.relative_to(root)))
        if len(hits) >= _MAX_SEARCH_HITS:
            break
    return hits


def _add_write_tools(toolset: FunctionToolset[None], root: Path) -> None:
    """Register the mutating File System tools (write/edit) jailed to *root*."""

    def write_file(path: str, content: str) -> str:
        """Create or overwrite a UTF-8 text file under the worktree."""
        target = resolve_within(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {path}"

    def edit_file(path: str, old: str, new: str) -> str:
        """Replace the first occurrence of *old* with *new* in a worktree file."""
        target = resolve_within(root, path)
        text = target.read_text(encoding="utf-8")
        if old not in text:
            msg = f"substring not found in {path!r}; no edit made"
            raise ValueError(msg)
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"edited {path}"

    toolset.add_function(write_file, takes_ctx=False, name=TOOL_WRITE)
    toolset.add_function(edit_file, takes_ctx=False, name=TOOL_EDIT)
