"""Git merge driver for generated docs — merge like git, and say the doc needs regenerating.

Registered via ``.gitattributes`` (``<path> merge=generated``) plus a per-clone
``git config merge.generated.driver`` (wired by ``t3 setup`` and worktree
provisioning). The generated docs — the CLI reference, the antipattern catalog, the
management-commands list — collide on nearly every CLI-touching PR, and the right
resolution is a re-run of the file's generator.

A merge driver cannot BE that re-run. Git decides every content merge before it writes
any of the merge result to the working tree, so the tree a driver would regenerate from
is still the local (ours) side — measured with a control on a merge whose source files
did not even conflict. A generator run here therefore reproduces ours byte-for-byte,
exits 0, and git records the generated doc as a CLEAN merge holding ours: the other
side's entries are dropped with no conflict, no marker and no warning, leaving the doc
stale against the merged command tree until CI's sync gate goes red. Worse, the outcome
then depends on whether this clone happened to run provisioning — one merge, two
results (souliane/teatree#4259).

So this driver resolves a driven path exactly as an unregistered clone would — git's own
textual 3-way merge, via ``git merge-file`` — and writes a stderr advisory naming the
generator to re-run once the merge is complete. Registration stops being a merge
variable, and the stale-doc risk is loud at merge time instead of red in CI.

Git invokes it as ``driver %O %A %B %P``:

- ``%O``  base (ancestor) version            — the 3-way merge's base
- ``%A``  ours / OUTPUT slot                 — merged in place; git takes the result here
- ``%B``  theirs version                     — the 3-way merge's other side
- ``%P``  the real pathname in the worktree  — selects the advisory's generator

See souliane/teatree#3582 for the registration wiring.
"""

import subprocess
import sys
from pathlib import Path

_EXPECTED_ARGC = 4

# `git merge-file` exits with the number of conflicts it left; anything above this is a
# failure to merge at all (a missing slot, an unreadable file), never a conflict count.
_MAX_CONFLICT_EXIT = 127

# Repo-relative pathname (forward slashes) -> the generator argv that rebuilds it,
# WITHOUT the trailing output path. ``None`` marks a driven path that is hand-maintained
# and has no generator, whose advisory points at its sync gate instead.
_GENERATORS: dict[str, list[str] | None] = {
    "docs/generated/cli-reference.md": ["scripts/hooks/generate_cli_reference.py"],
    "docs/generated/antipattern-catalog.md": ["scripts/hooks/generate_antipattern_catalog.py"],
    "docs/generated/management-commands.md": ["scripts/hooks/generate_management_commands_doc.py"],
    "evals/README.md": None,
}


def _textual_merge(base: str, ours_output: str, theirs: str) -> int:
    """Git's own 3-way merge into *ours_output*; 0 when clean, 1 when not.

    A merge git could not perform at all also returns 1, so the path stays conflicted
    for a human rather than resolving to the untouched ours slot.
    """
    result = subprocess.run(
        ["git", "merge-file", "-L", "ours", "-L", "base", "-L", "theirs", ours_output, base, theirs],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return 0
    if not 0 < result.returncode <= _MAX_CONFLICT_EXIT:
        sys.stderr.write(result.stderr)
    return 1


def teatree_source_root() -> Path:
    """Teatree's own tree root — the driver ships inside it, so it is the anchor."""
    return Path(__file__).resolve().parents[2]


def teatree_relative_path(pathname: str, *, repo_root: Path | str | None = None) -> str:
    """Git's ``%P`` re-expressed relative to teatree's root, so it can hit a generator key.

    ``%P`` is relative to the top of the WORKING TREE; the keys below are relative to
    teatree's own root. A fork that vendors core makes those differ by the vendoring
    prefix, and every generated doc then missed the lookup. A path outside teatree's
    tree (the fork's own docs) is returned untouched.
    """
    norm = pathname.replace("\\", "/")
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    try:
        prefix = teatree_source_root().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return norm
    if prefix in {"", "."}:
        return norm
    return norm.removeprefix(f"{prefix}/") if norm.startswith(f"{prefix}/") else norm


def regeneration_advisory(norm: str) -> str:
    """The stderr line telling the reader *norm* was merged textually, not regenerated.

    Empty for a path this driver knows nothing about — a broad ``merge=generated``
    attribute in a fork, where an advisory naming teatree's generators would mislead.
    """
    if norm not in _GENERATORS:
        return ""
    generator = _GENERATORS[norm]
    action = (
        f"re-run `python {generator[0]}` and commit the result"
        if generator is not None
        else "re-check it against its sync gate"
    )
    return (
        f"git_merge_generated: {norm} was merged TEXTUALLY — a merge driver runs before the "
        f"merged tree exists, so it cannot regenerate. After this merge, {action}.\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < _EXPECTED_ARGC:
        sys.stderr.write("git_merge_generated: expected `%O %A %B %P` arguments\n")
        return 2
    base, ours_output, theirs, pathname = args[0], args[1], args[2], args[3]

    sys.stderr.write(regeneration_advisory(teatree_relative_path(pathname)))
    return _textual_merge(base, ours_output, theirs)


def registered_paths() -> tuple[str, ...]:
    """The generated-doc pathnames this driver carries a regeneration advisory for."""
    return tuple(_GENERATORS)


if __name__ == "__main__":
    sys.exit(main())
