"""Structural non-bypassability: the egress class is the ONLY colleague-Slack egress.

A fitness function (AST walk of ``src/teatree``, call expressions only) that
fails if a NEW colleague-surface Slack egress is added outside
``on_behalf_egress.py``. Two invariants:

1.  The #1750 routed primitives (``react_routed`` / ``post_routed``) are
    called nowhere except inside the egress class — every routed
    colleague/self post funnels through it.
2.  The Connect-routed colleague egress (``react`` / ``post_message``) is
    called only at the documented bot→user / self-ack sinks (which are not
    on-behalf colleague egress and are correct ungated by design).

A regression here is a new bypass — the exact away-mode incident shape.
"""

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "teatree"

_ROUTED_METHODS = frozenset({"react_routed", "post_routed"})
_COLLEAGUE_METHODS = frozenset({"react", "post_message"})

# The egress class is the sole sanctioned caller of the routed primitives.
_ROUTED_ALLOWED = {"core/on_behalf_egress.py"}

# Bot→user / self-ack / FSM-transition / setup sinks. Each is either a DM TO
# the user (never on-behalf), the FSM reaction path on its own gated
# slack_reactions transport, or provisioning-only — NOT a colleague egress.
_COLLEAGUE_ALLOWED = {
    "backends/slack_bot.py",  # the primitive implementations themselves
    "core/notify.py",  # bot→user DM
    "core/reply_transport.py",  # bot→user post_dm + gated _send subclass deliver
    "core/daily_digest.py",  # bot→user digest thread
    "core/speak.py",  # bot→user audio upload
    "messaging/notify_with_fallback.py",  # bot→user fallback DM
    "loop/slack_answer/cycle.py",  # self-ack on the user's own DM
    "loop/scanners/red_card.py",  # self-ack on the user's own signal message
    "loop/scanners/review_nag.py",  # bot→user stale-MR DM (_dm_user_and_close)
    "loop/scanners/incoming_events.py",  # ALERT_USER → bot→user DM
    "loop/self_improve/actions.py",  # internal monitoring/alert channel post
    "cli/slack_setup.py",  # provisioning smoke-test DM
    "core/management/commands/review_request_post.py",  # already gate+audit-correct (#960)
}


def _relpath(path: Path) -> str:
    return str(path.relative_to(_SRC)).replace("\\", "/")


def _receiver_is_egress(node: ast.Attribute) -> bool:
    """True when ``node`` is a method call on the egress (``egress.x`` / ``OnBehalfSlackEgress(...).x``).

    A call chained off an :class:`OnBehalfSlackEgress` construction, or off a
    local named ``egress``, is the sanctioned chokepoint — not a raw backend
    colleague egress.
    """
    receiver = node.value
    if isinstance(receiver, ast.Name) and receiver.id == "egress":
        return True
    return (
        isinstance(receiver, ast.Call)
        and isinstance(receiver.func, ast.Name)
        and receiver.func.id == "OnBehalfSlackEgress"
    )


def _called_methods(tree: ast.Module, methods: frozenset[str]) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in methods:
            continue
        if _receiver_is_egress(node.func):
            continue
        found.add(node.func.attr)
    return found


def _modules_calling(methods: frozenset[str]) -> dict[str, set[str]]:
    hits: dict[str, set[str]] = {}
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls = _called_methods(tree, methods)
        if calls:
            hits[_relpath(path)] = calls
    return hits


def test_routed_primitives_called_only_inside_the_egress_class() -> None:
    offenders = {mod: calls for mod, calls in _modules_calling(_ROUTED_METHODS).items() if mod not in _ROUTED_ALLOWED}
    # ``slack_bot.py`` defines react_routed/post_routed; its self-call of
    # ``_post`` is not a routed call, so it never appears as an offender.
    assert offenders == {}, (
        f"react_routed/post_routed called outside OnBehalfSlackEgress: {offenders}. "
        "Every colleague-surface Slack post/react must route through the egress class."
    )


def test_colleague_primitives_called_only_at_documented_sinks() -> None:
    offenders = {
        mod: calls for mod, calls in _modules_calling(_COLLEAGUE_METHODS).items() if mod not in _COLLEAGUE_ALLOWED
    }
    assert offenders == {}, (
        f"react/post_message called outside the documented bot→user / self-ack sinks: {offenders}. "
        "A new colleague-surface egress must go through OnBehalfSlackEgress, not a raw backend.react/post_message."
    )
