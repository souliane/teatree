"""Detect a declared skill PIN that its own source has moved past.

:mod:`teatree.provisioning.skill_drift` answers one staleness question — did the
bytes INSTALLED here leave the reviewed source. This module answers the other
one, which nothing else asks: did the PIN leave the source. A manifest entry
names a skill at an exact commit (``<owner>/<repo>/<subpath>#<sha>``), so a
perfectly fresh install at a months-old pin is clean by every existing measure
while work merged after that commit reaches no consumer at all.

The comparison is deliberately shaped like its sibling:

*   **read from the declaration surface** — the pins come from
    :func:`teatree.provisioning.declared.skills_declared_in_apm_manifest`, never a
    list kept here, so a mandate added later is measured with no change to this
    module;
*   **one remote read, no clone** — ``git ls-remote --symref <url> HEAD`` returns
    the source's default branch and its tip in a single round trip, which is the
    whole question; fetching history to count the commits between would cost a
    transfer to decorate an answer already known;
*   **fail-loud on "cannot tell"** — no network, no auth, a rate limit or an
    unresolvable ref makes the pin UNMEASURABLE, never current, because "the pin
    is at head" and "I could not reach the source" are different answers.

Nothing here gates. A pin may be held deliberately — an operator pinning away
from head is exercising the point of pinning — so the output is a suggestion
carrying the sha to move to, and the rendering lives here rather than in one
caller because both the measuring surface and the reporting surface must say it
in the same voice.
"""

import dataclasses
import datetime as dt
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from teatree.provisioning.declared import DeclaredDependency, skill_bump_remediation
from teatree.provisioning.skill_source import parse_skill_source
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

#: Where an ``<owner>/<repo>`` spec is fetched from, matching the installer's own base.
DEFAULT_REMOTE_BASE = "https://github.com/"
#: How long a recorded measurement is treated as evidence about the present.
MEASUREMENT_HORIZON = dt.timedelta(days=14)

_LS_REMOTE_TIMEOUT_SECONDS = 30
_SHORT_SHA = 12
_HEAD = "HEAD"
_BRANCH_PREFIX = "refs/heads/"

#: What an IMMUTABLE pin looks like. Anything else (``main``, ``v5.0.7``, a branch) is
#: SYMBOLIC: it resolves to a moving target, so it cannot be "behind" its own source.
#: Seven is git's own minimum abbreviation, so a hand-shortened pin still reads as a sha.
_SHA_RE = re.compile(r"[0-9a-f]{7,40}")


def _short(sha: str) -> str:
    return sha[:_SHORT_SHA] if sha else "(none)"


@dataclass(frozen=True, slots=True)
class RemoteHead:
    """Where a source's default branch points right now, or why that is unknown."""

    sha: str = ""
    branch: str = ""
    unreachable: str = ""


def read_remote_head(url: str) -> RemoteHead:
    """The default branch and its tip at *url*, from one ``git ls-remote``.

    Terminal prompting is disabled: a source needing credentials must come back
    as unreachable rather than blocking a setup run on a password prompt that no
    unattended caller can answer.
    """
    try:
        result = run_allowed_to_fail(
            ["git", "ls-remote", "--symref", url, _HEAD],
            expected_codes=None,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=_LS_REMOTE_TIMEOUT_SECONDS,
        )
    except TimeoutExpired:
        return RemoteHead(unreachable=f"{url} did not answer within {_LS_REMOTE_TIMEOUT_SECONDS}s")
    except OSError as exc:
        return RemoteHead(unreachable=f"{url} could not be read: {exc}")
    if result.returncode != 0:
        detail = next((line.strip() for line in reversed(result.stderr.splitlines()) if line.strip()), "")
        return RemoteHead(unreachable=f"{url} is unreachable: {detail or f'git exited {result.returncode}'}")

    branch, sha = "", ""
    for line in result.stdout.splitlines():
        left, _, right = line.partition("\t")
        if right.strip() != _HEAD:
            continue
        if left.startswith("ref: "):
            branch = left.removeprefix("ref: ").strip().removeprefix(_BRANCH_PREFIX)
        else:
            sha = left.strip()
    if not sha:
        return RemoteHead(branch=branch, unreachable=f"{url} reports no {_HEAD} commit")
    return RemoteHead(sha=sha, branch=branch)


@dataclass(frozen=True, slots=True)
class SkillPinStatus:
    """One declared pin measured against the source it names.

    ``is_current`` and ``is_behind`` are BOTH false when the source could not be
    read: an unmeasurable pin belongs to neither answer, and a reader that folded
    it into one of them would report a guess as a measurement.

    A pin is one of two KINDS and the distinction decides the whole question. An
    IMMUTABLE pin names a commit (``#<sha>``, full or abbreviated) and can genuinely
    fall behind. A SYMBOLIC pin names a moving ref (``#main``, a tag, a branch) and
    resolves to whatever that ref points at TODAY — so it is current by construction
    and has no bump to suggest. Comparing the two spellings as strings made every
    symbolic pin permanently "behind" and produced a remediation telling the operator
    to replace their deliberately floating ref with a frozen sha — advice that
    reverses the choice the declaration was making.
    """

    name: str
    spec: str
    pinned_ref: str
    head_sha: str = ""
    branch: str = ""
    unmeasurable: str = ""

    @property
    def is_symbolic(self) -> bool:
        """Whether the pin names a moving ref rather than a commit."""
        return not _SHA_RE.fullmatch(self.pinned_ref.strip().lower())

    @property
    def _measured(self) -> bool:
        return not self.unmeasurable and bool(self.head_sha)

    @property
    def _resolves_to_head(self) -> bool:
        """An abbreviated pin matches by PREFIX — ``#d0008a3`` names the same commit as its full sha."""
        return self.head_sha.lower().startswith(self.pinned_ref.strip().lower())

    @property
    def tracks_default_branch(self) -> bool:
        """A symbolic pin naming the very branch ``git ls-remote --symref HEAD`` just resolved.

        The only symbolic pin this measurement actually proves anything about. The probe
        reads ``HEAD``, so a pin naming some OTHER branch or a tag was never compared to
        anything — calling that current would be the silent pass this module exists to
        remove, so it reports UNVERIFIED instead.
        """
        return self.is_symbolic and bool(self.branch) and self.pinned_ref.strip() == self.branch

    @property
    def is_current(self) -> bool:
        return self._measured and (self.tracks_default_branch or (not self.is_symbolic and self._resolves_to_head))

    @property
    def is_behind(self) -> bool:
        return self._measured and not self.is_symbolic and not self._resolves_to_head

    @property
    def is_unverified_ref(self) -> bool:
        """Measured, symbolic, and naming something other than the branch that was read."""
        return self._measured and self.is_symbolic and not self.tracks_default_branch

    @property
    def source_repo(self) -> str:
        """The ``<owner>/<repo>`` the spec names, for the finding's evidence."""
        return "/".join(self.spec.partition("#")[0].strip("/").split("/")[:2])

    @property
    def bumped_spec(self) -> str:
        """The declaration's own spec with the source's current head substituted."""
        return f"{self.spec.partition('#')[0]}#{self.head_sha}"


def measure_skill_pins(
    declared: Sequence[DeclaredDependency],
    *,
    remote_base: str = DEFAULT_REMOTE_BASE,
) -> list[SkillPinStatus]:
    """Compare every PINNED skill in *declared* against its source's current head.

    An entry carrying no ``#<ref>`` is skipped rather than reported: it declares
    no pin, so it already tracks whatever the source publishes and there is
    nothing to bump. One remote read per repo, not per skill — several skills
    commonly share a source.
    """
    heads: dict[str, RemoteHead] = {}
    statuses: list[SkillPinStatus] = []
    for dependency in declared:
        if dependency.kind != "skill" or not dependency.source:
            continue
        source = parse_skill_source(dependency.source)
        if source is None:
            statuses.append(
                SkillPinStatus(
                    name=dependency.name,
                    spec=dependency.source,
                    pinned_ref="",
                    unmeasurable=f"{dependency.source!r} names no fetchable source repo",
                )
            )
            continue
        if not source.ref:
            continue
        url = source.remote_url(remote_base)
        if url not in heads:
            heads[url] = read_remote_head(url)
        head = heads[url]
        statuses.append(
            SkillPinStatus(
                name=dependency.name,
                spec=dependency.source,
                pinned_ref=source.ref,
                head_sha=head.sha,
                branch=head.branch,
                unmeasurable=head.unreachable,
            )
        )
    return statuses


def pin_advisory_lines(statuses: Sequence[SkillPinStatus]) -> list[str]:
    """The operator-facing lines a measurement produces — empty when every pin is current.

    Shared by the surface that measures and the surface that reports so the
    suggestion reads identically in both. A moved pin is INFO (a suggestion,
    never a gate); a pin that could not be compared is WARN, because silence
    there would be the very failure this check exists to remove.

    A SYMBOLIC pin that tracks the source's default branch is silent: it resolves to
    whatever that branch points at, so it cannot trail it and there is nothing to
    suggest. Proposing a bump there — which a plain string comparison did, forever —
    told the operator to freeze the floating ref they deliberately chose.
    """
    lines: list[str] = []
    for status in statuses:
        if status.unmeasurable:
            lines.append(
                f"WARN  Skill pin {status.name!r} is UNVERIFIED: {status.unmeasurable}. Whether "
                f"{status.spec} has fallen behind its source is UNKNOWN, which is not the same answer as current."
            )
        elif status.is_unverified_ref:
            lines.append(
                f"WARN  Skill pin {status.name!r} is UNVERIFIED: {status.spec} names the ref "
                f"{status.pinned_ref!r}, but {status.source_repo} was only read at its default branch "
                f"({status.branch or _HEAD}). The two were never compared, which is not the same answer as current."
            )
        elif status.is_behind:
            lines.append(
                f"INFO  Skill pin {status.name!r} trails its source: pinned at {_short(status.pinned_ref)}, "
                f"{status.source_repo} {status.branch or _HEAD} is at {_short(status.head_sha)}. "
                f"Bump: {skill_bump_remediation(status.bumped_spec)}. A pin may be held deliberately, "
                f"so this is a suggestion and gates nothing."
            )
    return lines


@dataclass(frozen=True, slots=True)
class PinAudit:
    """A completed measurement, and WHEN it was taken.

    The timestamp is half the record. A pin comparison is evidence about the
    moment it ran, so a reader that could not see the age would present a
    months-old "current" as today's answer — the shape of clean report this
    module exists to stop.
    """

    measured_at: dt.datetime
    statuses: tuple[SkillPinStatus, ...] = ()

    def age(self, *, now: dt.datetime) -> dt.timedelta:
        return now - self.measured_at

    def is_fresh(self, *, now: dt.datetime, horizon: dt.timedelta = MEASUREMENT_HORIZON) -> bool:
        return self.age(now=now) <= horizon


def default_record_path() -> Path:
    """Where this box keeps the last pin measurement for offline readers."""
    from teatree.paths import get_data_dir  # noqa: PLC0415 — deferred: keeps the import graph off teatree.paths

    return get_data_dir("skill-pins") / "audit.json"


def write_pin_audit(audit: PinAudit, path: Path) -> bool:
    """Record *audit* at *path*; ``False`` when it could not be written.

    A failed write must not be silent to the caller: a reader that finds no
    record reports the measurement as never taken, which is true, but the
    surface that just took one is the only place that can say why it was lost.
    """
    payload = {
        "measured_at": audit.measured_at.isoformat(),
        "statuses": [dataclasses.asdict(status) for status in audit.statuses],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        return False
    return True


def read_pin_audit(path: Path) -> PinAudit | None:
    """The recorded measurement at *path*, or ``None`` when there is none to read.

    ``None`` means "no measurement this reader can trust" for every reason at
    once — absent, unreadable, or written by a shape this code does not know.
    Callers render that as unverified; none of them may render it as agreement.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        measured_at = dt.datetime.fromisoformat(data["measured_at"])
        statuses = tuple(SkillPinStatus(**row) for row in data["statuses"])
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError):
        return None
    if measured_at.tzinfo is None:
        measured_at = measured_at.replace(tzinfo=dt.UTC)
    return PinAudit(measured_at=measured_at, statuses=statuses)
