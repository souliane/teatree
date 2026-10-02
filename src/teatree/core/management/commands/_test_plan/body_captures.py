"""The captures a hand-authored ``--body-file`` plan links to.

A body names its captures itself, as links into ``evidence/<plan>/<side>/<file>``
(or a legacy flat ``evidence/<plan>/<file>``). Each link must resolve before the
body is written: with ``--embed-captures`` a side link is sourced from the run's
artifacts directory and copied in; otherwise it must already be committed. The
resolved set then passes the same capture gates as a manifest's captures.
"""

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from teatree.core.management.commands._test_plan.committed_captures import EVIDENCE_DIR_NAME
from teatree.core.management.commands._test_plan.state import _ENVS, TestPlanValidationError
from teatree.utils.media import MediaKind, media_kind

__all__ = ["BodyCaptures", "resolve_body_captures"]

_LINK_RE = re.compile(
    rf"\]\(\s*(?:\./)?({EVIDENCE_DIR_NAME}/[^)\s]+)|\bsrc\s*=\s*[\"'](?:\./)?({EVIDENCE_DIR_NAME}/[^\"']+)[\"']"
)


@dataclass(frozen=True, slots=True)
class BodyCaptures:
    """Where each capture a body links to comes from.

    ``incoming`` maps a side to the artifacts copied under ``<evidence_dir>/<side>/``;
    ``committed`` holds linked captures already in git; ``superseded_flat`` holds
    legacy flat captures the body no longer links whose side re-capture replaces them.
    """

    evidence_dir: Path
    incoming: dict[str, list[Path]] = field(default_factory=dict)
    committed: list[Path] = field(default_factory=list)
    superseded_flat: list[Path] = field(default_factory=list)

    def linked(self, kind: MediaKind) -> list[Path]:
        every = [source for sources in self.incoming.values() for source in sources] + self.committed
        return [capture for capture in every if media_kind(capture) is kind]

    def incoming_images(self) -> dict[str, list[Path]]:
        return {
            side: [source for source in sources if media_kind(source) is MediaKind.IMAGE]
            for side, sources in self.incoming.items()
        }

    def embed(self) -> None:
        for legacy in self.superseded_flat:
            legacy.unlink()
        for side, sources in self.incoming.items():
            destination = self.evidence_dir / side
            destination.mkdir(parents=True, exist_ok=True)
            for source in sources:
                shutil.copyfile(source, destination / source.name)


def resolve_body_captures(body: str, *, evidence_dir: Path, embed: bool, artifacts_dir: Path | None) -> BodyCaptures:
    """Resolve every evidence link in *body*, refusing one nothing can satisfy."""
    prefix = f"{EVIDENCE_DIR_NAME}/{evidence_dir.name}/"
    links = [
        (link, *_split_link(link, prefix=prefix))
        for link in dict.fromkeys(unquote(a or b) for a, b in _LINK_RE.findall(body))
    ]
    flat_linked = {evidence_dir / name for _, side, name in links if not side}
    index = _artifact_index(artifacts_dir) if embed and artifacts_dir is not None else {}
    captures = BodyCaptures(evidence_dir=evidence_dir)
    for link, side, name in links:
        if side and embed:
            source = _artifact_for(index, link=link, side=side, name=name, artifacts_dir=artifacts_dir)
            captures.incoming.setdefault(side, []).append(source)
            legacy = evidence_dir / name
            if legacy.is_file() and legacy not in flat_linked and legacy not in captures.superseded_flat:
                captures.superseded_flat.append(legacy)
            continue
        committed = evidence_dir / side / name
        if not committed.is_file():
            hint = " — pass --embed-captures with --artifacts-dir to copy it from the run" if side else ""
            msg = f"The body links {link!r}, but no capture is committed at {committed}{hint}."
            raise TestPlanValidationError(msg)
        captures.committed.append(committed)
    return captures


def _split_link(link: str, *, prefix: str) -> tuple[str, str]:
    """``(side, file name)`` for one evidence link; side is empty for a legacy flat capture."""
    if not link.startswith(prefix):
        msg = f"The body links {link!r}; this plan's captures live under {prefix}."
        raise TestPlanValidationError(msg)
    side, _, name = link.removeprefix(prefix).rpartition("/")
    if name and (not side or side in _ENVS):
        return side, name
    msg = f"The body links {link!r}; expected {prefix}<dev, local or stack>/<file> or {prefix}<file>."
    raise TestPlanValidationError(msg)


def _artifact_index(artifacts_dir: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    if artifacts_dir.is_dir():
        for path in sorted(artifacts_dir.rglob("*")):
            if path.is_file():
                index.setdefault(path.name, []).append(path)
    return index


def _artifact_for(index: dict[str, list[Path]], *, link: str, side: str, name: str, artifacts_dir: Path | None) -> Path:
    if artifacts_dir is None:
        msg = f"The body links {link!r}; --embed-captures needs --artifacts-dir (or T3_E2E_ARTIFACTS_DIR) to find it."
        raise TestPlanValidationError(msg)
    matches = [path for path in index.get(name, []) if side in path.relative_to(artifacts_dir).parts[:-1]]
    if len(matches) == 1:
        return matches[0]
    found = "not found" if not matches else f"ambiguous ({', '.join(str(match) for match in matches)})"
    msg = f"The body links {link!r}: capture {name!r} for {side!r} {found} under {artifacts_dir}."
    raise TestPlanValidationError(msg)
