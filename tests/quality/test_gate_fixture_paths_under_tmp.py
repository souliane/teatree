"""Fitness function: PreToolUse-gate fixtures never use a repo-relative source path.

The fragility class (souliane/teatree#2003, from the #1956 red-main fix): a
PreToolUse gate test embeds a repo-relative source path (``src/teatree/...``,
``hooks/scripts/...``) in its ``tool_input`` payload. On a checkout sitting on
``main`` (the push-to-main CI ``test`` job's container cwd), that path resolves
into a teatree-MANAGED protected-branch repo, so the higher-priority
``handle_protect_default_branch`` SAFETY gate fires FIRST and preempts the gate
under test. The test passes on every PR branch (protect gate inert) and only goes
red after landing on ``main`` — and CI ``maxfail=1`` hides the later failures.

The fix is to anchor such fixture paths under ``tmp_path`` (outside any
teatree-managed repo). This gate makes a regression mechanical instead of waiting
for the next red-main incident.

The scanner walks the ``tool_input`` of every hook-payload dict literal in
``tests/`` (a payload is a dict carrying a ``tool_name`` key — the PreToolUse hook
input shape) and flags a CONSTANT ``file_path``/``command`` value that starts with
a teatree-managed top-level prefix. Runtime-built paths (``str(tmp_path / ...)``,
a variable, an f-string) are invisible to a constant scan and are exactly the
right shape, so they are never flagged. A genuine repo-relative literal — e.g. a
test that asserts ``handle_protect_default_branch`` ITSELF fires — declares intent
with a ``# gate-fixture: repo-relative-ok`` pragma on the offending line.

Two halves, mirroring :mod:`tests.quality.test_patch_targets_resolve`:

:class:`TestLiveTree` is the gate itself — zero unanchored fixtures across
``tests/``.

:class:`TestGoldenCorpus` proves the scanner is neither vacuous (a must-FLAG set)
nor over-blocking (a symmetric must-NOT-FLAG set: ``tmp_path``-anchored, a
variable value, a bare-predicate string outside any payload, the pragma escape).
"""

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_ROOT = _REPO_ROOT / "tests"

# Top-level dirs that ARE teatree-managed source: a ``file_path`` resolving into
# one of these on a ``main`` checkout trips ``handle_protect_default_branch``.
_MANAGED_PREFIXES = ("src/", "hooks/", "scripts/", "plugins/", "docs/")

# The payload keys whose value reaches the gate as a path/command.
_PAYLOAD_PATH_KEYS = ("file_path", "command")

# A line carrying this pragma opts a deliberate repo-relative fixture out of the
# gate (e.g. a test that asserts the protect-default-branch gate itself fires).
_PRAGMA = "gate-fixture: repo-relative-ok"

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gate_fixture_paths"
_MUST_FLAG = sorted((_FIXTURES / "must_flag").glob("*.py.txt"))
_MUST_NOT_FLAG = sorted((_FIXTURES / "must_not_flag").glob("*.py.txt"))


@dataclass(frozen=True)
class FixturePathFinding:
    """A hook-payload fixture whose path value is a repo-relative source literal."""

    path: Path
    lineno: int
    key: str
    value: str


def _value_is_repo_relative_source(value: object) -> bool:
    """A constant ``file_path``/``command`` value that resolves into managed source."""
    return isinstance(value, str) and value.startswith(_MANAGED_PREFIXES)


def _pragma_lines(source: str) -> set[int]:
    """1-based line numbers carrying the opt-out pragma."""
    return {i for i, line in enumerate(source.splitlines(), start=1) if _PRAGMA in line}


def _iter_tool_input_constants(payload: ast.Dict) -> list[tuple[str, ast.Constant]]:
    """The constant ``file_path``/``command`` nodes inside a payload's ``tool_input``."""
    out: list[tuple[str, ast.Constant]] = []
    for key, value in zip(payload.keys, payload.values, strict=True):
        if not (isinstance(key, ast.Constant) and key.value == "tool_input" and isinstance(value, ast.Dict)):
            continue
        for sub_key, sub_value in zip(value.keys, value.values, strict=True):
            if (
                isinstance(sub_key, ast.Constant)
                and sub_key.value in _PAYLOAD_PATH_KEYS
                and isinstance(sub_value, ast.Constant)
            ):
                out.append((sub_key.value, sub_value))
    return out


def _payload_carries_tool_name(node: ast.Dict) -> bool:
    """A PreToolUse hook payload is a dict literal with a ``tool_name`` key."""
    return any(isinstance(key, ast.Constant) and key.value == "tool_name" for key in node.keys)


def scan_source(source: str, path: Path) -> list[FixturePathFinding]:
    """Findings for one parsed source string."""
    tree = ast.parse(source)
    pragma = _pragma_lines(source)
    findings: list[FixturePathFinding] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Dict) and _payload_carries_tool_name(node)):
            continue
        for key, const in _iter_tool_input_constants(node):
            if _value_is_repo_relative_source(const.value) and const.lineno not in pragma:
                findings.append(FixturePathFinding(path, const.lineno, key, const.value))
    return findings


def scan_file(path: Path) -> list[FixturePathFinding]:
    """Findings for one file (``.py`` or a ``.py.txt`` corpus fixture)."""
    return scan_source(path.read_text(encoding="utf-8"), path)


def scan_tree(root: Path) -> list[FixturePathFinding]:
    """Findings across every ``*.py`` test module under ``root``."""
    findings: list[FixturePathFinding] = []
    for py in sorted(root.rglob("*.py")):
        findings.extend(scan_file(py))
    return findings


class TestLiveTree:
    def test_no_gate_fixture_uses_a_repo_relative_path(self) -> None:
        findings = scan_tree(_TESTS_ROOT)
        assert not findings, (
            "PreToolUse-gate fixture(s) embed a repo-relative source path — anchor under tmp_path "
            "(or add a `# gate-fixture: repo-relative-ok` pragma if the gate under test is "
            "protect-default-branch itself):\n"
            + "\n".join(f"  {f.path.relative_to(_REPO_ROOT)}:{f.lineno}: {f.key}={f.value!r}" for f in findings)
        )


class TestScanner:
    def test_repo_relative_file_path_is_flagged(self) -> None:
        src = 'd = {"tool_name": "Edit", "tool_input": {"file_path": "src/teatree/core/x.py"}}\n'
        findings = scan_source(src, Path("x.py"))
        assert len(findings) == 1
        assert findings[0].key == "file_path"
        assert findings[0].value == "src/teatree/core/x.py"

    def test_tmp_anchored_file_path_is_not_flagged(self) -> None:
        src = 'd = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "x.py")}}\n'
        assert scan_source(src, Path("x.py")) == []

    def test_variable_file_path_is_not_flagged(self) -> None:
        src = 'd = {"tool_name": "Edit", "tool_input": {"file_path": file_path}}\n'
        assert scan_source(src, Path("x.py")) == []

    def test_bare_predicate_string_outside_payload_is_not_flagged(self) -> None:
        # A pure-predicate test parametrizes a path string with no tool_name payload.
        src = 'cases = ["src/teatree/core/models.py", "tests/test_x.py"]\n'
        assert scan_source(src, Path("x.py")) == []

    def test_tool_input_without_tool_name_is_not_flagged(self) -> None:
        src = 'd = {"tool_input": {"file_path": "src/teatree/core/x.py"}}\n'
        assert scan_source(src, Path("x.py")) == []

    def test_non_managed_relative_path_is_not_flagged(self) -> None:
        # A bare filename (or a non-source prefix) cannot resolve into managed source.
        src = 'd = {"tool_name": "Read", "tool_input": {"file_path": "x.py"}}\n'
        assert scan_source(src, Path("x.py")) == []

    def test_repo_relative_command_is_flagged(self) -> None:
        src = 'd = {"tool_name": "Bash", "tool_input": {"command": "src/teatree/run.py"}}\n'
        findings = scan_source(src, Path("x.py"))
        assert len(findings) == 1
        assert findings[0].key == "command"

    def test_pragma_opts_out_a_deliberate_repo_relative_fixture(self) -> None:
        src = f'd = {{"tool_name": "Edit", "tool_input": {{"file_path": "src/teatree/core/x.py"}}}}  # {_PRAGMA}\n'
        assert scan_source(src, Path("x.py")) == []

    def test_finding_carries_location(self) -> None:
        src = '\n\nd = {"tool_name": "Edit", "tool_input": {"file_path": "hooks/scripts/x.py"}}\n'
        findings = scan_source(src, Path("y.py"))
        assert len(findings) == 1
        assert findings[0].lineno == 3
        assert findings[0].path == Path("y.py")

    def test_scan_tree_skips_nonexistent_root(self) -> None:
        assert scan_tree(_REPO_ROOT / "does_not_exist") == []


class TestGoldenCorpus:
    def test_corpus_has_both_dimensions(self) -> None:
        assert _MUST_FLAG, "must-FLAG corpus is empty"
        assert _MUST_NOT_FLAG, "must-NOT-FLAG corpus is empty (over-block dimension missing)"

    @pytest.mark.parametrize("fixture", _MUST_FLAG, ids=[p.stem for p in _MUST_FLAG])
    def test_must_flag_fixture_is_flagged(self, fixture: Path) -> None:
        assert scan_file(fixture), f"{fixture.name} should produce a finding but did not"

    @pytest.mark.parametrize("fixture", _MUST_NOT_FLAG, ids=[p.stem for p in _MUST_NOT_FLAG])
    def test_must_not_flag_fixture_is_not_flagged(self, fixture: Path) -> None:
        findings = scan_file(fixture)
        assert not findings, f"{fixture.name} wrongly flagged: " + ", ".join(f"{f.key}={f.value!r}" for f in findings)
