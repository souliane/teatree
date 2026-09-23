"""Every deliberate refusal in ``teatree.mcp`` reaches the agent as a ``ToolError``.

mcp's tool wrapper (``tools/base.py``) re-raises a ``ToolError`` WITH its text and
everything else as ``UnexpectedToolError("Error executing tool <name>")``, reason
stripped. So a refusal raised as a plain ``RuntimeError`` still fires and still stops
the call — it just stops telling the agent why, which is the silent-empty every one of
these refusals was written to replace. The failure is invisible: the gate works, the
test that asserts the raise passes, and only the caller is left guessing.

Two shapes carry a refusal out of this package, and the walk covers both. A ``raise``
written here is pinned by requiring the class to derive from ``ToolError``. A refusal
raised by a lower layer and propagated through a tool body is pinned on the
``*_or_refuse`` seams, whose whole naming convention IS "this refuses by raising rather
than returning a neutral"; those must be re-raised as ``ToolError`` at the boundary,
because their classes live in dependency-free modules that cannot import mcp.

Its reach is lexical. A refusal reached through an indirection this walk cannot name —
a helper in another package that raises something new, a dynamically-built raise — is
outside it, the same honesty the merged-detection walk states about itself.
"""

import ast
from dataclasses import dataclass
from pathlib import Path

from tests.conformance._src_tree import SRC_DIR, parsed_modules

_MCP_DIR = SRC_DIR / "mcp"

#: The root of the accepted hierarchy. Anything deriving from it keeps its text.
_TOOL_ERROR = "ToolError"

#: Seam suffix meaning "refuses by raising rather than returning a neutral value".
_REFUSING_SEAM_SUFFIX = "_or_refuse"

#: Below these the walk cannot have been reading the real package.
_MIN_RAISES = 8
_MIN_REFUSING_SEAM_CALLS = 1

#: The ``raise`` sites that are NOT agent-facing refusals, and why. A new one must be
#: classified deliberately — that forcing function is the point of an explicit ledger.
_NOT_A_REFUSAL: dict[tuple[str, str], str] = {
    ("__init__.py", "AttributeError"): "the module __getattr__ protocol, not a tool call",
    ("server.py", "ToolNameCollisionError"): "raised at registration time, before any tool can be called",
    ("command_catalogue.py", "RuntimeError"): "unregistered provider — teatree.cli never imported, a wiring bug",
    ("review_seam.py", "RuntimeError"): "unregistered factory — teatree.cli never imported, a wiring bug",
}


@dataclass(frozen=True, slots=True)
class Violation:
    module: str
    line: int
    detail: str


def _class_name(node: ast.expr | None) -> str | None:
    """The bare class name *node* denotes, through a call and a dotted path."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def tool_error_subclasses(modules: tuple[tuple[Path, ast.Module], ...]) -> frozenset[str]:
    """Every class name in *modules* that derives from ``ToolError``, transitively."""
    bases: dict[str, set[str]] = {}
    for _path, tree in modules:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases[node.name] = {name for base in node.bases if (name := _class_name(base)) is not None}
    derived = {_TOOL_ERROR}
    while True:
        grown = derived | {name for name, parents in bases.items() if parents & derived}
        if grown == derived:
            return frozenset(derived)
        derived = grown


def _guarded_nodes(tree: ast.Module, accepted: frozenset[str]) -> set[ast.AST]:
    """Every node sitting in a ``try`` body whose handlers re-raise an accepted class."""
    guarded: set[ast.AST] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        converts = any(
            isinstance(inner, ast.Raise) and _class_name(inner.exc) in accepted
            for handler in node.handlers
            for inner in ast.walk(handler)
        )
        if converts:
            guarded.update(inner for stmt in node.body for inner in ast.walk(stmt))
    return guarded


def _called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def scan_module(label: str, tree: ast.Module, accepted: frozenset[str]) -> list[Violation]:
    """Refusals in *tree* that would reach the agent with their reason stripped."""
    guarded = _guarded_nodes(tree, accepted)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise):
            name = _class_name(node.exc)
            if name is not None and name not in accepted and (label, name) not in _NOT_A_REFUSAL:
                violations.append(Violation(label, node.lineno, f"raises {name}, not a ToolError"))
        elif isinstance(node, ast.Call) and _called_name(node).endswith(_REFUSING_SEAM_SUFFIX):
            if node not in guarded:
                detail = f"{_called_name(node)}() refuses by raising, un-converted to ToolError"
                violations.append(Violation(label, node.lineno, detail))
    return violations


def _scan_source(source: str, *, label: str = "probe.py") -> list[Violation]:
    tree = ast.parse(source)
    return scan_module(label, tree, tool_error_subclasses(((Path(label), tree),)))


def _live_violations() -> list[Violation]:
    modules = parsed_modules(_MCP_DIR)
    accepted = tool_error_subclasses(modules)
    return [v for path, tree in modules for v in scan_module(path.name, tree, accepted)]


def test_no_mcp_refusal_reaches_the_agent_with_its_reason_stripped() -> None:
    assert _live_violations() == []


def test_the_walk_actually_read_the_package() -> None:
    modules = parsed_modules(_MCP_DIR)
    raises = sum(isinstance(n, ast.Raise) for _p, t in modules for n in ast.walk(t))
    seams = sum(
        isinstance(n, ast.Call) and _called_name(n).endswith(_REFUSING_SEAM_SUFFIX)
        for _p, t in modules
        for n in ast.walk(t)
    )
    assert raises >= _MIN_RAISES, raises
    assert seams >= _MIN_REFUSING_SEAM_CALLS, seams


def test_every_ledgered_exemption_still_exists() -> None:
    # A ledger entry outliving its raise site turns into permanent permission for a
    # shape nobody reviewed, so it is pinned to the code rather than left to rot.
    live = {
        (path.name, name)
        for path, tree in parsed_modules(_MCP_DIR)
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise) and (name := _class_name(node.exc)) is not None
    }
    assert set(_NOT_A_REFUSAL) <= live, set(_NOT_A_REFUSAL) - live


def test_the_walk_flags_a_refusal_raised_as_a_plain_exception() -> None:
    # The control for the MentionQueueUnreadableError shape, verbatim pre-fix.
    pre_fix = """
class MentionQueueUnreadableError(RuntimeError):
    pass


async def _slack_mentions():
    raise MentionQueueUnreadableError
"""
    assert [v.detail for v in _scan_source(pre_fix)] == ["raises MentionQueueUnreadableError, not a ToolError"]


def test_the_walk_passes_the_same_refusal_once_it_derives_from_tool_error() -> None:
    post_fix = """
from mcp.server.mcpserver.exceptions import ToolError


class MentionQueueUnreadableError(ToolError):
    pass


async def _slack_mentions():
    raise MentionQueueUnreadableError
"""
    assert _scan_source(post_fix) == []


def test_the_walk_flags_an_unconverted_refusing_seam() -> None:
    # The control for the ChannelReadRefusedError shape, verbatim pre-fix.
    pre_fix = """
async def _slack_channel_history(channel):
    return _client().fetch_channel_history_or_refuse(channel=channel)
"""
    assert [v.detail for v in _scan_source(pre_fix)] == [
        "fetch_channel_history_or_refuse() refuses by raising, un-converted to ToolError"
    ]


def test_the_walk_passes_a_refusing_seam_converted_at_the_boundary() -> None:
    post_fix = """
from mcp.server.mcpserver.exceptions import ToolError

from teatree.types import ChannelReadRefusedError


async def _slack_channel_history(channel):
    try:
        return _client().fetch_channel_history_or_refuse(channel=channel)
    except ChannelReadRefusedError as refused:
        raise ToolError(str(refused)) from refused
"""
    assert _scan_source(post_fix) == []
