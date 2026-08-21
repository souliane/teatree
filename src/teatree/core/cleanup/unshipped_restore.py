"""Read a captured salvage bundle back into a checkout — the recovery half of the capture.

:mod:`teatree.core.cleanup.unshipped_work` writes the bundles; nothing read one
back, so the first person ever to apply one was an operator recovering real work
by hand. That is how bundles that ``git apply`` rejected as corrupt accumulated
unnoticed (#4435), and it is the worst moment to find out. This module is the
other half, so the restore contract is exercised by the suite instead.
"""

from dataclasses import dataclass, field
from pathlib import Path

from teatree.core.cleanup.unshipped_work import (
    COMMITS_SUFFIX,
    FILES_SUFFIX,
    UNCOMMITTED_SUFFIX,
    UNREADABLE_SUFFIX,
    bundle_path,
)
from teatree.core.models import UnshippedWorkRecord
from teatree.utils import git
from teatree.utils.git_run import git_env_without_overrides, run_with_status

# Commits first: the uncommitted patch is the delta ON TOP of them.
ORDERED_PARTS: tuple[str, ...] = (COMMITS_SUFFIX, UNCOMMITTED_SUFFIX)


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    """What each bundle part did, and why the restore refused when it did."""

    prefix: str
    into: Path
    parts: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines = [f"bundle {self.prefix} -> {self.into}"]
        lines += [f"  {suffix}: {outcome}" for suffix, outcome in self.parts.items()]
        lines += [f"  ERROR: {error}" for error in self.errors]
        return "\n".join(lines)


def resolve_prefix(reference: str) -> str:
    """The bundle prefix for ``reference`` — a recorded checkout path, or a prefix itself.

    Both spellings are what an operator actually holds: ``t3 doctor check``
    names the checkout, the artifact directory names the prefix.
    """
    record = UnshippedWorkRecord.objects.filter(checkout_path=reference).first()
    if record is not None and record.artifact_prefix:
        return record.artifact_prefix
    return reference


def _apply(part: Path, into: Path, *, dry_run: bool) -> str:
    args = ["apply", *(["--check"] if dry_run else []), str(part)]
    result = run_with_status(repo=str(into), args=args, env=git_env_without_overrides())
    if result.returncode == 0:
        return "applies cleanly (nothing written)" if dry_run else "applied"
    return f"FAILED — {result.stderr.strip() or f'git apply exited {result.returncode}'}"


def _has_content(prefix: str, suffix: str) -> bool:
    part = bundle_path(prefix, suffix)
    return part.is_file() and bool(part.stat().st_size)


def _refusal(prefix: str, into: Path) -> str:
    """Why this restore cannot start, or ``""`` when it can.

    An all-empty bundle refuses rather than reporting a restore that moved
    nothing: the capture degrades to the tracked-only delta when the
    untracked-inclusive route is refused, so an untracked-only checkout can
    leave patches holding the filenames and none of the content.
    """
    if not git.is_git_checkout(into):
        return f"{into} is not a git checkout — git apply needs one to restore into"
    if any(_has_content(prefix, suffix) for suffix in ORDERED_PARTS):
        return ""
    marker = bundle_path(prefix, UNREADABLE_SUFFIX)
    if marker.is_file():
        return (
            f"{prefix} captured no content — the checkout was unreadable: {marker.read_text(encoding='utf-8').strip()}"
        )
    if any(bundle_path(prefix, suffix).is_file() for suffix in ORDERED_PARTS):
        manifest = bundle_path(prefix, FILES_SUFFIX)
        return f"{prefix} holds no patch content — {manifest} names what was there"
    return f"no salvage bundle at {prefix}"


def restore_bundle(reference: str, into: Path, *, dry_run: bool = False) -> RestoreOutcome:
    """Apply ``reference``'s bundle into ``into``; each part reported on its own.

    ``git apply`` is all-or-nothing per invocation, so a part that fails leaves
    ``into`` exactly as it was and the other part is still worth attempting —
    the two are independent recoveries and the operator picks between them.
    """
    prefix = resolve_prefix(reference)
    refusal = _refusal(prefix, into)
    if refusal:
        return RestoreOutcome(prefix=prefix, into=into, errors=[refusal])
    parts: dict[str, str] = {}
    for suffix in ORDERED_PARTS:
        if not _has_content(prefix, suffix):
            continue
        parts[suffix] = _apply(bundle_path(prefix, suffix), into, dry_run=dry_run)
    errors = [f"{suffix}: {outcome}" for suffix, outcome in parts.items() if outcome.startswith("FAILED")]
    return RestoreOutcome(prefix=prefix, into=into, parts=parts, errors=errors)


__all__ = ["ORDERED_PARTS", "RestoreOutcome", "resolve_prefix", "restore_bundle"]
