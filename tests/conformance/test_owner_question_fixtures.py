"""The owner-question tests carry placeholder identifiers only: no real workspace id, host or address (#4990).

A test module joins the scan by citing the issue number in its docstring, this one excepted. The placeholders are
the ``*0DEMO*`` Slack ids and the ``acme`` hosts; a bare ``U1`` owner id is too short to look like a real one.
"""

import ast
import re
from pathlib import Path

import pytest

from tests.conformance._src_tree import REPO_ROOT

_MARKER = "#4990"
_SLACK_ID = re.compile(r"\b[UCDWB](?=[A-Z0-9]*\d)[A-Z0-9]{8,}\b")
_PLACEHOLDER_ID = re.compile(r"^[UCDWB]0DEMO[A-Z0-9]*$")
_HOST = re.compile(r"(?:https?://|\b[\w.+-]+@)([A-Za-z0-9.-]+)")
_PLACEHOLDER_HOST = re.compile(r"(^|\.)acme\.")


def _scanned_modules() -> list[Path]:
    paths = []
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        docstring = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
        if docstring and _MARKER in docstring and path != Path(__file__).resolve():
            paths.append(path)
    return paths


def _real_looking(text: str) -> list[str]:
    ids = [found for found in _SLACK_ID.findall(text) if not _PLACEHOLDER_ID.match(found)]
    hosts = [host for host in _HOST.findall(text) if not _PLACEHOLDER_HOST.search(host)]
    return [*ids, *hosts]


@pytest.mark.parametrize("path", _scanned_modules(), ids=lambda path: path.name)
def test_a_module_carries_placeholder_ids_and_hosts_only(path: Path) -> None:
    assert _real_looking(path.read_text(encoding="utf-8")) == []


def test_the_scan_covers_the_owner_question_tests() -> None:
    names = {path.name for path in _scanned_modules()}

    assert {"test_question_binding_click.py", "test_owner_question_message.py", "test_question_card.py"} <= names


@pytest.mark.parametrize(
    "planted",
    [
        "C04ABCDEF12",  # privacy-scan:allow deliberate scanner-test fixture, reserved example name
        "https://real.example.org/x",
        "someone@real.example.org",  # privacy-scan:allow deliberate scanner-test fixture, reserved example name
    ],
)
def test_the_scan_flags_what_is_not_a_placeholder(planted: str) -> None:
    assert _real_looking(f"channel = {planted!r}") != []


@pytest.mark.parametrize("planted", ["C0DEMOCHAN1", "U0DEMOOWNER", "https://git.acme.example/x", "bob@acme.example"])
def test_the_scan_passes_the_placeholders(planted: str) -> None:
    assert _real_looking(f"channel = {planted!r}") == []
