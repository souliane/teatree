"""No attempt recorder may silently drop the spend its run already billed (#4164).

Only the SUCCESS path ever wrote usage, so every post-turn failure — a lost lease, an
evidence-gate refusal, a turn-ceiling truncation — discarded tokens that were already
billed: zero of 8,217 failed ``TaskAttempt`` rows in the table's history carry a token
count, and a measured 916 of them provably spent. A third recorder must not be able to
appear the same way, so every site that writes a ``TaskAttempt`` either carries the spend
or names, in code, why there is none.
"""

import ast
from pathlib import Path

from tests.conformance._src_tree import SRC_DIR, src_modules

#: The pragma a site uses to declare that no turn was billed, followed by the reason.
NO_USAGE_PRAGMA = "# no-usage:"

#: How a site carries spend: the shared mapping splatted in, or ``usage=`` handed on.
_CARRIES_SPEND = ("usage_fields(", "usage=")

#: Receivers whose ``.objects.create`` writes a ``TaskAttempt``.
_ATTEMPT_MANAGERS = ("TaskAttempt.objects.create", "task_attempt_model.objects.create")


def _writes_an_attempt(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    rendered = ast.unparse(node.func)
    return rendered in _ATTEMPT_MANAGERS or rendered.rsplit(".", maxsplit=1)[-1] == "_record_failure"


def _accounted_for(call: ast.Call, lines: list[str]) -> bool:
    source = ast.unparse(call)
    if any(token in source for token in _CARRIES_SPEND):
        return True
    span = lines[call.lineno - 1 : (call.end_lineno or call.lineno)]
    return any(NO_USAGE_PRAGMA in line for line in span)


def unaccounted_attempt_writes() -> list[str]:
    """Every ``TaskAttempt`` write that neither carries spend nor declares it had none."""
    offenders: list[str] = []
    for path, tree in src_modules():
        lines = path.read_text(encoding="utf-8").splitlines()
        for node in ast.walk(tree):
            if _writes_an_attempt(node) and not _accounted_for(node, lines):
                assert isinstance(node, ast.Call)
                offenders.append(f"{path.relative_to(SRC_DIR.parents[1])}:{node.lineno}")
    return offenders


def test_every_attempt_write_carries_its_spend_or_says_why_not() -> None:
    offenders = unaccounted_attempt_writes()
    assert not offenders, (
        "These sites write a TaskAttempt without recording usage and without saying why: "
        f"{offenders}. Pass usage= (the run billed turns) or add a "
        f"'{NO_USAGE_PRAGMA} <why>' comment (no turn was billed, so the columns stay NULL — "
        "a zero would read as a measurement)."
    )


def test_the_walk_detects_an_unaccounted_write() -> None:
    """The control: a recorder added without either form is caught, not waved through."""
    source = "TaskAttempt.objects.create(task=task, error=reason)\n"
    call = ast.parse(source).body[0].value

    assert _writes_an_attempt(call)
    assert not _accounted_for(call, source.splitlines())


def test_the_pragma_is_only_honoured_on_the_calls_own_lines() -> None:
    """A pragma parked elsewhere in the module must not launder an unrelated write."""
    source = "# no-usage: unrelated\nTaskAttempt.objects.create(task=task, error=reason)\n"
    call = ast.parse(source).body[0].value

    assert not _accounted_for(call, source.splitlines())


def test_the_shared_usage_mapping_is_the_one_seam() -> None:
    """Both recorders map usage through ``usage_fields``, so they cannot diverge again."""
    recorder = (Path(SRC_DIR) / "agents" / "attempt_recorder.py").read_text(encoding="utf-8")
    assert "def usage_fields(" in recorder
    assert recorder.count("usage_fields(") >= 3  # the definition plus both recorders
