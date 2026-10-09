# test-path: cross-cutting — walks teatree.eval, teatree.cli.eval and scripts/eval for one write seam.
"""Every file the eval code writes goes through the redacting ``write_artifact`` seam.

A redactor only protects the writes that call it, so a new ``write_text`` in a
report path would quietly ship an unredacted artifact. This walks the eval
packages and the eval scripts and fails on any file write outside the seam that
is not on the allowlist below — each entry says why that write is not an
artifact anyone uploads.
"""

import ast
import re
from collections.abc import Iterator
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_ROOTS = ("src/teatree/eval", "src/teatree/cli/eval", "scripts/eval")
_WRITE_METHODS = frozenset({"write_text", "write_bytes"})
#: Module functions that put a file on disk, however the module or the function is imported.
_MODULE_WRITES = {
    "shutil": frozenset({"copy", "copy2", "copyfile", "copytree", "move"}),
    "os": frozenset({"replace", "rename", "renames"}),
    "tempfile": frozenset({"NamedTemporaryFile"}),
}
_MODE = re.compile(r"[rwxabt+]{1,3}")

_ALLOWED = {
    # The seam itself: the one writer that redacts before it opens the file.
    "src/teatree/eval/artifact_redaction.py::Redactor.write",
    # Fixtures: the throwaway repos, stub CLIs, media and recorded runs a scenario starts from.
    "src/teatree/eval/git_fixture.py::_write",
    "src/teatree/eval/git_fixture.py::_write_recording",
    "src/teatree/eval/git_fixture.py::provision_agent_skill_dir_fixture",
    "src/teatree/eval/git_fixture.py::provision_e2e_artifacts_fixture",
    "src/teatree/eval/cli_stub_fixture.py::provision_cli_stubs",
    "src/teatree/eval/regression_corpus_fixtures.py::seed_repo_behind_but_clean",
    "src/teatree/eval/regression_corpus_fixtures.py::seed_repo_on_branch",
    "src/teatree/eval/regression_corpus_fixtures.py::seed_repo_with_diverging_target",
    "src/teatree/eval/regression_corpus_predicates.py::_check_account_switch_detect_and_recover",
    "scripts/eval/run_against_fixture.py::main",
    # Sandbox: files the agent under test reads inside its own throwaway cwd.
    "src/teatree/eval/eval_sandbox.py::_create_file",
    "src/teatree/eval/eval_sandbox.py::_replace_file",
    "src/teatree/eval/system_prompt_file.py::spill_system_prompt",
    "src/teatree/eval/production_hook_bridge.py::ProductionHookBridge._write_transcript",
    # Corpus: local inputs a maintainer reviews before committing (`label add` refuses a redaction hit).
    "src/teatree/cli/eval/label.py::add",
    "src/teatree/cli/eval/set_baseline.py::_write_baseline",
    "scripts/eval/corpus_gen/emit.py::write_catalog",
    "src/teatree/eval/subagent_capture.py::capture_to",
    "src/teatree/eval/transcript_manifest.py::write",
    # CI plumbing: $GITHUB_ENV and step metadata, never uploaded (the OAuth export must stay raw).
    "scripts/eval/select_oauth.py::_export",
    "scripts/eval/scenarios_for_changed.py::main",
}


def _mode_arg(call: ast.Call) -> ast.expr | None:
    keyword = next((kw.value for kw in call.keywords if kw.arg == "mode"), None)
    if keyword is not None:
        return keyword
    position = 0 if isinstance(call.func, ast.Attribute) else 1
    return call.args[position] if len(call.args) > position else None


def _opens_for_writing(call: ast.Call) -> bool:
    mode = _mode_arg(call)
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and _MODE.fullmatch(mode.value):
        return bool(set(mode.value) & set("wax+"))
    return True


class _WriteSites(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.sites: list[tuple[str, int]] = []
        self.module_aliases: dict[str, str] = {}
        self.imported_writes: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        self.module_aliases |= {
            alias.asname or alias.name: alias.name for alias in node.names if alias.name in _MODULE_WRITES
        }

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        writes = _MODULE_WRITES.get(node.module or "", frozenset())
        self.imported_writes |= {alias.asname or alias.name for alias in node.names if alias.name in writes}

    def _is_write(self, call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Name):
            return (
                func.id in self.imported_writes
                or func.id in _WRITE_METHODS
                or (func.id == "open" and _opens_for_writing(call))
            )
        if not isinstance(func, ast.Attribute):
            return False
        module = self.module_aliases.get(func.value.id, "") if isinstance(func.value, ast.Name) else ""
        return (
            func.attr in _MODULE_WRITES.get(module, frozenset())
            or func.attr in _WRITE_METHODS
            or (func.attr == "open" and _opens_for_writing(call))
        )

    def _scoped(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_write(node):
            self.sites.append((".".join(self.scope) or "<module>", node.lineno))
        self.generic_visit(node)


def _write_sites() -> Iterator[tuple[str, int]]:
    for root in _ROOTS:
        for path in sorted((_REPO / root).rglob("*.py")):
            visitor = _WriteSites()
            visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
            relative = path.relative_to(_REPO).as_posix()
            yield from ((f"{relative}::{scope}", line) for scope, line in visitor.sites)


def test_every_eval_file_write_goes_through_the_redacting_seam() -> None:
    unsanctioned = sorted(f"{site}:{line}" for site, line in _write_sites() if site not in _ALLOWED)

    assert unsanctioned == [], (
        "these eval writes bypass write_artifact, so a credential in their content reaches disk "
        f"unredacted — route them through teatree.eval.artifact_redaction.write_artifact: {unsanctioned}"
    )


def test_the_allowlist_names_only_live_write_sites() -> None:
    live = {site for site, _line in _write_sites()}

    assert live >= _ALLOWED, f"stale allowlist entries (no write there any more): {sorted(_ALLOWED - live)}"


def test_the_walker_sees_a_raw_write() -> None:
    visitor = _WriteSites()
    visitor.visit(ast.parse("def render(p):\n    p.write_text('x')\n    open(p, 'a')\n    p.open()\n"))

    assert visitor.sites == [("render", 2), ("render", 3)]


_COPIES_RENAMES_AND_TEMP_FILES = """\
import os
import shutil as sh
import tempfile
from os import rename as move_into
from shutil import copyfile
from tempfile import NamedTemporaryFile as Scratch


def publish(src, dst, text):
    sh.copy2(src, dst)
    copyfile(src, dst)
    os.replace(src, dst)
    os.rename(src, dst)
    move_into(src, dst)
    tempfile.NamedTemporaryFile("w", dir=dst)
    Scratch(dir=dst)
    text.replace("a", "b")
"""


def test_the_walker_sees_a_copy_a_rename_and_a_temp_file_however_imported() -> None:
    visitor = _WriteSites()
    visitor.visit(ast.parse(_COPIES_RENAMES_AND_TEMP_FILES))

    assert visitor.sites == [("publish", line) for line in range(10, 17)]
