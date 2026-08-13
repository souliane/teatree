"""A blocking finding must cite a file the PR actually touches (#4251).

A cold reviewer can diff three-dot correctly and still take every RUNTIME
measurement against the wrong tree. Probing the branch checkout in isolation
reads whatever ``main`` did to a file since the branch was cut and reports it
as a regression of the branch: a docs-only PR — zero ``src/`` files changed —
was held at high severity on a ``src/`` behaviour it does not touch, and every
prescription the finding carried was already on ``main``, so applying it would
have turned an ``rc=0`` merge into a conflict. A stale finding is not inert.

The gate is decidable from data the reviewer already has: the PR's own
changed-file set. A finding citing a path outside it may not carry blocking
severity until it has been re-taken against the materialised merge result
(``t3 review merge-tree``).

An UNPROVABLE changed-file set never fires the gate. "Outside the diff",
asserted without having read the diff, is the same unbacked claim the gate
exists to stop — and it is the house rule for every three-valued probe in the
tree (``probe_checkout``'s UNKNOWN never reaps). An empty fetch is UNAVAILABLE,
not a proven no-op diff: a real open PR always changes at least one file, the
rule ``ci_rollup.attach_touched_paths`` already reads, so the two paths cannot
disagree about one diff.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

#: Severity words a blocking finding is written with. Free text in practice —
#: ``Severity`` names ``blocker``/``major`` while reviewers write ``high`` and
#: ``critical`` — so the classifier reads the vocabulary, not one enum.
BLOCKING_SEVERITIES = frozenset({"blocker", "blocking", "critical", "high", "major", "p0", "sev0"})


class FindingLike(Protocol):
    """The two fields the gate reads off a finding — kept structural to avoid a model import."""

    @property
    def severity(self) -> str: ...  # pragma: no cover — Protocol declaration

    @property
    def file(self) -> str: ...  # pragma: no cover — Protocol declaration


def is_blocking_severity(severity: str) -> bool:
    """True iff *severity* names a severity that blocks the merge."""
    return severity.strip().lower() in BLOCKING_SEVERITIES


def normalize_path(value: str) -> str:
    """Canonicalise a cited or forge-reported path down to its repo-relative form.

    Strips a ``file:line`` suffix, the ``./`` prefix, and git's ``a/``/``b/``
    diff prefixes, so the reviewer's citation and the forge's changed-path list
    are compared in one shape.
    """
    path = value.strip().split(":", 1)[0].strip().lstrip("/")
    path = path.removeprefix("./")
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


@dataclass(frozen=True, slots=True)
class ChangedFileSet:
    """The PR's changed-file set, three-valued: known-and-non-empty, or unavailable.

    ``available`` is the whole point: a caller that could not read the diff
    hands back :meth:`unavailable`, and every predicate below then declines to
    judge rather than treating an unread diff as an empty one.
    """

    paths: tuple[str, ...] = ()
    available: bool = False

    @classmethod
    def known(cls, paths: Iterable[str]) -> "ChangedFileSet":
        """The set the forge reported — UNAVAILABLE when empty or sentinel-marked.

        A NUL byte is what makes ``backend_protocols.CHANGED_PATHS_UNAVAILABLE``
        un-collidable with a real forge path, and POSIX forbids one in a filename,
        so a NUL-bearing entry is read here as "this list is not a diff" without
        this pure leaf needing to know the sentinel's spelling.
        """
        raw = list(paths)
        if not raw or any("\x00" in path for path in raw):
            return cls.unavailable()
        return cls(paths=tuple(normalize_path(path) for path in raw), available=True)

    @classmethod
    def unavailable(cls) -> "ChangedFileSet":
        """The diff could not be read to completion — the gate declines to judge."""
        return cls()

    def covers(self, cited: str) -> bool:
        """True iff *cited* names one of the changed paths, matched component-aligned.

        A citation and a forge path can differ by a leading segment (a monorepo
        prefix, a repo-relative form), so a suffix match is accepted — but only
        on a ``/`` boundary, so ``src/other_paths.py`` never matches
        ``src/paths.py``.
        """
        needle = normalize_path(cited)
        if not needle:
            return False
        return any(_same_or_suffix(needle, path) for path in self.paths)


def _same_or_suffix(left: str, right: str) -> bool:
    """True iff one path equals the other or is a ``/``-aligned tail of it."""
    return left == right or left.endswith(f"/{right}") or right.endswith(f"/{left}")


def has_blocking_citation(findings: Iterable[FindingLike]) -> bool:
    """True iff some finding is blocking AND cites a file — the only case the gate can fire.

    The caller's cue for whether the changed-file set is worth a forge read at
    all: a clean verdict, or one carrying only nits and PR-level notes, can
    never trip the gate, so it never pays for the diff.
    """
    return any(is_blocking_severity(finding.severity) and finding.file.strip() for finding in findings)


def out_of_scope_blocking_findings(findings: Iterable[FindingLike], changed: ChangedFileSet) -> tuple[FindingLike, ...]:
    """The blocking findings citing a file *changed* proves the PR does not touch.

    Empty when the changed set is unavailable (nothing is proven), when no
    finding is blocking, and for a PR-level finding that cites no file at all.
    """
    if not changed.available:
        return ()
    return tuple(
        finding
        for finding in findings
        if is_blocking_severity(finding.severity) and finding.file.strip() and not changed.covers(finding.file)
    )


def out_of_scope_refusal(findings: Sequence[FindingLike], changed: ChangedFileSet, *, merge_result_retake: bool) -> str:
    """The refusal for a verdict blocking on code the PR does not touch, or ``""``.

    *merge_result_retake* is the reviewer's attestation that the finding was
    re-taken against the materialised merge result rather than the branch
    checkout alone — the one thing that makes an out-of-diff blocking claim
    admissible, so it clears the refusal.
    """
    if merge_result_retake:
        return ""
    offenders = out_of_scope_blocking_findings(findings, changed)
    if not offenders:
        return ""
    cited = ", ".join(sorted({normalize_path(finding.file) for finding in offenders}))
    return (
        f"{len(offenders)} blocking finding(s) cite a file this PR does not change ({cited}) — the "
        f"PR's changed-file set has {len(changed.paths)} path(s), none of them these. A branch-only "
        f"probe reports what main did to that file since the branch was cut, so the finding cannot "
        f"be true of this PR until it has been re-taken against the MERGE RESULT: extract one with "
        f"`t3 review merge-tree` (a plain directory, never a git worktree) and re-measure there. "
        f"Re-record with the re-take attested (`--merge-result-retake`) if the finding survives, or "
        f"drop it below blocking severity."
    )
