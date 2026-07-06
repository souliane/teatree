"""Pre-merge migration-fork probe (#995).

The §17.4.3 merge gate certified CI-green PRs that were not actually
mergeable end-to-end: two branches each added a migration on the same
parent, both cleared and merged, and the fork surfaced only when the
second branch's post-merge ``migrate --no-input`` failed with
``Conflicting migrations detected``. The merge-time live-CI re-check
cannot see it — each branch's own graph is linear; the fork exists only
in the *merged* graph.

This is the CLEAR-side sibling of :mod:`teatree.core.worktree.branch_currency`:
it predicts the merged tree of ``reviewed_sha`` + target via
``git merge-tree --write-tree`` (a pure object-DB merge that never
touches the index or worktree), reads every migration file from that
tree, and refuses when any app's migration graph would carry more than
one leaf node — the exact condition under which ``migrate`` refuses.

A failed fetch or an uncomputable merge-tree (old git, bad object) is
inconclusive and fails *open* — the same "if we can verify, refuse; if
we can't, don't block" posture as :mod:`branch_currency` and
:mod:`teatree.core.gates.clone_guard`. The merge-time live-CI re-check
and the post-merge ``migrate`` still backstop correctness.
"""

import ast
from dataclasses import dataclass

from teatree.utils.git import git_env_without_overrides
from teatree.utils.run import run_allowed_to_fail

_MIGRATIONS_SEGMENT = "/migrations/"
_DEPENDENCIES_FIELD = "dependencies"
_DEPENDENCY_PAIR_LEN = 2
_LS_TREE_MIN_FIELDS = 3
_CLEAN_AND_CONFLICT_CODES = frozenset({0, 1})


@dataclass(frozen=True, slots=True)
class MigrationLeafConflict:
    """One forked-migration finding: an app would have >1 leaf post-merge.

    ``leaf_names`` are the migration names (``"0002_branch_a"``) with no
    descendant in the merged graph — two siblings off one parent is the
    #995 fork. ``app_label`` is the Django app the fork is in.
    """

    app_label: str
    leaf_count: int
    leaf_names: tuple[str, ...]


def _git(repo: str, *args: str) -> tuple[int, str]:
    """``(returncode, stdout)`` for ``git -C <repo> <args>`` — never raises.

    Strips inherited ``GIT_*`` env so a call from inside a git hook still
    targets ``repo``, not the ambient one (mirrors :mod:`branch_currency`).
    """
    result = run_allowed_to_fail(
        ["git", "-C", repo, *args],
        expected_codes=None,
        env=git_env_without_overrides(),
    )
    return result.returncode, result.stdout


def _fetch_target(repo: str, target: str) -> bool:
    """Fetch the remote behind ``target``; a failed fetch is an inconclusive skip."""
    remote = target.split("/", 1)[0] if "/" in target else "origin"
    rc, _ = _git(repo, "fetch", remote)
    return rc == 0


def _merged_tree_oid(repo: str, reviewed_sha: str, target: str) -> str | None:
    """The tree oid of merging ``target`` into ``reviewed_sha``, no mutation.

    ``git merge-tree --write-tree`` (git ≥ 2.38) writes the merged tree to
    the object DB and prints its oid on the first line without touching the
    index or worktree. Exit ``0`` ⇒ clean merge (first line is the tree
    oid); ``1`` ⇒ textual conflicts (a content conflict is
    :mod:`branch_currency`'s concern, not this probe's — and the migration
    files themselves are distinct additions that do not text-conflict, so
    the merged tree the first line names still carries both, which is
    exactly the graph we must inspect); any other code is inconclusive and
    returns ``None`` so the caller fails open.
    """
    rc, out = _git(repo, "merge-tree", "--write-tree", reviewed_sha, target)
    if rc not in _CLEAN_AND_CONFLICT_CODES:
        return None
    first_line = out.splitlines()[0].strip() if out.strip() else ""
    return first_line or None


def _migration_blobs(repo: str, tree_oid: str) -> dict[str, str]:
    """Map ``"<app>/<name>"`` → blob oid for every migration file in ``tree_oid``.

    Recursively lists the tree; a path under a ``…/migrations/`` directory
    ending in ``.py`` (excluding ``__init__.py``) is a migration. The app
    label is the directory immediately above ``migrations``.
    """
    rc, out = _git(repo, "ls-tree", "-r", tree_oid)
    if rc != 0:
        return {}
    blobs: dict[str, str] = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        if not path or _MIGRATIONS_SEGMENT not in path or not path.endswith(".py"):
            continue
        name = path.rsplit("/", 1)[-1].removesuffix(".py")
        if name == "__init__":
            continue
        app_dir, _, _ = path.partition(_MIGRATIONS_SEGMENT)
        app_label = app_dir.rsplit("/", 1)[-1]
        parts = meta.split()
        if len(parts) < _LS_TREE_MIN_FIELDS:
            continue
        blobs[f"{app_label}/{name}"] = parts[2]
    return blobs


def _parse_dependencies(source: str) -> list[tuple[str, str]]:
    """Extract the ``Migration.dependencies`` ``(app, name)`` pairs from source.

    Parses the AST rather than importing — the migrated tree's files are not
    importable here and ``ast.literal_eval`` is safe on the list literal.
    A non-literal entry (e.g. ``migrations.swappable_dependency(...)``) is
    skipped: it never names a sibling migration, so it cannot mask a fork.
    """
    try:
        module = ast.parse(source)
    except SyntaxError:
        return []
    pairs: list[tuple[str, str]] = []
    for node in ast.walk(module):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple))):
            continue
        if not any(isinstance(t, ast.Name) and t.id == _DEPENDENCIES_FIELD for t in node.targets):
            continue
        for element in node.value.elts:
            try:
                value = ast.literal_eval(element)
            except (ValueError, SyntaxError):
                continue
            if (
                isinstance(value, tuple)
                and len(value) == _DEPENDENCY_PAIR_LEN
                and all(isinstance(part, str) for part in value)
            ):
                pairs.append((value[0], value[1]))
    return pairs


def _leaves_by_app(repo: str, tree_oid: str) -> dict[str, list[str]]:
    """For each app, the migration names that no other migration depends on.

    A linear graph has exactly one leaf per app; a fork (two siblings off
    one parent) leaves two. Builds the parent set from every file's
    ``dependencies`` and returns ``{app: [leaf names]}`` for apps with at
    least one migration.
    """
    blobs = _migration_blobs(repo, tree_oid)
    if not blobs:
        return {}
    migrations_by_app: dict[str, set[str]] = {}
    parents: set[tuple[str, str]] = set()
    for key, oid in blobs.items():
        app_label, name = key.split("/", 1)
        migrations_by_app.setdefault(app_label, set()).add(name)
        rc, source = _git(repo, "cat-file", "-p", oid)
        if rc != 0:
            continue
        parents.update(_parse_dependencies(source))
    return {
        app_label: sorted(name for name in names if (app_label, name) not in parents)
        for app_label, names in migrations_by_app.items()
    }


def sha_forks_migration_graph(
    repo: str,
    reviewed_sha: str,
    target: str = "origin/main",
) -> MigrationLeafConflict | None:
    """Return a finding only when merging ``reviewed_sha`` onto ``target`` forks the graph.

    The CLEAR-side / pre-merge migration probe (#995): refuses when the
    *merged* migration graph would carry more than one leaf node for any
    app — the exact state under which a post-merge ``migrate --no-input``
    fails with ``Conflicting migrations detected``. Each branch's own
    graph is linear, so this is invisible to per-branch CI; only the
    merged tree exposes it.

    Inconclusive cases (failed fetch, uncomputable merge-tree) return
    ``None`` so the probe fails open — same posture as
    :mod:`branch_currency`. The first forking app is reported (one finding
    per probe is enough to refuse and is the actionable signal).
    """
    if not _fetch_target(repo, target):
        return None
    tree_oid = _merged_tree_oid(repo, reviewed_sha, target)
    if tree_oid is None:
        return None
    for app_label, leaves in sorted(_leaves_by_app(repo, tree_oid).items()):
        if len(leaves) > 1:
            return MigrationLeafConflict(
                app_label=app_label,
                leaf_count=len(leaves),
                leaf_names=tuple(leaves),
            )
    return None
