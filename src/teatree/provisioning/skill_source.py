import re
from dataclasses import dataclass

_MIN_SPEC_SEGMENTS = 3
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True, slots=True)
class SkillSource:
    """The repo, subpath, and pinned ref a skill declaration names."""

    owner_repo: str
    subpath: str
    ref: str

    def remote_url(self, base: str) -> str:
        """Where this source is fetched from under *base* — the one URL shape.

        Shared by the installer that clones it and the pin check that reads its
        head, so the two can never disagree about what a declaration points at.
        """
        return f"{base}{self.owner_repo}"


def parse_skill_source(spec: str) -> SkillSource | None:
    """Split ``<owner>/<repo>/<subpath>[#<ref>]``; ``None`` when it names no single skill."""
    body, _, ref = spec.partition("#")
    segments = body.strip("/").split("/")
    if len(segments) < _MIN_SPEC_SEGMENTS:
        return None
    return SkillSource(
        owner_repo="/".join(segments[:2]),
        subpath="/".join(segments[2:]),
        ref=ref.strip(),
    )


def owner_repo(spec: str) -> str:
    return "/".join(spec.partition("#")[0].strip("/").split("/")[:2])


def pinned_commit(spec: str) -> str:
    """The lowercase 40-hex ref of *spec*, or ``""``; lowercased so the CLI gets the string the pin check read."""
    ref = spec.partition("#")[2].strip().lower()
    return ref if _FULL_COMMIT.fullmatch(ref) else ""
