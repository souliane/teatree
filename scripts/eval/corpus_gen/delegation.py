"""The orchestrator-delegation scenario shape and its builder.

A 'delegate the long unit of work, never do it in the foreground' scenario:
the orchestrator must dispatch a ``Task`` instead of doing the work itself. Kept
in its own module so :mod:`scripts.eval.corpus_gen.per_skill` stays within its
LOC budget.
"""

import dataclasses

from scripts.eval.corpus_gen.catalog import RULES, task
from scripts.eval.corpus_gen.model import Branch, Call, Scenario, match, negative, positive


@dataclasses.dataclass(frozen=True)
class DelegSpec:
    """A 'delegate the long unit of work, never do it in the foreground' scenario.

    Passes when a ``Task`` is dispatched whose prompt matches ``keyword`` (a
    regex); the negative ``forbid`` matcher (with its violating ``forbid_call``)
    pins that the orchestrator did not do the work itself in the foreground.

    ``fixture_phrase`` is the natural-language phrase the ``_pass`` fixture's
    Task prompt carries — a concrete sentence the ``keyword`` regex matches. It
    defaults to ``keyword`` (the simple plain-substring scenarios); a scenario
    whose ``keyword`` is a regex alternation supplies a readable phrase instead.
    """

    name: str
    desc: str
    prompt: str
    keyword: str
    forbid: Branch
    forbid_call: Call
    yaml_file: str
    fixture_phrase: str = ""
    #: Per-scenario metered-budget ceiling (USD). The CORRECT trajectory for a
    #: delegation scenario dispatches a sub-agent whose work burns more than the
    #: lane default ($1.0); without relief a budget-capped trial reds the pass@k
    #: aggregate (#2192) even though the delegation matcher passed and the agent
    #: did the right thing. ``None`` keeps the default for the cheap scenarios that
    #: finish under it (investigation/refactor pass 3/3 at the default).
    max_budget_usd: float | None = None

    def green_fixture_phrase(self) -> str:
        return self.fixture_phrase or self.keyword


def delegation_scenario(spec: DelegSpec) -> Scenario:
    return Scenario(
        name=spec.name,
        scenario=spec.desc,
        agent_path=RULES,
        prompt=spec.prompt,
        expects=(
            positive(
                match("Task", "prompt", spec.keyword),
                pass_call=task(f"please {spec.green_fixture_phrase()} in a worktree"),
                fail_call=task("do something else"),
            ),
            negative(spec.forbid, fail_call=spec.forbid_call),
        ),
        tools=("Bash", "Task", "Edit"),
        yaml_file=spec.yaml_file,
        max_budget_usd=spec.max_budget_usd,
    )
