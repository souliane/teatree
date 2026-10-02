"""Every hook that reads git-reported PATHS has decided which tree they name.

prek runs a NESTED project's hooks — any directory carrying its own
``.pre-commit-config.yaml`` — from that project's directory, and git exports
``GIT_DIR`` to a hook fired from a linked worktree, where it treats the current
directory as the top of the work tree. So a hook reading ``git diff --cached``
inside a vendored copy of this project gets names relative to the FORK's root
while every path literal it owns is relative to this project's root. Joined, they
address a file that exists nowhere; matched against a literal, they never hit.
Either way the hook examines nothing and exits 0.

The class is invisible precisely because it is silent, so it cannot be left to
the next author to notice. This scan finds every hook module that builds a RAW
git argv whose output names work-tree paths, and refuses it unless one of two
things is true:

- it routes those names through :mod:`teatree.utils.work_tree`, which re-roots
    them onto the project they belong to — the default, and where any gate
    carrying project-relative literals must be; or
- it is registered in :data:`WHOLE_WORK_TREE`, meaning it reads the whole work
    tree ON PURPOSE and never resolves a name against a project-relative literal
    or directory, so the vendoring prefix cannot make it miss. Each entry states
    why, because "it happens to still work" and "it cannot be blinded" are
    different claims and only the second belongs here.

Neither is the failure: a new hook has been written and nobody has decided which
tree its paths name.
"""

import ast
from pathlib import Path

_CORE_ROOT = Path(__file__).resolve().parents[2]

#: Git subcommands whose output NAMES paths in the work tree.
_PATH_PRODUCING = frozenset({"diff", "diff-index", "diff-tree", "ls-files", "show", "status"})

#: ``rev-parse`` only names a path with one of these.
_PATH_PRODUCING_FLAGS = frozenset({"--show-toplevel", "--show-prefix", "--show-cdup"})

_SCAN_ROOTS = ("scripts/hooks", "src/teatree/hooks")
_SCAN_FILES = ("src/teatree/quality/diff_base.py",)

_HELPER_MODULE = "teatree.utils.work_tree"
_HELPER_NAME = "work_tree"

WHOLE_WORK_TREE: dict[str, str] = {
    "scripts/hooks/check_antipatterns.py": (
        "Scans diff LINES against the catalog's own regexes; the path is carried into the "
        "report only. Its self-exclusion prefixes stop matching under a vendoring prefix, "
        "which makes it noisier, never blinder — and it is manual-stage and always exits 0."
    ),
    "scripts/hooks/check_comment_density.py": (
        "Advisory, always exits 0. Reads diff LINES; the tests/docs exemptions are the only "
        "path use, and losing them over-reports rather than under-reports."
    ),
    "scripts/hooks/check_no_overlay_leak.py": (
        "A leak gate: it must see the WHOLE work tree a fork commits, not this project's "
        "slice. Its staged-name pass falls through to a full tree walk, so a name it does "
        "not recognise widens the scan instead of emptying it."
    ),
    "scripts/hooks/check_skill_prose.py": (
        "Every path pattern it owns is ``(?:^|/)``-anchored and it never opens a file, so a "
        "vendoring prefix cannot stop a match. Scoping it to this project would drop a "
        "fork's own skills from the gate."
    ),
    "scripts/hooks/check_snapshot_baseline.py": (
        "The baseline path pattern is ``(?:^|/)``-anchored and no file is opened; the ticket "
        "is resolved by walking UP from the cwd. A fork's own staged baselines must stay in "
        "scope."
    ),
    "src/teatree/hooks/banned_terms_cli.py": (
        "A leak gate over whatever files it is handed; narrowing it to this project would "
        "stop scanning the fork's own tree."
    ),
    "src/teatree/hooks/banned_terms_tree_scan.py": (
        "The whole-tree arm of the same leak gate — scanning the entire work tree is the point of it."
    ),
    "src/teatree/hooks/portable/check_no_silent_skip.py": (
        "Matches ``tests/``-shaped paths through a suffix/segment test, not a root-anchored "
        "prefix, and a consumer repo's whole tree is the intended scope."
    ),
    "src/teatree/hooks/portable/check_pr_body_stray.py": (
        "Refuses a scratch file staged ANYWHERE in the work tree; a project-scoped view would let one land beside it."
    ),
}


def _string_items(node: ast.List) -> list[str]:
    return [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]


def _reads_git_paths(source: str) -> bool:
    """True when *source* builds a git argv whose output names work-tree paths."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        head = node.elts[0]
        if not (isinstance(head, ast.Constant) and head.value == "git"):
            continue
        words = _string_items(node)[1:]
        if any(word in _PATH_PRODUCING or word in _PATH_PRODUCING_FLAGS for word in words):
            return True
    return False


def _hook_modules() -> list[Path]:
    found = [path for root in _SCAN_ROOTS for path in sorted((_CORE_ROOT / root).rglob("*.py"))]
    found.extend(_CORE_ROOT / rel for rel in _SCAN_FILES)
    return found


def _raw_path_reading_modules() -> dict[str, str]:
    """Every scanned module building a raw git argv that names paths, mapped to its source."""
    out: dict[str, str] = {}
    for path in _hook_modules():
        source = path.read_text(encoding="utf-8")
        if _reads_git_paths(source):
            out[path.relative_to(_CORE_ROOT).as_posix()] = source
    return out


class TestRawGitPathReadsAreAccountedFor:
    def test_no_unregistered_hook_reads_git_paths_without_the_helper(self) -> None:
        unaccounted = sorted(
            rel
            for rel, source in _raw_path_reading_modules().items()
            if rel not in WHOLE_WORK_TREE and _HELPER_NAME not in source
        )
        assert not unaccounted, (
            "These hooks build a raw git argv that names work-tree paths, and neither route it "
            f"through `{_HELPER_MODULE}` nor declare themselves whole-work-tree readers: "
            f"{unaccounted}. Run from a vendored project a hook is handed names relative to the "
            "FORK's root, so a project-relative literal never matches and the hook reports clean "
            "having read nothing. Re-root the names, or add the module to WHOLE_WORK_TREE with "
            "the reason the prefix cannot blind it."
        )

    def test_registry_does_not_name_a_module_that_stopped_reading_paths(self) -> None:
        stale = sorted(set(WHOLE_WORK_TREE) - set(_raw_path_reading_modules()))
        assert not stale, f"Registered but no longer reading git-reported paths — drop the entry: {stale}"


class TestTheDetectorCanActuallyFire:
    """A scan with nothing to find is indistinguishable from a broken one."""

    def test_it_flags_a_raw_staged_name_read(self) -> None:
        assert _reads_git_paths('subprocess.run(["git", "diff", "--cached", "--name-only"])')

    def test_it_flags_a_raw_toplevel_read(self) -> None:
        assert _reads_git_paths('subprocess.run(["git", "rev-parse", "--show-toplevel"])')

    def test_it_ignores_a_git_call_that_names_no_path(self) -> None:
        assert not _reads_git_paths('subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"])')

    def test_it_ignores_a_call_routed_through_the_helper(self) -> None:
        assert not _reads_git_paths('tree.run("diff", "--cached", "--relative", "--name-only")')

    def test_the_registry_is_populated_so_the_scan_has_a_subject(self) -> None:
        assert WHOLE_WORK_TREE


class TestWholeWorkTreeEntriesCarryAReason:
    def test_every_entry_states_why_the_prefix_cannot_blind_it(self) -> None:
        empty = sorted(rel for rel, reason in WHOLE_WORK_TREE.items() if len(reason.split()) < 8)
        assert not empty, f"WHOLE_WORK_TREE entries need a real reason, not a placeholder: {empty}"
