from dataclasses import dataclass

_MIN_SPEC_SEGMENTS = 3


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
