# test-path: cross-cutting — scans BLUEPRINT.md and docs/blueprint/ for code references;
# its subject is the architecture spec, not any one package.
"""Every first-party path, private name and env var the architecture spec cites must exist in the tree.

A module moved into a package or a helper renamed leaves the spec pointing at nothing,
and a reader who follows it finds no code. Paths resolve the way the spec writes them:
package-relative, repo-relative, or through a Django app's ``templates``/``static`` root.
"""

import re
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = REPO_ROOT / "src" / "teatree"
SPEC = (REPO_ROOT / "BLUEPRINT.md", *sorted((REPO_ROOT / "docs" / "blueprint").glob("*.md")))
CODE_ROOTS = ("src", "hooks", "scripts", "dev", "deploy")
CODE_SUFFIXES = frozenset({".py", ".sh", ".zsh", ""})
#: Box-local files the spec names but the repo never holds.
RUNTIME_SUFFIXES = frozenset({".env", ".json"})
#: Private names of third-party code the spec cites by their own name.
THIRD_PARTY_PRIVATE = frozenset({"_find_cli"})

_SUBPACKAGES = "|".join(sorted(p.name for p in PACKAGE.iterdir() if (p / "__init__.py").is_file()))
PATH_REF = re.compile(rf"`(?:src/teatree/|teatree/)?((?:{_SUBPACKAGES})/[\w/.-]+?)`")
DOTTED_REF = re.compile(r"`([\w.]+)(?:\(\))?`")
PRIVATE_SEGMENT = re.compile(r"_[a-z]\w{3,}")
ENV_REF = re.compile(r"\b((?:T3|TEATREE)_[A-Z][A-Z0-9_]{2,})\b")


def _spec_refs(pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    return [
        (f"{doc.relative_to(REPO_ROOT)}:{number}", ref)
        for doc in SPEC
        for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1)
        for ref in pattern.findall(line)
    ]


def _private_refs() -> list[tuple[str, str]]:
    return [
        (where, segment)
        for where, ref in _spec_refs(DOTTED_REF)
        for segment in ref.split(".")
        if PRIVATE_SEGMENT.fullmatch(segment) and segment not in THIRD_PARTY_PRIVATE
    ]


@cache
def _code_words() -> frozenset[str]:
    return frozenset(
        word
        for root in CODE_ROOTS
        for path in (REPO_ROOT / root).rglob("*")
        if path.suffix in CODE_SUFFIXES and path.is_file()
        for word in re.findall(r"\w+", path.read_text(encoding="utf-8", errors="replace"))
    )


def _path_resolves(ref: str) -> bool:
    app = ref.split("/", 1)[0]
    candidates = [base / ref for base in (PACKAGE, REPO_ROOT, PACKAGE / app / "templates", PACKAGE / app / "static")]
    if any(c.exists() or c.with_suffix(".py").is_file() for c in candidates):
        return True
    module, _, attr = ref.rpartition(".")
    owner = next((base / f"{module}.py" for base in (PACKAGE, REPO_ROOT) if (base / f"{module}.py").is_file()), None)
    return owner is not None and re.search(rf"\b{re.escape(attr)}\b", owner.read_text(encoding="utf-8")) is not None


def test_the_scan_reads_the_spec() -> None:
    """Guards the guard: a pattern that stopped matching would make the checks below vacuous."""
    assert len(_spec_refs(PATH_REF)) >= 200
    assert len(_private_refs()) >= 50
    assert len(_spec_refs(ENV_REF)) >= 50


def test_every_cited_path_resolves() -> None:
    stale = [
        f"{where}: {ref}"
        for where, ref in _spec_refs(PATH_REF)
        if Path(ref).suffix not in RUNTIME_SUFFIXES and not _path_resolves(ref)
    ]
    assert not stale, "spec paths with no file or symbol behind them:\n  " + "\n  ".join(stale)


def test_every_cited_private_name_exists() -> None:
    stale = [f"{where}: {name}" for where, name in _private_refs() if name not in _code_words()]
    assert not stale, "spec names no code defines:\n  " + "\n  ".join(stale)


def test_every_cited_env_var_is_read() -> None:
    stale = [f"{where}: {name}" for where, name in _spec_refs(ENV_REF) if name not in _code_words()]
    assert not stale, "spec env vars no code reads:\n  " + "\n  ".join(stale)
