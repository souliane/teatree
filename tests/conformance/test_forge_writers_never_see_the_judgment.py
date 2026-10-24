"""Issue writers keep duplicate judgments away from outbound body and comment text.

The walk checks lexical references inside direct forge writer functions; it does not
follow helper calls or aliases across functions.
"""

import ast
import re
from collections.abc import Iterable
from pathlib import Path

import pytest

from tests.conformance._src_tree import src_modules

_WRITES = {"create_issue", "update_issue", "post_issue_comment"}
_JUDGMENT_NAMES = {"decisions", "CreateDecision", "rejected", "judgment"}
_PUBLIC_AUDIT_TEXT = re.compile(r"rejected candidates|dedupe check", re.IGNORECASE)


def _violations(modules: Iterable[tuple[Path, ast.Module]]) -> tuple[int, list[str]]:
    writers = 0
    failures: list[str] = []
    for path, module in modules:
        docstrings = {
            id(owner.body[0].value)
            for owner in ast.walk(module)
            if isinstance(owner, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and owner.body
            and isinstance(owner.body[0], ast.Expr)
            and isinstance(owner.body[0].value, ast.Constant)
            and isinstance(owner.body[0].value.value, str)
        }
        for node in ast.walk(module):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and _PUBLIC_AUDIT_TEXT.search(node.value)
            ):
                failures.append(f"{path}:{node.lineno}: public duplicate-audit text")
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = [
                child
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr in _WRITES
            ]
            if not calls:
                continue
            writers += 1
            names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
            names.update(child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute))
            names.update(arg.arg for arg in node.args.args)
            if exposed := names & _JUDGMENT_NAMES:
                failures.append(f"{path}:{node.lineno}: writer {node.name} sees {', '.join(sorted(exposed))}")
    return writers, failures


def test_forge_writers_cannot_format_judgments_into_public_text() -> None:
    writers, failures = _violations(src_modules())
    assert writers > 0
    assert failures == []


def test_walk_detects_a_writer_that_publishes_a_rejection_list() -> None:
    source = 'def publish(decisions, host):\n    host.create_issue(body="Rejected candidates:" + str(decisions))\n'
    writers, failures = _violations(((Path("synthetic.py"), ast.parse(source)),))
    assert writers == 1
    assert len(failures) == 2


@pytest.mark.parametrize("owner", ["self", "draft"])
def test_walk_detects_attribute_held_judgments(owner: str) -> None:
    source = f"def publish({owner}, host):\n    host.create_issue(body={owner}.decisions)\n"
    writers, failures = _violations(((Path("synthetic.py"), ast.parse(source)),))
    assert writers == 1
    assert len(failures) == 1
