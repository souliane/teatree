"""Merged-branch detection stays single-homed in ``src/`` (#4070).

``git cherry``, ``git branch --merged`` and ``git merge-base --is-ancestor`` are the
primitives a landed-ness question gets hand-rolled from, and each one misreads a
squash-merge on its own: the squash rewrites the branch's commits into a new sha, so
per-commit and ancestor tests both report landed work as unmerged. The layered answer
lives in :mod:`teatree.core.worktree.branch_classification`, and this walk pins that no
NEW call site re-derives it somewhere else.

A forward ratchet, not a repair — every call site below is already accounted for, either
as the canonical detector itself or as a use that is not a landed-ness question at all.
The breach it complements was typed bash, which the PreToolUse advisory
(``hooks/scripts/merged_detection_probe_gate.py``) covers; ``src/`` is the half a test
can hold hard.

Its reach is one argv list literal at a call site. An argv assembled across statements,
or built from a variable subcommand, is out of an AST walk's reach — the same honesty the
index-blind walk states about itself.
"""

import ast
from dataclasses import dataclass

from tests.conformance._src_tree import SRC_DIR, src_modules

# Below this the walk cannot have been looking at the real source at all.
_MIN_PROBE_CALL_SITES = 7

#: Where a merged-detection primitive may legitimately be invoked, and why.
_ALLOWED: dict[str, str] = {
    "core/worktree/branch_classification.py": "the canonical three-layer detector itself",
    "utils/git_branch.py": "the thin `branch_merged` wrapper the canonical detector's layer (c) calls",
    "core/management/commands/repro.py": "proves a RED sha is an ancestor of a GREEN sha — provenance, not landed-ness",
    "core/merge/conflict_only.py": "proves a merge's second parent is an ancestor of a FRESH base — review currency",
    "core/management/commands/_workspace/cleanup.py": (
        "the branch-prune pass's plain-merge class; the squash class in the same loop goes through `is_squash_merged`"
    ),
}


@dataclass(frozen=True, slots=True)
class ProbeSite:
    """One argv literal invoking a merged-detection primitive."""

    path: str
    primitive: str


def _literal_words(node: ast.List) -> list[str]:
    """The string-literal elements of *node*, ignoring the interpolated ones.

    A real call site holds its refs in variables (``["cherry", target, branch]``), so
    demanding every element resolve would miss every site this walk exists to see. The
    primitive is always the literal.
    """
    return [el.value for el in node.elts if isinstance(el, ast.Constant) and isinstance(el.value, str)]


def probe_primitive(words: list[str]) -> str | None:
    """The merged-detection primitive *words* invokes, or ``None``.

    ``--merged`` counts only on a ``git branch`` argv: the forge queries
    (``glab mr list --merged``) carry the same flag and ask the forge, not git.
    """
    if "cherry" in words:
        return "git cherry"
    if "--merged" in words and "branch" in words:
        return "git branch --merged"
    if "--is-ancestor" in words:
        return "git merge-base --is-ancestor"
    return None


def probe_sites(tree: ast.Module, path: str) -> list[ProbeSite]:
    """Every merged-detection argv passed as a call argument in *tree*."""
    sites: list[ProbeSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for candidate in (*node.args, *(keyword.value for keyword in node.keywords)):
            if not isinstance(candidate, ast.List):
                continue
            if primitive := probe_primitive(_literal_words(candidate)):
                sites.append(ProbeSite(path=path, primitive=primitive))
    return sites


def walk_source() -> list[ProbeSite]:
    """Every merged-detection call site under ``src/teatree``."""
    return [site for path, tree in src_modules() for site in probe_sites(tree, path.relative_to(SRC_DIR).as_posix())]


class TestMergedDetectionStaysSingleHomed:
    def test_the_walk_reaches_the_real_call_sites(self) -> None:
        sites = walk_source()

        assert len(sites) >= _MIN_PROBE_CALL_SITES, f"the walk only reached {len(sites)} call sites — it is broken"

    def test_every_probe_call_site_is_accounted_for(self) -> None:
        stray = sorted({f"{site.path}: {site.primitive}" for site in walk_source() if site.path not in _ALLOWED})

        assert stray == [], (
            "a merged-detection primitive outside its single home — route the question through "
            "`teatree.core.worktree.branch_verdict.branch_verdict_report`, or add the site to "
            "`_ALLOWED` with the reason it is not asking whether a branch landed"
        )

    def test_the_canonical_detector_is_among_the_sites(self) -> None:
        # Anti-vacuity in the other direction: an allow-list that stopped matching
        # anything would leave the accounting test trivially green.
        assert {site.path for site in walk_source()} >= {"core/worktree/branch_classification.py"}


class TestTheWalkFiresRed:
    def test_a_planted_cherry_argv_is_flagged(self) -> None:
        planted = ast.parse('git.run_strict(repo=repo, args=["cherry", "origin/main", branch])')

        assert [site.primitive for site in probe_sites(planted, "core/loop/whatever.py")] == ["git cherry"]

    def test_a_planted_branch_merged_argv_is_flagged(self) -> None:
        planted = ast.parse('git.run(repo=repo, args=["branch", "--merged", target])')

        assert [site.primitive for site in probe_sites(planted, "core/loop/whatever.py")] == ["git branch --merged"]

    def test_a_planted_is_ancestor_argv_is_flagged(self) -> None:
        planted = ast.parse('git.check(repo=repo, args=["merge-base", "--is-ancestor", a, b])')

        assert [site.primitive for site in probe_sites(planted, "x.py")] == ["git merge-base --is-ancestor"]

    def test_a_forge_merged_query_is_not_a_git_probe(self) -> None:
        forge = ast.parse('probe(["glab", "mr", "list", "--merged", "--source-branch", branch])')

        assert probe_sites(forge, "x.py") == []

    def test_an_unrelated_argv_is_not_flagged(self) -> None:
        unrelated = ast.parse('git.run(repo=repo, args=["log", branch, "--not", target, "--oneline"])')

        assert probe_sites(unrelated, "x.py") == []
