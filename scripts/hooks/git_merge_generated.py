"""Git merge driver for generated docs — resolve a conflict by REGENERATING.

Registered via ``.gitattributes`` (``<path> merge=generated``) plus a per-clone
``git config merge.generated.driver`` (wired by ``t3 setup`` and worktree
provisioning). When two branches both touch a generated doc — the CLI reference,
the antipattern catalog, the management-commands list — a textual 3-way merge
leaves ``<<<<<<<`` / ``=======`` / ``>>>>>>>`` markers that a human then resolves
by re-running the generator. This driver does that automatically: it discards
both sides and runs the file's generator against the merged working tree,
writing the regenerated content as the merge result — so a CLI-touching PR never
collides on ``docs/generated/cli-reference.md`` again, and a merge-queue rebase
that re-triggers the same collision self-resolves.

Git invokes it as ``driver %O %A %B %P``:

- ``%O``  base (ancestor) version            — unused; the result is regenerated
- ``%A``  ours / OUTPUT slot                 — the driver MUST leave the result here
- ``%B``  theirs version                     — unused
- ``%P``  the real pathname in the worktree  — selects which generator to run

A path with a registered generator is regenerated into ``%A``. A path listed for
the driver but with NO generator (a hand-maintained doc such as ``evals/README.md``)
falls back to keeping the local (ours) version already sitting in ``%A`` — the CI
sync gates (``check_cli_reference_sync``, ``tests/eval_replay/test_readme_sync.py``)
stay the loud backstop for any genuine drift the keep-ours fallback would hide.

See souliane/teatree#3582.
"""

import subprocess
import sys

_EXPECTED_ARGC = 4

# Repo-relative pathname (forward slashes) -> the generator argv that rebuilds it,
# WITHOUT the trailing output path (the driver appends the ``%A`` slot). Each of
# these generators accepts an explicit output path as its first argument and skips
# git-staging when that path is not its committed default — exactly what a merge
# driver needs. ``None`` marks a path that is deliberately driven but has no
# generator: resolve it by keeping ours (see module docstring).
_GENERATORS: dict[str, list[str] | None] = {
    "docs/generated/cli-reference.md": ["scripts/hooks/generate_cli_reference.py"],
    "docs/generated/antipattern-catalog.md": ["scripts/hooks/generate_antipattern_catalog.py"],
    "docs/generated/management-commands.md": ["scripts/hooks/generate_management_commands_doc.py"],
    "evals/README.md": None,
}


def _regenerate(generator_argv: list[str], output_path: str) -> bool:
    """Run *generator_argv* writing to *output_path*; return whether it succeeded.

    Uses the same interpreter the driver runs under (``uv run python`` supplies
    the venv python with teatree + Django installed), so no nested ``uv run`` is
    needed and the generator's ``django.setup()`` resolves.
    """
    result = subprocess.run(
        [sys.executable, *generator_argv, output_path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < _EXPECTED_ARGC:
        sys.stderr.write("git_merge_generated: expected `%O %A %B %P` arguments\n")
        return 2
    _base, ours_output, _theirs, pathname = args[0], args[1], args[2], args[3]
    norm = pathname.replace("\\", "/")

    # An unknown path (driver matched by a broad attribute) or a keep-ours entry
    # (generator is None): %A already holds ours, so a clean exit resolves to the
    # local version.
    if norm not in _GENERATORS:
        return 0
    generator = _GENERATORS[norm]
    if generator is None:
        return 0

    return 0 if _regenerate(generator, ours_output) else 1


def registered_paths() -> tuple[str, ...]:
    """The generated-doc pathnames this driver knows how to resolve."""
    return tuple(_GENERATORS)


if __name__ == "__main__":
    sys.exit(main())
