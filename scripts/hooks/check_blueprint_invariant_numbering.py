"""Pre-commit hook: BLUEPRINT.md §17.1 invariant-numbering integrity.

Background (souliane/teatree#836, §17.6 gate family): concurrent PRs each
appended "the next" §17.1 invariant number against a stale base, so two
PRs both added invariant ``6`` (or ``7``) and the merge silently dropped
or duplicated one. This recurred three times in one session (#856/#859,
#859/#863). Memory/vigilance does not catch it — a deterministic gate
evaluated on the merge result does.

The gate parses the numbered invariant list under ``### 17.1 Invariants``
in ``BLUEPRINT.md`` and FAILS when the numbers are not a gapless ``1..N``
sequence with no repeats. It runs at the same pre-commit/prek layer as
the existing ``blueprint-sync`` / ``skill-prose-ban`` checks, so it fires
on whatever tree is being committed — including the merge-result tree.

CI mode (``--ci --base-ref <ref>``, souliane/teatree#1288): the
pre-commit hook is tree-local — two concurrent PRs each appending the
same next §17.N pass locally and pass each PR's CI. Only the merge
result loses one. The CI mode reads BLUEPRINT.md at ``base-ref`` (the
target branch tip), parses both sides' §17.1 numbers, and fails when
the PR introduces a number that already exists on the base. Skips when
BLUEPRINT.md is unchanged between base and HEAD to keep CI cost down.
"""

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# The list lives under "### 17.1 Invariants" and ends at the next "### "
# heading (the next subsection — e.g. "### 17.2 The flywheel"). Each
# invariant is a top-level numbered list item: ``N. **Title.**``.
_SECTION_HEADING_RE = re.compile(r"^###\s+17\.1\s+Invariants\s*$")
_NEXT_SUBSECTION_RE = re.compile(r"^###\s")
_INVARIANT_ITEM_RE = re.compile(r"^(\d+)\.\s+\*\*")


@dataclass(frozen=True)
class NumberingResult:
    numbers: list[int]
    ok: bool
    reason: str


@dataclass(frozen=True)
class CrossPrResult:
    ok: bool
    reason: str
    colliding: list[int]


def _blueprint_path() -> Path:
    return Path(__file__).resolve().parents[2] / "BLUEPRINT.md"


def extract_invariant_numbers(blueprint_text: str) -> list[int]:
    """Return the ordered list of §17.1 numbered-invariant markers.

    Reads only the block between the ``### 17.1 Invariants`` heading and
    the next ``### `` subsection heading, so numbered lists elsewhere in
    the document do not contaminate the check.
    """
    numbers: list[int] = []
    in_section = False
    for line in blueprint_text.splitlines():
        if not in_section:
            if _SECTION_HEADING_RE.match(line):
                in_section = True
            continue
        if _NEXT_SUBSECTION_RE.match(line):
            break
        match = _INVARIANT_ITEM_RE.match(line)
        if match is not None:
            numbers.append(int(match.group(1)))
    return numbers


def check_numbering(numbers: list[int]) -> NumberingResult:
    """Validate that *numbers* is a gapless ``1..N`` sequence, no repeats."""
    if not numbers:
        return NumberingResult(
            numbers=numbers,
            ok=False,
            reason="No numbered invariants found under '### 17.1 Invariants'.",
        )

    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        return NumberingResult(
            numbers=numbers,
            ok=False,
            reason=(
                f"Duplicate invariant number(s) {duplicates} in §17.1 "
                f"(parsed sequence: {numbers}). Two concurrent PRs likely "
                "appended the same next number against a stale base — "
                "renumber so the list is a gapless 1..N with no repeats."
            ),
        )

    expected = list(range(1, len(numbers) + 1))
    if numbers != expected:
        return NumberingResult(
            numbers=numbers,
            ok=False,
            reason=(
                f"§17.1 invariants are not contiguous: parsed {numbers}, "
                f"expected {expected}. A merge silently dropped or "
                "reordered an invariant — renumber to a gapless 1..N."
            ),
        )

    return NumberingResult(numbers=numbers, ok=True, reason="")


def check_numbering_against_base(
    *,
    base: list[int],
    pr: list[int],
    merge_base: list[int] | None = None,
) -> CrossPrResult:
    """Detect cross-PR §17.1 collisions against a merge-base snapshot.

    Both sides may individually be a clean ``1..N``; the collision class
    is when the PR appends a number that the base also added since the
    PR's branch-point (a concurrently merged PR).

    Three references frame it:

    - *merge_base*: the common ancestor's §17.1 sequence (where the PR
        branched). Anything not in *merge_base* was added on one side or
        the other.
    - *base*: the current target branch tip's §17.1 (e.g. ``origin/main``).
        Numbers in *base* but not in *merge_base* are "added on main"
        since the PR diverged (typically by a concurrently merged PR).
    - *pr*: the PR HEAD's §17.1. Numbers in *pr* but not in *merge_base*
        are "added by this PR".

    Any number added by *both* the PR and the base independently is a
    collision: the merge result will lose one of them. When
    *merge_base* is ``None`` (CLI invocation that did not compute it),
    a conservative fallback uses the longest common ``1..k`` prefix as
    a proxy.
    """
    if merge_base is not None:
        mb_set = set(merge_base)
        base_new = set(base) - mb_set
        pr_new = set(pr) - mb_set
    else:
        common_prefix = 0
        for left, right in zip(base, pr, strict=False):
            if left == right:
                common_prefix += 1
            else:
                break
        base_new = set(base[common_prefix:])
        pr_new = set(pr[common_prefix:])
        if base[common_prefix:] == pr[common_prefix:]:
            # PR didn't touch §17.1; its tail mirrors base's tail.
            return CrossPrResult(ok=True, reason="", colliding=[])

    colliding = sorted(base_new & pr_new)
    if not colliding:
        return CrossPrResult(ok=True, reason="", colliding=[])

    return CrossPrResult(
        ok=False,
        reason=(
            f"§17.1 cross-PR collision on invariant number(s) {colliding}: "
            f"PR sequence {pr} adds {sorted(pr_new)} but base (main) "
            f"already advertises {sorted(base_new)} from a concurrently "
            "merged PR. Rebase on main and renumber the PR's new "
            "invariant(s) to the next free slot."
        ),
        colliding=colliding,
    )


def _blueprint_in_commit() -> bool:
    """True when BLUEPRINT.md is part of the staged change.

    The numbering invariant only needs re-checking when the file (or its
    merge result) is actually being committed; an unrelated commit must
    not be blocked by pre-existing drift it did not introduce.
    """
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "BLUEPRINT.md" in result.stdout.splitlines()


def _git_show(repo: Path, ref: str, path: str) -> str | None:
    """Return ``git show <ref>:<path>`` content, or ``None`` if absent."""
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _merge_base(repo: Path, base_ref: str) -> str | None:
    """Return the merge-base SHA between *base_ref* and HEAD, or None."""
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", base_ref, "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _blueprint_in_pr(repo: Path, base_ref: str) -> bool:
    """True when BLUEPRINT.md differs between *base_ref* and HEAD."""
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", f"{base_ref}...HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "BLUEPRINT.md" in result.stdout.splitlines()


def main() -> int:
    if not _blueprint_in_commit():
        return 0

    path = _blueprint_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # Missing/unreadable BLUEPRINT — nothing this gate can assert;
        # fail open rather than block on a broken tree.
        return 0

    return _report_tree_local(text)


def _report_tree_local(blueprint_text: str) -> int:
    result = check_numbering(extract_invariant_numbers(blueprint_text))
    if result.ok:
        return 0
    print()
    print("  BLUEPRINT.md §17.1 invariant-numbering integrity FAILED:")
    print()
    print(f"    {result.reason}")
    print()
    print("    Recurring collision class (#836 §17.6 gate 1): concurrent")
    print("    PRs append the same 'next' invariant number against a stale")
    print("    base. Renumber §17.1 to a gapless 1..N before committing.")
    print()
    return 1


def _head_blueprint_in_scope(repo: Path, base_ref: str) -> str | None:
    """HEAD's BLUEPRINT.md text when the PR changes it, else None (no-op).

    None means the gate has nothing to check: BLUEPRINT.md is unchanged between
    *base_ref* and HEAD, or the PR deleted it (out of scope for this gate).
    """
    if not _blueprint_in_pr(repo, base_ref):
        return None
    try:
        return (repo / "BLUEPRINT.md").read_text(encoding="utf-8")
    except OSError:
        return None


def _resolve_merge_base_numbers(repo: Path, base_ref: str) -> list[int] | None:
    """The §17.1 sequence at the merge-base, or None when it cannot be read.

    FAIL-CLOSED contract: a None return means the cross-PR check CANNOT run —
    either ``git merge-base`` returned nothing or BLUEPRINT.md did not exist at
    the merge-base (file introduced since the branch-point). The old code
    delegated that to the approximate common-prefix fallback in
    ``check_numbering_against_base``, which returns ok=True for the canonical
    cross-PR collision ([1..N] on both sides) — a silent pass of the exact case
    this gate exists to catch. The caller treats None as a hard failure. With
    fetch-depth: 0 the merge-base resolves on the happy path, so this only fires
    on a genuinely broken base ref / fetch or a brand-new BLUEPRINT.
    """
    merge_base_sha = _merge_base(repo, base_ref)
    if merge_base_sha is not None:
        mb_text = _git_show(repo, merge_base_sha, "BLUEPRINT.md")
        if mb_text is not None:
            return extract_invariant_numbers(mb_text)

    print()
    print("  BLUEPRINT.md §17.1 cross-PR check FAILED: cannot read the merge-base snapshot.")
    print()
    if merge_base_sha is None:
        print(f"    `git merge-base {base_ref} HEAD` returned no commit while BLUEPRINT.md")
        print("    is in the PR diff.")
    else:
        print("    BLUEPRINT.md is not present at the merge-base commit while it IS in the")
        print("    PR diff (the file was introduced since the branch-point).")
    print("    The cross-PR numbering check cannot run without it, so the gate fails")
    print("    CLOSED rather than approximate-pass. Ensure the base ref is fetched (CI")
    print("    uses fetch-depth: 0) and re-run.")
    print()
    return None


def ci_main(*, repo: Path, base_ref: str) -> int:
    """Cross-PR invariant-numbering check, evaluated against *base_ref*.

    Reads ``BLUEPRINT.md`` at HEAD (the PR tip) and at *base_ref* (the
    target branch tip — typically ``origin/main``). If BLUEPRINT.md is
    unchanged between the two, no-op (keep CI cost down per the
    BLUEPRINT-touching diff scope). Otherwise:

    1. Apply the existing ``check_numbering`` to the HEAD tree — a
        PR-side gap or duplicate is still a fail.
    2. Apply ``check_numbering_against_base`` — a §17.N introduced on
        both sides independently is the cross-PR collision.

    Returns ``0`` on pass, ``1`` on fail. Prints actionable diagnostics
    to stdout for the CI log.
    """
    head_text = _head_blueprint_in_scope(repo, base_ref)
    if head_text is None:
        # BLUEPRINT.md unchanged in the PR, or deleted by it — out of scope.
        return 0

    base_text = _git_show(repo, base_ref, "BLUEPRINT.md")
    if base_text is None:
        # Base ref doesn't have BLUEPRINT.md yet (first introduction);
        # nothing to compare against. Defer to the tree-local check.
        return _report_tree_local(head_text)

    head_numbers = extract_invariant_numbers(head_text)
    base_numbers = extract_invariant_numbers(base_text)

    # The PR-tree gap/duplicate check is unconditional — a local 1..N defect is
    # a real fail regardless of the merge-base.
    tree_local = check_numbering(head_numbers)
    if not tree_local.ok:
        print()
        print("  BLUEPRINT.md §17.1 invariant-numbering integrity FAILED (PR tree):")
        print()
        print(f"    {tree_local.reason}")
        print()
        return 1

    merge_base_numbers = _resolve_merge_base_numbers(repo, base_ref)
    if merge_base_numbers is None:
        # FAIL-CLOSED: see _resolve_merge_base_numbers for why a missing snapshot
        # reds the gate rather than delegating to the approximate fallback.
        return 1

    cross_pr = check_numbering_against_base(
        base=base_numbers,
        pr=head_numbers,
        merge_base=merge_base_numbers,
    )
    if not cross_pr.ok:
        print()
        print("  BLUEPRINT.md §17.1 cross-PR invariant-numbering integrity FAILED:")
        print()
        print(f"    {cross_pr.reason}")
        print()
        print("    Recurring collision class (#836 §17.6 gate 1 / #1288): the")
        print("    pre-commit hook is tree-local and cannot see concurrently")
        print("    merged PRs. Rebase the branch on the base ref and renumber")
        print("    the PR's new invariant(s).")
        print()
        return 1

    return 0


def _cli_entry(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Run the cross-PR (merge-base) check, not the staged-tree check.",
    )
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Base ref to diff against in --ci mode (default: origin/main).",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Repository root (default: cwd).",
    )
    args = parser.parse_args(argv)

    if args.ci:
        return ci_main(repo=Path(args.repo).resolve(), base_ref=args.base_ref)
    return main()


if __name__ == "__main__":
    raise SystemExit(_cli_entry(sys.argv[1:]))
