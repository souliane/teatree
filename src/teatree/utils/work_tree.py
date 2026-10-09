"""Resolve a hook's file set against the work tree git actually records it in.

The reader half of a two-sided defect. The writer half is a generated doc
written to the correct file and then staged one directory off; this is a check
hook handed a name it cannot resolve back to a file. Both come from the same
disagreement about where the work tree starts.

A prek workspace runs a NESTED project's hooks — any directory carrying its own
``.pre-commit-config.yaml`` — with the cwd set to that project's directory, and
git separately exports ``GIT_DIR`` to a hook fired from a linked worktree. Its
documented rule for a ``GIT_DIR`` given without ``GIT_WORK_TREE``/``core.worktree``
is that the CURRENT DIRECTORY is the top of the work tree. So a check hook run
from a vendored project asks two questions and gets two answers that disagree:
``git rev-parse --show-toplevel`` names the nested directory, while
``git diff --cached --name-only`` still lists the entries of the REAL index,
named from the real top (``vendor/core/src/...``, and the fork's own
``overlay/...`` alongside them).

A hook that joins those two builds ``vendor/core/vendor/core/src/...`` — a path
that exists nowhere — or matches them against a project-relative literal
(``src/<pkg>/``, ``BLUEPRINT.md``) that can never hit. Both shapes end the same
way: the hook examines nothing and exits 0. A gate reporting clean while reading
zero files is worse than no gate, because it manufactures confidence.

Two rules make that impossible here:

- **Drop the overrides, keep the index.** ``GIT_DIR``/``GIT_WORK_TREE`` are
    stripped so git rediscovers the repository from the cwd and answers about the
    real top. ``GIT_INDEX_FILE`` is deliberately kept — it names the index the
    in-progress commit reads, and a partial commit (``git commit --only``) points
    it at a temporary one.
- **Ask git for project-relative names.** ``--relative`` is git's own spelling
    for "exclude changes outside this directory and name what is left relative to
    it", so the prefix arithmetic is git's rather than a caller's string surgery.
    In a plain clone the project IS the top and every method below is an identity.

And a hook that cannot find its files FAILS. :func:`resolve` raises when git
cannot answer, and :meth:`WorkTree.read` raises on a path the staged set named
but the tree does not carry — the state that used to be swallowed as "nothing to
scan". Skipping is how this class of bug stayed invisible; erroring is how the
next recurrence announces itself.
"""

import functools
import os
from dataclasses import dataclass
from pathlib import Path

from teatree.utils.run import run_allowed_to_fail

#: The two variables that decide where git thinks the work tree starts.
_WORK_TREE_OVERRIDES = frozenset({"GIT_DIR", "GIT_WORK_TREE"})

#: Pinned so a repo with ``diff.noprefix``/``diff.mnemonicPrefix`` set still emits
#: the ``a/``/``b/`` headers every diff-parsing consumer strips.
_DIFF_PREFIX_ARGS = ("--src-prefix=a/", "--dst-prefix=b/")


class WorkTreeError(RuntimeError):
    """A hook could not establish, or could not read, the tree it must scan."""


def clean_env() -> dict[str, str]:
    """This process's environment minus the two work-tree overrides.

    ``GIT_INDEX_FILE`` deliberately survives: it names the index the in-progress
    commit reads, and a partial commit (``git commit --only``) points it at a
    temporary one. Any git call a gate makes about the commit being authored
    wants this environment.
    """
    return {key: value for key, value in os.environ.items() if key not in _WORK_TREE_OVERRIDES}


@dataclass(frozen=True)
class WorkTree:
    """A project root together with the real work-tree top that contains it."""

    root: Path
    project: Path

    @property
    def prefix(self) -> str:
        """Where the project starts inside the work tree, as a path prefix.

        ``""`` for a plain clone (the two coincide) and ``"vendor/core/"`` for a
        fork that vendors this project.
        """
        offset = self.project.relative_to(self.root).as_posix()
        return "" if offset == "." else f"{offset}/"

    def run(self, *args: str) -> str:
        """A git query answered about the real work tree, FAIL-LOUD on non-zero.

        ``check=False`` would let a git failure return an empty string, which a
        caller reads as "nothing staged" and exits 0 — the fake-green this whole
        module exists to foreclose.
        """
        result = run_allowed_to_fail(["git", *args], expected_codes=None, cwd=self.project, env=clean_env())
        if result.returncode != 0:
            message = (
                f"`git {' '.join(args)}` failed in {self.project} (exit {result.returncode}): {result.stderr.strip()}"
            )
            raise WorkTreeError(message)
        return result.stdout

    def staged_names(self, *args: str) -> list[str]:
        """The staged paths inside this project, named relative to its root."""
        out = self.run("diff", "--cached", "--relative", "--name-only", *args)
        return [line for line in out.splitlines() if line]

    def staged_diff(self, *args: str) -> str:
        """The staged diff for this project, with project-relative path headers."""
        return self.run("diff", "--cached", "--relative", *_DIFF_PREFIX_ARGS, *args)

    def tracked(self, rel: str) -> str:
        """*rel* as git names it in the index — what ``<ref>:<path>`` needs."""
        return f"{self.prefix}{rel}"

    def blob(self, ref: str, rel: str) -> str | None:
        """``git show <ref>:<rel>``, or ``None`` when that ref does not carry it.

        An EMPTY *ref* reads the INDEX — git's own spelling for "the version
        being committed". ``None`` is a real answer here (an untracked or
        newly-added path), not a swallowed failure.
        """
        result = run_allowed_to_fail(
            ["git", "show", f"{ref}:{self.tracked(rel)}"],
            expected_codes=None,
            cwd=self.project,
            env=clean_env(),
        )
        return result.stdout if result.returncode == 0 else None

    def staged_text(self, rel: str) -> str:
        """The INDEX blob for *rel* — the bytes this commit will carry — FAIL-LOUD.

        The index is the right source for a commit gate: an unstaged edit belongs
        to no commit, and a path the staged set just named always has a blob. A
        missing one therefore means the names and the tree disagree, so it raises
        rather than degrading to "nothing to scan".
        """
        blob = self.blob("", rel)
        if blob is None:
            message = f"staged path {rel!r} has no index blob under {self.project} — names and work tree disagree"
            raise WorkTreeError(message)
        return blob

    def read(self, rel: str) -> str:
        """The working-tree text of *rel*, raising when the tree does not carry it.

        A staged path that does not resolve means the caller's names and its root
        disagree — exactly the re-rooting this module repairs. Raising turns that
        into a visible failure instead of a scan of nothing.
        """
        target = self.project / rel
        try:
            return target.read_text(encoding="utf-8")
        except OSError as exc:
            message = f"staged path {rel!r} does not resolve under {self.project} ({exc})"
            raise WorkTreeError(message) from exc


def resolve(project: Path) -> WorkTree:
    """The :class:`WorkTree` *project* sits in, raising when git cannot say.

    *project* is the directory the hook considers its own root: the source root
    for a hook shipped with this project, and the process cwd for a portable hook
    a consumer repo runs (prek runs every project's hooks from its own root).
    """
    try:
        result = run_allowed_to_fail(
            ["git", "rev-parse", "--show-toplevel"],
            expected_codes=None,
            cwd=project,
            env=clean_env(),
        )
    except OSError as exc:
        message = f"git could not be run in {project} ({exc})"
        raise WorkTreeError(message) from exc
    toplevel = result.stdout.strip()
    if result.returncode != 0 or not toplevel:
        message = f"no git work tree contains {project} (exit {result.returncode}): {result.stderr.strip()}"
        raise WorkTreeError(message)
    root = Path(toplevel).resolve()
    resolved = project.resolve()
    if not resolved.is_relative_to(root):
        message = f"{resolved} is not inside the work tree git reports ({root})"
        raise WorkTreeError(message)
    return WorkTree(root=root, project=resolved)


@functools.cache
def _tree_for(cwd: str) -> WorkTree:
    return resolve(Path(cwd))


def for_cwd() -> WorkTree:
    """The work tree the CURRENT directory sits in, memoised per directory.

    The anchor a PORTABLE hook wants: prek runs every project's hooks from that
    project's own root, so the cwd IS the project whose names must be re-rooted.
    (A hook shipped with this project anchors on its own script location instead,
    because an installed copy is a different checkout than the one being
    committed — those call :func:`resolve` directly.)
    """
    return _tree_for(str(Path.cwd()))


def reset_cwd_cache() -> None:
    """Drop the cwd → work-tree memo. Test-only.

    The memo answers a question about the FILESYSTEM, and a real hook run — one
    process, one cwd, one ``git rev-parse`` — cannot outlive its answer. A test
    process runs many such runs in one interpreter and creates and destroys git
    repositories between them, so an entry kept across tests answers a later one
    about a tree that no longer has that shape. ``tests/conftest.py`` clears it
    around every test.
    """
    _tree_for.cache_clear()
