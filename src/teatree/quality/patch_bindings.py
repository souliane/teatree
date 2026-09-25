"""Does a patch reach the binding the SUBJECT reads, or only the one it was defined on?

:mod:`teatree.quality.patch_targets` asks whether a patch string RESOLVES. That is
necessary and not sufficient: ``patch("teatree.core.overlay_loader.get_overlay")``
resolves perfectly, which is exactly why the dogfood bug shipped — the subject held its
own module-level ``from teatree.core.overlay_loader import get_overlay``, so the patch
replaced a name the code under test never reads, the real call ran, and a bare ``except``
turned its failure into the value two of the three tests asserted.

WHY THE GENERAL CHECK IS NOT TRACTABLE, AND WHAT IS
---------------------------------------------------
The question "does this patch reach the code under test" needs to know WHICH module is
under test, and a test file names many. Measured over this tree: taking every first-party
module a test file imports as a candidate subject yields 81 findings, and the spot checks
are false — ``test_slack_setup.py`` patches ``manifest._slack_app_api`` for
``export_manifest``, which lives in ``manifest``; the flagged consumer is a module the
file merely imports. Widening further (any from-import depth) yields 991. A gate at
either size is noise, and the reviewer's 220-target sweep is the same measurement: it is
the BLIND SPOT, not a bug count.

What IS decidable is the subject: this repo mirrors test paths onto module paths
(``tests/teatree_<pkg>/<sub>/test_<leaf>.py`` ↔ ``src/teatree/<pkg>/<sub>/<leaf>.py``,
the rule :mod:`teatree.quality.test_path_mirror` already enforces), so 901 of 2604 test
files name exactly one subject. Restricted to those, and to a name the subject actually
READS, the live tree yields ONE finding — small enough to be a ratchet rather than a
backlog. Coverage is therefore partial by construction: a cross-cutting test file names
no single subject and is not checked here.

The other half of the precision is import DEPTH. A ``from M import attr`` inside a
function body re-imports at call time, so a patch on ``M`` DOES reach it; only a
MODULE-LEVEL from-import creates the second, permanent binding that a patch misses.
Reading imports at any depth is what produced the 991.

A patch that deliberately targets the defining module — because the subject's own binding
is asserted separately — declares itself with a ``# patch-binding: defining-module``
pragma on the call's line, the same shape ``patch_targets`` gives a genuinely dynamic
target.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from teatree.quality.patch_targets import patch_target_calls

DEFINING_MODULE_PRAGMA = "patch-binding: defining-module"


@dataclass(frozen=True)
class PatchBindingFinding:
    """A patch that resolves, on a module the subject under test does not read it from."""

    path: Path
    lineno: int
    target: str
    subject: str
    attribute: str

    @property
    def unreached_binding(self) -> str:
        return f"{self.subject}.{self.attribute}"


def module_index(src_root: Path, package: str) -> dict[str, Path]:
    """``{dotted module: file}`` for every module under ``src_root/<package>``."""
    index: dict[str, Path] = {}
    for path in sorted((src_root / package).rglob("*.py")):
        parts = list(path.relative_to(src_root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            index[".".join(parts)] = path
    return index


def mirrored_subject(test_path: Path, tests_root: Path, index: dict[str, Path], package: str) -> str | None:
    """The one module ``test_path``'s own path names, or ``None`` when it names none."""
    parts = list(test_path.relative_to(tests_root).parts)
    if not parts[-1].startswith("test_"):
        return None
    directories = parts[:-1]
    if not directories:
        dotted = [package]
    elif directories[0].startswith(f"{package}_"):
        dotted = [package, directories[0][len(package) + 1 :], *directories[1:]]
    else:
        return None
    candidate = ".".join([*dotted, parts[-1][len("test_") : -len(".py")]])
    return candidate if candidate in index else None


def _module_level_bindings(source: Path) -> dict[tuple[str, str], str]:
    """``{(source module, imported name): bound name}`` for MODULE-LEVEL from-imports."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    return {
        (node.module, alias.name): alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
        for alias in node.names
    }


def _loaded_names(source: Path) -> frozenset[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    return frozenset(
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    )


def _subject_reads(source: Path) -> dict[tuple[str, str], str]:
    """The module-level from-import bindings the subject actually READS."""
    loaded = _loaded_names(source)
    return {key: bound for key, bound in _module_level_bindings(source).items() if bound in loaded}


def _pragma_lines(source: str) -> set[int]:
    return {lineno for lineno, line in enumerate(source.splitlines(), start=1) if DEFINING_MODULE_PRAGMA in line}


def scan_file(test_path: Path, tests_root: Path, index: dict[str, Path], package: str) -> Iterator[PatchBindingFinding]:
    subject = mirrored_subject(test_path, tests_root, index, package)
    if subject is None:
        return
    source = test_path.read_text(encoding="utf-8")
    calls = patch_target_calls(source, test_path)
    patched = {target for _lineno, target in calls}
    pragma_lines = _pragma_lines(source)
    reads = _subject_reads(index[subject])
    for lineno, target in calls:
        module, _, attribute = target.rpartition(".")
        if module == subject or module not in index or lineno in pragma_lines:
            continue
        if (module, attribute) in reads and f"{subject}.{attribute}" not in patched:
            yield PatchBindingFinding(test_path, lineno, target, subject, attribute)


def scan_tree(tests_root: Path, src_root: Path, *, package: str = "teatree") -> list[PatchBindingFinding]:
    """Every patch in ``tests_root`` that misses its mirrored subject's own binding."""
    index = module_index(src_root, package)
    return [
        finding
        for test_path in sorted(tests_root.rglob("test_*.py"))
        for finding in scan_file(test_path, tests_root, index, package)
    ]
