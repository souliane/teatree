"""How the dashboard reads an attempt's recorded skill bundle (#3886).

One rule, shared by every surface that lists a task — the ticket drawer, the session
index and the live-work view — so "no skills" cannot read as a fault on one page and
as blank on another.

The bundle is the single biggest determinant of how an agent behaves in a phase, and
it is assembled from several independent sources that can each silently contribute
nothing: the phase's ``agents/<name>.md`` frontmatter, cwd-driven detection, the
transitive ``requires:`` chain resolved against a cached index, and the active
overlay's companion/stage skills. A cold cache, an overlay that resolves to nothing,
or a detector that does not fire all degrade the bundle quietly — the dispatch still
runs and still looks entirely normal. Rendering the empty result as blank is what
makes that invisible, so an empty bundle is reported as a FAULT instead.

Read from what the dispatch RECORDED (``TaskAttempt.skills_loaded``), never re-derived
at render time: a re-derivation reports today's answer for yesterday's dispatch, which
is precisely the bug class this surface exists to expose.
"""

from typing import TYPE_CHECKING

from teatree.core.models.task import Task

if TYPE_CHECKING:
    from teatree.core.models.task_attempt import TaskAttempt


def skill_bundle(attempt: "TaskAttempt") -> tuple[tuple[str, ...], bool]:
    """*attempt*'s recorded bundle, and whether an empty one is a fault.

    A HEADLESS attempt resolves a bundle by construction, so an empty one is a
    fault worth showing. An INTERACTIVE attempt runs inside the operator's own
    session and never resolves one, so it is exempt rather than perpetually
    accusing a surface that was never going to carry a bundle.
    """
    names = tuple(str(name) for name in (attempt.skills_loaded or []))
    headless = str(attempt.execution_target) == Task.ExecutionTarget.HEADLESS
    return names, headless and not names
