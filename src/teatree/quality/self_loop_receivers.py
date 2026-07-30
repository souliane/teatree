"""AST guard: a ``post_transition`` receiver must decide what a self-loop means.

Several FSM transitions list their own target among their sources so a re-run is
safe — ``Ticket.mark_reviewed_externally`` re-stamps a moved head SHA and stays at
``REVIEW_POSTED``. Such a transition is idempotent in STATE but not in SIDE
EFFECTS: the replay still fires ``post_transition``, so a receiver that mints work
for ENTERING a state mints it again for a state entered long ago. Every occurrence
so far was one receiver keyed on the ``target`` alone, and each cost a row/job/post
per ticket per pass, forever.

The rule: a receiver connected to ``post_transition`` for a sender whose FSM admits
a self-loop must either contain an explicit ``source``/``target`` comparison, or
carry a ``# self-loop-safe: <reason>`` marker naming why re-firing is what the
caller asked for (the name-keyed workers whose transition IS the request:
re-provision, restart, re-verify, re-teardown). A sender whose FSM has no self-loop
cannot reach the class at all, and that exemption is DERIVED from the live FSM
(``self_looping_senders``) rather than asserted in prose — adding a self-loop to
such a model turns the walk RED on its receivers.

Fail-closed: an unknown sender, an absent ``sender=``, and a receiver whose
definition the walk cannot resolve are all violations, so the guard can never go
quiet by losing sight of a receiver.
"""

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

SELF_LOOP_SAFE_PRAGMA = "self-loop-safe:"

#: The marker must NAME a reason; this many whitespace-separated words is the floor
#: that stops it degrading into a bare silencer.
_MIN_REASON_WORDS = 3

_SIGNAL_NAME = "post_transition"
_SOURCE_PARAM = "source"
_TARGET_PARAM = "target"
_SELF_LOOP_OPS: tuple[type[ast.cmpop], ...] = (ast.Eq, ast.NotEq)


@dataclass(frozen=True)
class ReceiverBinding:
    """One function connected to ``post_transition``, with the sender it listens for."""

    path: Path
    name: str
    lineno: int
    sender: str


@dataclass(frozen=True)
class ReceiverViolation:
    """One ``post_transition`` receiver that has not decided what a self-loop means."""

    path: Path
    lineno: int
    receiver: str
    sender: str
    reason: str


@dataclass(frozen=True)
class WalkResult:
    """Every discovered receiver plus the subset that fails the rule."""

    receivers: tuple[ReceiverBinding, ...]
    violations: tuple[ReceiverViolation, ...]


def _dotted_tail(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _connect_binding(call: ast.Call) -> tuple[ast.expr, str] | None:
    """The ``(receiver_expr, sender_name)`` of a ``post_transition.connect(...)`` call."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "connect"):
        return None
    if _dotted_tail(func.value) != _SIGNAL_NAME:
        return None
    receiver = call.args[0] if call.args else _keyword(call, "receiver")
    if receiver is None:
        return None
    sender = _keyword(call, "sender")
    return receiver, (_dotted_tail(sender) if sender is not None else "")


def _decorator_sender(decorator: ast.expr) -> str | None:
    """The sender of a ``@receiver(post_transition, sender=X)`` decorator, if it is one."""
    if not isinstance(decorator, ast.Call) or _dotted_tail(decorator.func) != "receiver":
        return None
    if not any(_dotted_tail(arg) == _SIGNAL_NAME for arg in decorator.args):
        return None
    sender = _keyword(decorator, "sender")
    return _dotted_tail(sender) if sender is not None else ""


def _functions_by_name(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}


def _compares_source_to_target(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(func):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for index, op in enumerate(node.ops):
            if not isinstance(op, _SELF_LOOP_OPS):
                continue
            pair = {_dotted_tail(operands[index]), _dotted_tail(operands[index + 1])}
            if pair == {_SOURCE_PARAM, _TARGET_PARAM}:
                return True
    return False


def _opt_out_reason(func: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str]) -> str:
    """The reason text of the receiver's ``# self-loop-safe:`` marker, ``""`` when absent."""
    start = min([func.lineno, *(d.lineno for d in func.decorator_list)])
    for line in lines[start - 1 : func.end_lineno]:
        _, marker, reason = line.partition(SELF_LOOP_SAFE_PRAGMA)
        if marker and len(reason.split()) >= _MIN_REASON_WORDS:
            return reason.strip()
    return ""


def _classify(
    binding: ReceiverBinding,
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    lines: list[str],
) -> ReceiverViolation | None:
    if _compares_source_to_target(func) or _opt_out_reason(func, lines):
        return None
    return ReceiverViolation(
        path=binding.path,
        lineno=binding.lineno,
        receiver=binding.name,
        sender=binding.sender,
        reason=(
            f"keys on the transition without comparing {_SOURCE_PARAM} to {_TARGET_PARAM}, so a "
            f"state-preserving self-loop on {binding.sender or '<any sender>'} re-fires it — add the "
            f"guard, or a '# {SELF_LOOP_SAFE_PRAGMA} <reason>' marker naming why re-firing is intended"
        ),
    )


def _iter_bindings(tree: ast.Module, path: Path) -> Iterator[ReceiverBinding]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            connected = _connect_binding(node)
            if connected is not None:
                receiver, sender = connected
                yield ReceiverBinding(path=path, name=_dotted_tail(receiver), lineno=node.lineno, sender=sender)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for decorator in node.decorator_list:
                sender = _decorator_sender(decorator)
                if sender is not None:
                    yield ReceiverBinding(path=path, name=node.name, lineno=node.lineno, sender=sender)


def _binding_violation(
    binding: ReceiverBinding,
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    lines: list[str],
    self_looping_senders: frozenset[str] | None,
) -> ReceiverViolation | None:
    if self_looping_senders is not None and binding.sender and binding.sender not in self_looping_senders:
        return None
    func = functions.get(binding.name)
    if func is None:
        return ReceiverViolation(
            path=binding.path,
            lineno=binding.lineno,
            receiver=binding.name,
            sender=binding.sender,
            reason="receiver is not defined in this module, so the walk cannot classify it",
        )
    return _classify(binding, func, lines)


def scan_source(
    source: str,
    path: Path,
    self_looping_senders: frozenset[str] | None = None,
) -> WalkResult:
    """Walk *source* for ``post_transition`` receivers and the ones that ignore self-loops.

    *self_looping_senders* names the models whose FSM admits a state-preserving
    transition; ``None`` treats every sender as one (the fail-closed default for a
    caller with no FSM knowledge).
    """
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    functions = _functions_by_name(tree)
    bindings = tuple(_iter_bindings(tree, path))
    violations = tuple(
        violation
        for binding in bindings
        if (violation := _binding_violation(binding, functions, lines, self_looping_senders)) is not None
    )
    return WalkResult(receivers=bindings, violations=violations)


def scan_file(path: Path, self_looping_senders: frozenset[str] | None = None) -> WalkResult:
    return scan_source(path.read_text(encoding="utf-8"), path, self_looping_senders)


def scan_tree(roots: Iterable[Path], self_looping_senders: frozenset[str] | None = None) -> WalkResult:
    """Walk every ``.py`` under *roots* for ``post_transition`` receivers."""
    receivers: list[ReceiverBinding] = []
    violations: list[ReceiverViolation] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            result = scan_file(path, self_looping_senders)
            receivers.extend(result.receivers)
            violations.extend(result.violations)
    return WalkResult(receivers=tuple(receivers), violations=tuple(violations))
