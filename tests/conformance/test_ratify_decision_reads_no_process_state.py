"""The modules that decide a ratification read stored rows only, never the process environment or terminal."""

import ast

import pytest

from tests.conformance._src_tree import SRC_DIR

_DECIDING_MODULES = (
    "core/models/deferred_question.py",
    "core/models/directive.py",
    "core/models/outer_loop_experiment.py",
    "core/models/approval_metrics.py",
    "loop/question_binding.py",
    "core/management/commands/questions.py",
    "mcp/write_tools.py",
    *sorted(str(path.relative_to(SRC_DIR)) for path in SRC_DIR.glob("loops/*/ratify.py")),
)
_PROCESS_PROBES = frozenset({"environ", "getenv", "isatty", "getppid", "ttyname", "psutil"})
_PROCESS_MODULES = frozenset({"psutil", "teatree.core.session_identity", "teatree.utils.env"})


def _process_reads(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in _PROCESS_PROBES:
            found.append(node.attr)
        elif isinstance(node, ast.Name) and node.id in _PROCESS_PROBES:
            found.append(node.id)
        elif isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names if alias.name in _PROCESS_MODULES)
        elif isinstance(node, ast.ImportFrom) and node.module in _PROCESS_MODULES:
            found.append(node.module)
    return found


def test_both_ratify_loops_are_scanned() -> None:
    assert {"loops/directive_loop/ratify.py", "loops/outer_loop/ratify.py"} <= set(_DECIDING_MODULES)


@pytest.mark.parametrize(
    "probe",
    [
        "import os\nos.environ.get('X')",
        "import os\nos.getenv('X')",
        "import sys\nsys.stdin.isatty()",
        "import os\nos.getppid()",
        "import os\nos.ttyname(0)",
        "import psutil",
        "from teatree.core.session_identity import current_session_id",
        "from teatree.utils.env import patched_environ",
    ],
)
def test_the_scan_sees_each_process_read(probe: str) -> None:
    assert _process_reads(probe)


@pytest.mark.parametrize("module", _DECIDING_MODULES)
def test_a_deciding_module_reads_no_process_state(module: str) -> None:
    assert _process_reads((SRC_DIR / module).read_text(encoding="utf-8")) == []
