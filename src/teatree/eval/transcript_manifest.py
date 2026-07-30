"""Provenance sidecar for a captured eval transcript.

A transcript graded by :class:`~teatree.eval.backends.TranscriptRunner` is just a
``<scenario>.jsonl`` file on disk. On a 24/7-loop host that is not enough: the
capture step picks the freshest sub-agent JSONL on the machine, so a concurrent
UNRELATED transcript can be copied in as scenario X's, and a transcript recorded
against an OLD ``SKILL.md`` keeps grading green after a regression the new skill
would fail.

The manifest binds a captured transcript to its provenance — the scenario it was
captured for, a hash of the scenario prompt at capture time, the repo HEAD SHA,
and the capture epoch. :func:`verify` re-derives the current expectation and
refuses a transcript whose recorded provenance no longer matches (a scenario
whose prompt drifted, a transcript captured at a different HEAD), so a stale or
cross-contaminated transcript surfaces as a skip rather than a silent pass.

Written ONLY by the capture path (``t3 eval capture-subagent`` →
:func:`teatree.eval.subagent_capture.capture_to`). A hand-placed fixture with no
manifest is graded as before — the manifest is provenance for a genuine capture,
not a requirement on curated harness fixtures.
"""

import dataclasses
import hashlib
import json
import time
from pathlib import Path

from teatree.utils import git
from teatree.utils.run import CommandFailedError

MANIFEST_SUFFIX = ".manifest.json"


def current_head_sha() -> str:
    """The repo HEAD SHA, or ``""`` when not resolvable (not a git repo).

    Django-free (unlike ``persistence.current_git_sha``) so the transcript grade
    path stays importable without the ORM.
    """
    try:
        return git.head_sha()
    except (CommandFailedError, OSError):
        return ""


def manifest_path(transcript: Path) -> Path:
    """The sidecar path for *transcript* — ``<scenario>.jsonl.manifest.json``."""
    return transcript.with_name(transcript.name + MANIFEST_SUFFIX)


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class TranscriptManifest:
    scenario: str
    prompt_sha256: str
    head_sha: str
    captured_at: float
    source: str

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "TranscriptManifest":
        data = json.loads(raw)
        return cls(
            scenario=str(data["scenario"]),
            prompt_sha256=str(data["prompt_sha256"]),
            head_sha=str(data["head_sha"]),
            captured_at=float(data["captured_at"]),
            source=str(data["source"]),
        )


@dataclasses.dataclass(frozen=True)
class ProvenanceResult:
    """The outcome of verifying a transcript's provenance against a scenario.

    ``present`` is False when no manifest sidecar exists (a hand-placed fixture) —
    the caller grades it unverified. ``ok`` is True when a manifest exists and
    every bound field matches; a False ``ok`` with ``present`` carries the
    ``reason`` the transcript is refused.
    """

    present: bool
    ok: bool
    reason: str


def write(transcript: Path, *, scenario: str, prompt: str, head_sha: str, source: Path) -> TranscriptManifest:
    """Write the provenance sidecar for a freshly captured *transcript*."""
    manifest = TranscriptManifest(
        scenario=scenario,
        prompt_sha256=prompt_hash(prompt),
        head_sha=head_sha,
        captured_at=time.time(),
        source=str(source),
    )
    manifest_path(transcript).write_text(manifest.to_json(), encoding="utf-8")
    return manifest


def verify(transcript: Path, *, scenario: str, prompt: str, head_sha: str) -> ProvenanceResult:
    """Check a captured transcript's recorded provenance against the live scenario.

    A missing sidecar means "unverified capture / curated fixture" (``present``
    False) — grade it as before. A present sidecar must match the scenario name,
    the current prompt hash (the scenario's prompt did not drift), and the current
    repo HEAD (the transcript is not stale against a since-changed skill); the
    first mismatch names the reason the transcript is refused.
    """
    sidecar = manifest_path(transcript)
    if not sidecar.is_file():
        return ProvenanceResult(present=False, ok=True, reason="")
    try:
        manifest = TranscriptManifest.from_json(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
        return ProvenanceResult(present=True, ok=False, reason=f"unreadable provenance manifest ({exc})")
    if manifest.scenario != scenario:
        return ProvenanceResult(
            present=True, ok=False, reason=f"manifest scenario {manifest.scenario!r} != {scenario!r}"
        )
    if manifest.prompt_sha256 != prompt_hash(prompt):
        return ProvenanceResult(
            present=True,
            ok=False,
            reason="scenario prompt changed since capture (prompt hash mismatch) — recapture the transcript",
        )
    if head_sha and manifest.head_sha and manifest.head_sha != head_sha:
        return ProvenanceResult(
            present=True,
            ok=False,
            reason=(
                f"transcript captured at HEAD {manifest.head_sha[:12]} but grading at {head_sha[:12]} — "
                "stale against a since-changed skill; recapture the transcript"
            ),
        )
    return ProvenanceResult(present=True, ok=True, reason="")
