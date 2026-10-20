# test-path: cross-cutting — scans BLUEPRINT.md and docs/blueprint/ for code references;
# its subject is the architecture spec, not any one package.
"""Every first-party path, private name and env var the architecture spec cites must exist in the tree.

A module moved into a package or a helper renamed leaves the spec pointing at nothing,
and a reader who follows it finds no code. Paths resolve the way the spec writes them:
package-relative, repo-relative, or through a Django app's ``templates``/``static`` root.
"""

import ast
import re
from functools import cache
from pathlib import Path

from teatree.quality import ref_baseline
from teatree.quality.skill_symbol_refs import _exempt_lines, scan_file, scan_source

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = REPO_ROOT / "src" / "teatree"
SPEC = tuple(
    doc for doc in ref_baseline.charter_docs(REPO_ROOT) if doc.name == "BLUEPRINT.md" or doc.parent.name == "blueprint"
)
CODE_ROOTS = ("src", "hooks", "scripts", "dev", "deploy")
CODE_SUFFIXES = frozenset({".py", ".sh", ".zsh"})

DOTTED_REF = re.compile(r"`([\w.]+)(?:\(\))?`")
PRIVATE_SEGMENT = re.compile(r"_[a-z]\w{3,}")
ENV_REF = re.compile(r"\b((?:T3|TEATREE)_[A-Z][A-Z0-9_]{2,})\b")


def _spec_refs(pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    return [
        (f"{doc.relative_to(REPO_ROOT)}:{number}", ref)
        for doc in SPEC
        for source in [doc.read_text(encoding="utf-8")]
        for exempt in [_exempt_lines(source)]
        for number, line in enumerate(source.splitlines(), 1)
        if number not in exempt
        for ref in pattern.findall(line)
    ]


def _private_refs() -> list[tuple[str, str]]:
    return [
        (where, segment)
        for where, ref in _spec_refs(DOTTED_REF)
        for segment in ref.split(".")
        if PRIVATE_SEGMENT.fullmatch(segment)
    ]


@cache
def _code_words() -> frozenset[str]:
    words: set[str] = {p.name for p in PACKAGE.rglob("*") if p.is_dir() and (p / "__init__.py").is_file()}
    for root in CODE_ROOTS:
        for path in (REPO_ROOT / root).rglob("*"):
            if not path.is_file() or path.name.startswith(".") or path.suffix not in CODE_SUFFIXES:
                continue
            source = path.read_text(encoding="utf-8")
            if path.suffix == ".py":
                words.update(_python_code_words(source))
                words.add(path.stem)
            else:
                active_lines = (line for line in source.splitlines() if not line.lstrip().startswith("#"))
                words.update(re.findall(r"\w+", "\n".join(active_lines)))
    words.update(p.stem for p in PACKAGE.rglob("*.html"))
    return frozenset(words)


def _python_code_words(source: str) -> set[str]:
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    words: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            words.add(node.name)
        elif isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Store):
            words.add(node.id if isinstance(node, ast.Name) else node.attr)
        elif isinstance(node, ast.arg):
            words.add(node.arg)
        elif isinstance(node, ast.alias):
            words.add(node.asname or node.name.rsplit(".", 1)[-1])
        elif (
            isinstance(node, ast.Constant)
            and id(node) not in docstrings
            and isinstance(node.value, str)
            and ENV_REF.fullmatch(node.value)
        ):
            words.add(node.value)
    return words


def test_the_scan_reads_the_spec() -> None:
    """Guards the guard: a pattern that stopped matching would make the checks below vacuous."""
    assert sum(1 for doc in SPEC for finding in scan_file(doc, REPO_ROOT, charter=True) if "/" in finding.ref) >= 200
    assert len(_private_refs()) >= 50
    assert len(_spec_refs(ENV_REF)) >= 50
    nested = scan_source(
        "`_workspace/provision_parallel.run_worktree_provisions_in_parallel`",
        REPO_ROOT / "BLUEPRINT.md",
        REPO_ROOT,
        charter=True,
    )
    assert len(nested) == 1
    assert nested[0].reason is None


def test_every_cited_path_resolves() -> None:
    spec = REPO_ROOT / "docs/blueprint/admission-governor.md"
    source = spec.read_text(encoding="utf-8")
    assert "core/factory/drain_starvation.read_drain_lane_state" in source
    mutated = source.replace(
        "core/factory/drain_starvation.read_drain_lane_state",
        "core/factory/drain_starvation.read_drain_lane_state_GONE",
    )
    findings = scan_source(mutated, spec, REPO_ROOT, charter=True)
    assert any(f.ref.endswith("read_drain_lane_state_GONE") and f.reason is not None for f in findings)


def test_every_cited_private_name_exists() -> None:
    assert "_retired_symbol" not in _python_code_words('# _retired_symbol\n"""_retired_symbol"""\nVALUE = 1\n')
    stale = [f"{where}: {name}" for where, name in _private_refs() if name not in _code_words()]
    assert not stale, "spec names no code defines:\n  " + "\n  ".join(stale)


def test_every_cited_env_var_is_read() -> None:
    assert "T3_RETIRED_TOKEN" not in _python_code_words('"""T3_RETIRED_TOKEN"""\nVALUE = 1\n')
    stale = [f"{where}: {name}" for where, name in _spec_refs(ENV_REF) if name not in _code_words()]
    assert not stale, "spec env vars no code reads:\n  " + "\n  ".join(stale)
