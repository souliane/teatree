"""Detect installed skills whose bytes no longer match the source they came from.

Skills install as dereferenced physical COPIES — one materialised tree under the
shared agent skills root, symlinked from each agent's own directory — never as
symlinks into a clone. That shape is deliberate on two counts: a clone sits on
whatever branch its owner last checked out, so linking would publish half-written
WIP to every agent on the box, and one materialised tree has to back several
agent front doors at once. The cost is that a copy is a SNAPSHOT with no way of
announcing that its source moved on, so a merged fix reaches nobody until each
person separately re-installs — silently, with nothing anywhere reporting the
gap.

This module is that announcement. Per skill it compares the bytes actually
installed against the bytes on the source clone's reviewed ref, and it does so:

*   **offline** — ``git ls-tree`` / ``git grep`` read a ref directly, so no
    fetch, no checkout, and no working-tree state is touched (the clone may sit
    on a dirty feature branch and the answer is unaffected);
*   **without an install marker** — a bookkeeping file records only what ONE
    install path chose to write and says nothing about bytes that arrived by any
    other route, so the comparison reads the installed files themselves;
*   **fail-loud on "cannot tell"** — a missing clone is reported as unmeasurable
    rather than collapsed into agreement, because "the install matches" and "I
    could not check" are different answers.

A tree symlink (mode ``120000``) is skipped: its blob holds a path, not a skill,
and the file it points at is enumerated on its own, which keeps exactly one row
per skill.
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from teatree.utils.run import run_allowed_to_fail

_SKILL_FILE = "SKILL.md"
_SYMLINK_MODE = "120000"
_GIT_TIMEOUT_SECONDS = 60
_FALLBACK_REFS = ("origin/main", "origin/master")


class SkillSourceClone(BaseModel):
    """A local clone whose reviewed ref supplies the skills installed on this box."""

    #: Human name for the source, used in the finding line (e.g. the repo slug).
    label: str = ""
    #: Candidate clone locations, first existing wins — a team clone lives
    #: somewhere different on every box, and one with no clone at all cannot be
    #: measured, which is reported rather than passed off as agreement.
    paths: list[str] = Field(default_factory=list)
    #: The reviewed ref. Empty resolves the clone's own ``origin/HEAD``, then the
    #: conventional fallbacks — never the working tree, which is somebody's WIP.
    ref: str = ""


@dataclass(frozen=True, slots=True)
class SkillDrift:
    """What one source clone says about the skills installed from it.

    *stale* and *absent* are kept apart because they are different failures with
    the same cause: stale bytes mean a merged fix never landed here, absent means
    a whole skill the source publishes was never installed at all — the shape a
    lifecycle stage hits when it dispatches a skill this box does not have.
    """

    label: str
    ref: str = ""
    stale: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    unmeasurable: str = ""

    @property
    def is_clean(self) -> bool:
        return not (self.stale or self.absent or self.unmeasurable)


@dataclass(frozen=True, slots=True)
class PublishedSkills:
    """What a source clone publishes at its reviewed ref — resolved ONCE, shared.

    The drift gate and the installer must agree on three things or they cannot
    converge: which clone is the source, which ref is reviewed, and which skills
    that ref publishes under which install names. Resolving them here, once, is
    what makes "install it" and "it is installed" the same question. A source that
    cannot be resolved carries *unmeasurable* and no names — the gate reports it
    UNVERIFIED and the installer declines, neither guessing.
    """

    label: str
    repo: Path | None = None
    ref: str = ""
    #: ``SKILL.md`` path at *ref* → the install name its front matter declares.
    names: dict[str, str] = field(default_factory=dict)
    unmeasurable: str = ""


def resolve_published_skills(clone: SkillSourceClone) -> PublishedSkills:
    """Locate *clone*, pick its reviewed ref, and read the skills it publishes there."""
    label = clone.label or (clone.paths[0] if clone.paths else "skill source")
    repo = _first_existing_clone(clone.paths)
    if repo is None:
        listed = ", ".join(clone.paths) or "(none declared)"
        return PublishedSkills(label=label, unmeasurable=f"no clone at any declared path ({listed})")
    ref = _resolve_ref(repo, clone.ref)
    if not ref:
        return PublishedSkills(label=label, repo=repo, unmeasurable=f"no reviewed ref resolves in {repo}")
    names = _source_names(repo, ref)
    if not names:
        return PublishedSkills(label=label, repo=repo, ref=ref, unmeasurable=f"{ref} in {repo} lists no {_SKILL_FILE}")
    return PublishedSkills(label=label, repo=repo, ref=ref, names=names)


def _git(repo: Path, *args: str) -> str:
    """Run a read-only git command in *repo*; empty string on any failure."""
    result = run_allowed_to_fail(
        ["git", "-C", str(repo), *args],
        expected_codes=None,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    return result.stdout if result.returncode == 0 else ""


def _first_existing_clone(paths: list[str]) -> Path | None:
    for raw in paths:
        candidate = Path(raw).expanduser()
        if (candidate / ".git").exists():
            return candidate
    return None


def _resolve_ref(repo: Path, declared: str) -> str:
    """The reviewed ref to compare against; empty when none resolves.

    A declared ref wins. Otherwise the clone's own ``origin/HEAD`` (which
    survives a default-branch rename), then the conventional fallbacks.
    """
    candidates = [declared] if declared else []
    head = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip()
    if head:
        candidates.append(head)
    candidates.extend(_FALLBACK_REFS)
    for ref in candidates:
        if _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip():
            return ref
    return ""


def _source_blobs(repo: Path, ref: str) -> dict[str, str]:
    """Every non-symlink path → blob sha at *ref*.

    The whole tree, not the ``SKILL.md`` files alone: a skill installs as its entire
    directory, so a merged fix that lands only in ``references/`` is drift the install
    still has to hear about.
    """
    blobs: dict[str, str] = {}
    for line in _git(repo, "ls-tree", "-r", ref).splitlines():
        meta, _, path = line.partition("\t")
        fields = meta.split()
        expected_fields = 3
        if len(fields) != expected_fields:
            continue
        mode, _kind, sha = fields
        if mode == _SYMLINK_MODE:
            continue
        blobs[path] = sha
    return blobs


def _files_by_skill(blobs: dict[str, str], skill_md_paths: list[str]) -> dict[str, dict[str, str]]:
    """``SKILL.md`` path → that skill's own files, keyed relative to its directory.

    Longest prefix wins, so a skill nested inside another owns its files rather than
    counting as drift in its parent.
    """
    by_prefix = {path: path.removesuffix(_SKILL_FILE) for path in skill_md_paths}
    deepest_first = sorted(by_prefix, key=lambda path: len(by_prefix[path]), reverse=True)
    owned: dict[str, dict[str, str]] = {path: {} for path in skill_md_paths}
    for path, sha in blobs.items():
        for skill_md_path in deepest_first:
            if path.startswith(by_prefix[skill_md_path]):
                owned[skill_md_path][path.removeprefix(by_prefix[skill_md_path])] = sha
                break
    return owned


def _source_names(repo: Path, ref: str) -> dict[str, str]:
    """``SKILL.md`` path → the skill name its front matter declares at *ref*.

    The name, not the directory, is the install identity: a skill can live at
    ``internal/ao-elite-review/`` and install as ``elite-review``. Only the FIRST
    ``name:`` per file counts — a later one belongs to an example inside the body.
    """
    names: dict[str, str] = {}
    for line in _git(repo, "grep", "-n", "^name:", ref, "--", f"*/{_SKILL_FILE}").splitlines():
        # git grep over a ref emits `<ref>:<path>:<lineno>:name: <value>`.
        remainder = line.partition(":")[2]
        path, _, remainder = remainder.partition(":")
        remainder = remainder.partition(":")[2]
        value = remainder.partition(":")[2].strip().strip("\"'")
        if path and value and path not in names:
            names[path] = value
    return names


def _git_blob_sha(path: Path) -> str:
    """The sha a git blob of *path*'s bytes would have; empty when unreadable.

    Comparing hashes rather than contents means the source side needs no blob
    read at all — ``ls-tree`` already carries it.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def _installed_skill_dir(name: str, search_dirs: list[Path]) -> Path | None:
    """The directory an agent would actually load *name* from — first dir wins."""
    for search_dir in search_dirs:
        if (search_dir / name / _SKILL_FILE).is_file():
            return search_dir / name
    return None


def measure_skill_drift(clone: SkillSourceClone, *, search_dirs: list[Path]) -> SkillDrift:
    """Compare every skill *clone* publishes against the copy installed here."""
    published = resolve_published_skills(clone)
    if published.unmeasurable or published.repo is None:
        return SkillDrift(label=published.label, ref=published.ref, unmeasurable=published.unmeasurable)
    label, repo, ref, names = published.label, published.repo, published.ref, published.names

    blobs = _source_blobs(repo, ref)
    owned = _files_by_skill(blobs, list(names))
    stale: list[str] = []
    absent: list[str] = []
    for path, name in sorted(names.items()):
        installed = _installed_skill_dir(name, search_dirs)
        if installed is None:
            absent.append(name)
        elif any(_git_blob_sha(installed / rel) != sha for rel, sha in owned[path].items()):
            stale.append(name)
    return SkillDrift(label=label, ref=ref, stale=tuple(sorted(stale)), absent=tuple(sorted(absent)))
