"""First-party skill pins their source has moved past (#4677) — a pure trigger.

``t3 setup`` has reported this drift correctly on every run for 31 days, as one INFO
line in a ~100-line log the container entrypoint prints on every start, and nobody
read it. Something right and unheard is indistinguishable from something absent. This
scanner is the half that does not need a reader: it notices on its own cadence and the
mechanical handler opens the bump PR.

A loop rather than a prek hook, because a hook fires only when someone commits to this
repo while the box needs refreshing regardless of commit activity — and a hook does not
run in the container, where the skills actually resolve.

**The allowlist is load-bearing, not a detail.** ``dependencies.apm`` deliberately mixes
two trust classes, and a blanket "bump every pin" refresher would pull unreviewed
third-party skill code onto the box on a timer — precisely what pinning exists to
prevent. So a source outside the manifest owner's own namespace is never fetched and
never signalled: the guarantee is the absent fetch, not a filter somewhere downstream.

Read-only with respect to the core clone and to ``~/.claude/skills``: it fetches into
its own cache and emits a signal. ``apm.yml`` stays the single source of truth, the
bump lands as a reviewable PR, and ``t3 setup`` installs it on its next run.
"""

import logging
from dataclasses import dataclass, field
from itertools import starmap
from pathlib import Path

from teatree.loop.scanners.base import ScanSignal
from teatree.provisioning.declared import DeclarationUnreadableError, skills_declared_in_apm_manifest
from teatree.provisioning.skill_pin import DEFAULT_REMOTE_BASE, first_party_owner, measure_skill_pins
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

logger = logging.getLogger(__name__)

#: Routed to the ``open_skill_pin_bump_pr`` mechanical handler via ``MECHANICAL_BY_KIND``.
SKILL_PIN_BEHIND_KIND = "skill_pin.behind"

_APM_MANIFEST = "apm.yml"
_GIT_TIMEOUT_SECONDS = 120
_MAX_LOGGED_COMMITS = 20


@dataclass(slots=True)
class SkillPinRefreshScanner:
    """Emit one signal per first-party source whose head has left the declared pin."""

    repo: Path
    cache_root: Path
    remote_base: str = DEFAULT_REMOTE_BASE
    name: str = "skill_pin_refresh"
    _clones: dict[str, Path] = field(default_factory=dict)

    def scan(self) -> list[ScanSignal]:
        manifest = self.repo / _APM_MANIFEST
        try:
            declared = skills_declared_in_apm_manifest(manifest)
        except DeclarationUnreadableError:
            logger.warning("%s: %s is unreadable, so no pin can be measured", self.name, manifest)
            return []
        owner = first_party_owner(manifest)
        if not owner:
            return []
        behind = [
            status
            for status in measure_skill_pins(declared, remote_base=self.remote_base, first_party_owner=owner)
            if status.is_behind and status.first_party
        ]
        return list(starmap(self._signal, _by_source(behind)))

    def _signal(self, source: str, statuses: list) -> ScanSignal:
        pinned, head = statuses[0].pinned_ref, statuses[0].head_sha
        return ScanSignal(
            kind=SKILL_PIN_BEHIND_KIND,
            summary=f"{source} has moved past its declared pin ({len(statuses)} spec(s) to bump)",
            payload={
                "repo": str(self.repo),
                "source": source,
                "pinned": pinned,
                "head": head,
                "specs": [status.spec for status in statuses],
                "bumped_specs": [status.bumped_spec for status in statuses],
                "log": self._log_between(source, pinned, head),
            },
        )

    def _log_between(self, source: str, pinned: str, head: str) -> str:
        """What the bump would bring in — empty when the range cannot be read.

        A fetched clone is needed for this and for nothing else, so it happens only
        for a source already proved first-party AND already proved behind.
        """
        clone = self._fetch(source)
        if clone is None:
            return ""
        result = run_allowed_to_fail(
            ["git", "-C", str(clone), "log", "--oneline", f"-{_MAX_LOGGED_COMMITS}", f"{pinned}..{head}"],
            expected_codes=None,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def _fetch(self, source: str) -> Path | None:
        if source in self._clones:
            return self._clones[source]
        destination = self.cache_root / f"refresh-{source.replace('/', '-')}"
        try:
            if not (destination / ".git").exists():
                self.cache_root.mkdir(parents=True, exist_ok=True)
                cloned = run_allowed_to_fail(
                    ["git", "clone", "--quiet", f"{self.remote_base}{source}", str(destination)],
                    expected_codes=None,
                    timeout=_GIT_TIMEOUT_SECONDS,
                )
                if cloned.returncode != 0:
                    logger.warning("%s: could not clone %s: %s", self.name, source, cloned.stderr.strip())
                    return None
            else:
                run_allowed_to_fail(
                    ["git", "-C", str(destination), "fetch", "--quiet", "origin"],
                    expected_codes=None,
                    timeout=_GIT_TIMEOUT_SECONDS,
                )
        except (TimeoutExpired, OSError):
            logger.warning("%s: could not read %s", self.name, source, exc_info=True)
            return None
        self._clones[source] = destination
        return destination


def _by_source(statuses: list) -> list[tuple[str, list]]:
    """Group by source repo so several skills sharing one source ride one bump."""
    grouped: dict[str, list] = {}
    for status in statuses:
        grouped.setdefault(status.source_repo, []).append(status)
    return sorted(grouped.items())


__all__ = ["SKILL_PIN_BEHIND_KIND", "SkillPinRefreshScanner"]
