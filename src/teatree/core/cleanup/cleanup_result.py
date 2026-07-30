"""The result value object returned by a single worktree teardown.

Split out of :mod:`teatree.core.cleanup.cleanup` to keep that module under the
module-health LOC cap. :class:`CleanupResult` is the structured outcome of
:func:`teatree.core.cleanup.cleanup.cleanup_worktree` — a human-readable
``label`` plus the machine-readable ``errors`` channel every teardown-step
failure appends to (#877/#932) — re-exported from ``cleanup`` for its callers.
"""

from dataclasses import dataclass, field


@dataclass(slots=True)
class CleanupResult:
    """Outcome of a single :func:`cleanup_worktree` teardown.

    ``label`` is the human-readable summary (still printed by the
    interactive ``clean-all`` / ``clean-merged`` callers and surfaced as
    the runner ``detail``). ``errors`` is the structured, machine-readable
    channel: every teardown step that failed appends a descriptive string
    here instead of crashing mid-teardown or being swallowed by a
    ``suppress(Exception)`` (#877).

    #932's lesson — a swallowed string the caller never inspects is not
    surfacing. Sync backends push ``errors`` into ``SyncResult.errors`` and
    runners fold it into their failure detail, so a teardown failure
    actually reaches the operator/exit path.
    """

    label: str
    errors: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True when every teardown step succeeded."""
        return not self.errors

    def __str__(self) -> str:
        if self.errors:
            return f"{self.label} [with errors: {'; '.join(self.errors)}]"
        return self.label
