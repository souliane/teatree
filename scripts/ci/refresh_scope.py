"""Confine the weekly lock-refresh PR to lock/generated artifacts, and gate its self-merge (#4569).

``scripts/ci/lock_delta.py`` answers "what VERSIONS moved". That is the wrong question when the
diff is not a lockfile bump at all: #4490 arrived carrying 108 ``src/`` and 80 ``tests/``
files pushed onto the refresh branch to make it green, and GitHub-native auto-merge — armed at
creation from a patch-only version verdict — landed every one of them unreviewed. A green PR
with auto-merge armed looks identical either way; only the changed-path set tells them apart.

So this script answers the other half: is every changed path a lock or generated artifact. Both
halves gate the arming, and this half is re-asked on every push to the PR, because trust
established at creation says nothing about what the branch has since become.

Fail-closed in three directions, because each one otherwise reads as a clean bump: an empty
path list (an unreadable diff is not a refresh), a file list whose length disagrees with the
pull request's own ``changed_files`` (a truncated page of in-scope paths hides the escaping
ones behind it), and a ``git diff`` that exits non-zero.

Stdlib only, and no ``teatree`` import: the lock-refresh job runs ``uv lock --upgrade`` and
deliberately no ``uv sync`` — the constraint ``scripts/ci/audit_canary.py`` documents — so
reaching into the package would add a full install to a job that needs none.
"""

import argparse
import dataclasses
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

ALLOWED_FILES = frozenset({"uv.lock", "dist/sbom.json"})
ALLOWED_PREFIXES = ("docs/generated/",)

MARKER = "<!-- lock-refresh-scope -->"

_MAX_NAMED = 20
_ALLOWLIST_PROSE = "`uv.lock`, `dist/sbom.json` and `docs/generated/**`"


@dataclasses.dataclass(frozen=True)
class Verdict:
    in_scope: bool
    escaping: tuple[str, ...]
    total: int
    reason: str


def _allowed(path: str) -> bool:
    return path in ALLOWED_FILES or path.startswith(ALLOWED_PREFIXES)


def escaping(paths: "Iterable[str]") -> tuple[str, ...]:
    """Every changed path the lock/generated allowlist does not cover."""
    return tuple(sorted({path for path in _clean(paths) if not _allowed(path)}))


def _clean(paths: "Iterable[str]") -> tuple[str, ...]:
    return tuple(stripped for stripped in (path.strip() for path in paths) if stripped)


def decide(paths: "Iterable[str]", *, expected_count: int | None = None) -> Verdict:
    """Whether this diff may still self-merge, and the sentence saying why."""
    changed = _clean(paths)
    if expected_count is not None and len(changed) != expected_count:
        return Verdict(
            False,
            (),
            len(changed),
            f"the changed-file list holds {len(changed)} path(s) but the pull request reports "
            f"{expected_count} — a truncated list cannot prove confinement",
        )
    if not changed:
        return Verdict(False, (), 0, "no changed path was read — an unreadable diff is not a lockfile bump")
    outside = escaping(changed)
    if outside:
        return Verdict(
            False,
            outside,
            len(changed),
            f"{len(outside)} of {len(changed)} changed path(s) fall outside {_ALLOWLIST_PROSE}",
        )
    return Verdict(True, (), len(changed), f"all {len(changed)} changed path(s) are lock/generated artifacts")


def in_scope(paths: "Iterable[str]") -> bool:
    """Whether the diff is confined to lock/generated artifacts."""
    return decide(paths).in_scope


def render_hold_comment(verdict: Verdict) -> str:
    """The PR comment naming why auto-merge was withheld, keyed for idempotent re-posting."""
    lines = [
        MARKER,
        "**Auto-merge withheld — this is a lockfile bump in name only.**",
        "",
        (
            f"The weekly refresh self-merges on green only while its diff stays inside {_ALLOWLIST_PROSE}. "
            f"Here, {verdict.reason}."
        ),
    ]
    if verdict.escaping:
        lines += ["", "Paths outside that scope:", ""]
        lines += [f"- `{path}`" for path in verdict.escaping[:_MAX_NAMED]]
        if len(verdict.escaping) > _MAX_NAMED:
            lines.append(f"- …and {len(verdict.escaping) - _MAX_NAMED} more")
    lines += [
        "",
        (
            "This is not the [#4548](https://github.com/souliane/teatree/issues/4548) stall — CI is not the "
            "blocker here, the diff scope is. Review the change and merge it by hand, or move the "
            "non-lockfile work to its own pull request "
            "([#4569](https://github.com/souliane/teatree/issues/4569))."
        ),
    ]
    return "\n".join(lines) + "\n"


def _git_changed_paths(base: str, head: str) -> tuple[str, ...] | None:
    """Paths the head adds over the merge-base, or ``None`` when git could not answer."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"::warning::git diff {base}...{head} failed: {result.stderr.strip()}", file=sys.stderr)
        return None
    return tuple(result.stdout.splitlines())


def _read_listing(source: str) -> tuple[str, ...] | None:
    if source == "-":
        return tuple(sys.stdin.read().splitlines())
    try:
        return tuple(Path(source).read_text(encoding="utf-8").splitlines())
    except OSError as error:
        print(f"::warning::could not read the changed-file listing: {error}", file=sys.stderr)
        return None


def _verdict_from_args(args: argparse.Namespace) -> Verdict:
    if args.paths_from:
        listing = _read_listing(args.paths_from)
        if listing is None:
            return Verdict(False, (), 0, "the changed-file listing could not be read — that is not a lockfile bump")
        return decide(listing, expected_count=args.expected_count)
    changed = _git_changed_paths(args.base, args.head)
    if changed is None:
        return Verdict(False, (), 0, f"`git diff {args.base}...{args.head}` failed — the diff could not be read")
    return decide(changed)


def main(argv: "Sequence[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="", help="base ref, for the git-diff input mode")
    parser.add_argument("--head", default="", help="head ref, for the git-diff input mode")
    parser.add_argument("--paths-from", default="", help="file (or - for stdin) holding one changed path per line")
    parser.add_argument("--expected-count", type=int, help="the pull request's own changed_files, cross-checked")
    parser.add_argument("--comment-out", type=Path, help="where to write the hold comment when scope is refused")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"), help="GITHUB_OUTPUT file")
    parser.add_argument("--fail-on-escape", action="store_true", help="exit non-zero on a refused scope")
    args = parser.parse_args(argv)

    if args.paths_from and (args.base or args.head):
        parser.error("--paths-from and --base/--head are mutually exclusive input modes")
    if args.paths_from and args.expected_count is None:
        parser.error("--paths-from requires --expected-count: a truncated listing must not read as confined")
    if not args.paths_from and not (args.base and args.head):
        parser.error("give either --paths-from with --expected-count, or both --base and --head")

    verdict = _verdict_from_args(args)

    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as handle:
            handle.write(f"in-scope={'true' if verdict.in_scope else 'false'}\n")

    print(f"{verdict.total} changed path(s): {verdict.reason}.")
    for path in verdict.escaping[:_MAX_NAMED]:
        print(f"  outside scope: {path}")

    if verdict.in_scope:
        return 0
    if args.comment_out:
        args.comment_out.write_text(render_hold_comment(verdict), encoding="utf-8")
    print(f"::warning::Auto-merge withheld (#4569): {verdict.reason}.", file=sys.stderr)
    return 1 if args.fail_on_escape else 0


if __name__ == "__main__":
    raise SystemExit(main())
