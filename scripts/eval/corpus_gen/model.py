"""Typed declaration of one eval scenario and its anti-vacuous fixtures.

A :class:`Scenario` declares the YAML the loader parses *and* the concrete
tool-call inputs that make each of the three fixtures consistent with the
matchers. The emitter (:mod:`scripts.eval.corpus_gen.emit`) turns a scenario
into the on-disk YAML and ``stream-json`` fixtures.

Matcher model mirrors :mod:`teatree.eval.loader`: an :class:`Expect` is a
positive matcher (a tool call whose ``arg`` matches ``value`` must exist), a
negative matcher (no tool call whose ``arg`` matches ``value`` may exist), or a
disjunction (``any_of`` — at least one positive branch holds). ``op`` is
``contains`` (substring) or ``~`` (regex), exactly the loader's two operators.
"""

import dataclasses
import json

from teatree.agents.model_tiering import DEFAULT_PHASE_MODELS

POSITIVE = "positive"
NEGATIVE = "negative"
ANY_OF = "any_of"


@dataclasses.dataclass(frozen=True)
class Call:
    """One tool call to embed in a fixture transcript."""

    tool: str
    args: dict[str, object]


@dataclasses.dataclass(frozen=True)
class Branch:
    """One positive alternative inside an ``any_of`` disjunction."""

    tool: str
    arg: str
    value: str
    op: str = "~"


@dataclasses.dataclass(frozen=True)
class Expect:
    """One declared matcher and the calls that pass / fail it.

    For a positive / negative matcher, ``tool``/``arg``/``op``/``value`` render
    the loader matcher line. ``pass_call`` is the call that satisfies the
    scenario for the ``_pass`` fixture; ``fail_call`` is the call that violates
    it for the ``_fail`` fixture. For an ``any_of`` disjunction, ``branches``
    holds the positive alternatives and ``pass_call`` satisfies one of them.
    """

    kind: str
    tool: str = ""
    arg: str = ""
    op: str = "~"
    value: str = ""
    branches: tuple[Branch, ...] = ()
    pass_call: Call | None = None
    fail_call: Call | None = None

    @property
    def is_positive(self) -> bool:
        return self.kind in {POSITIVE, ANY_OF}


def match(tool: str, arg: str, value: str, op: str = "~") -> Branch:
    """A matcher target (tool + arg + op + value), reused by positive/negative/any_of."""
    return Branch(tool=tool, arg=arg, value=value, op=op)


def positive(target: Branch, *, pass_call: Call, fail_call: Call) -> Expect:
    return Expect(
        kind=POSITIVE,
        tool=target.tool,
        arg=target.arg,
        op=target.op,
        value=target.value,
        pass_call=pass_call,
        fail_call=fail_call,
    )


def negative(target: Branch, *, fail_call: Call) -> Expect:
    return Expect(
        kind=NEGATIVE, tool=target.tool, arg=target.arg, op=target.op, value=target.value, fail_call=fail_call
    )


def any_of(branches: tuple[Branch, ...], *, pass_call: Call) -> Expect:
    return Expect(kind=ANY_OF, branches=branches, pass_call=pass_call)


@dataclasses.dataclass(frozen=True)
class Scenario:
    """A single declared scenario: YAML fields plus its fixture calls."""

    name: str
    scenario: str
    agent_path: str
    prompt: str
    expects: tuple[Expect, ...]
    tools: tuple[str, ...] = ("Bash",)
    max_turns: int = 3
    agent_sections: tuple[str, ...] = ()
    yaml_file: str = ""
    #: Explicit abstract tier (``frontier``/``balanced``/``cheap``). Empty → the
    #: tier is INFERRED from ``agent_path`` (see :func:`infer_tier_or_phase`). The
    #: emitted YAML carries ``tier:`` or ``phase:``, never a concrete model id —
    #: the single ``teatree.agents.model_tiering.TIER_MODELS`` constant owns ids.
    tier: str = ""
    #: Per-scenario metered-budget ceiling (USD). ``None`` leaves the lane default
    #: ($1.0). A scenario whose CORRECT trajectory legitimately dispatches a
    #: sub-agent (an orchestrator-delegation scenario) burns more than the default,
    #: so it needs relief — without it a budget-capped trial reds the pass@k
    #: aggregate (#2192) even though every matcher passed and the agent did the
    #: right thing. Mirrors the hand-written ``delegates_under_load`` ($4.0).
    max_budget_usd: float | None = None
    #: Opt-in throwaway sandbox fixture emitted as the YAML ``fixture:`` field
    #: (``""`` → omitted). ``git_repo`` provisions a real repo (staged change,
    #: commits to squash, an ``origin`` remote) so a working-tree-presupposing
    #: prompt matches the sandbox and the agent fires the command instead of
    #: investigating the empty-cwd mismatch.
    fixture: str = ""

    @property
    def has_negative(self) -> bool:
        return any(e.kind == NEGATIVE for e in self.expects)

    @property
    def has_positive(self) -> bool:
        return any(e.is_positive for e in self.expects)


def _op_expr(op: str, value: str) -> str:
    """YAML-safe ``'op "value"'`` scalar.

    The loader matches ``op "value"`` after YAML parsing, so the whole
    expression is wrapped in single quotes — a regex value may contain ``#``
    (a YAML comment lead-in), ``:`` or ``{`` that would otherwise corrupt the
    scalar. Any literal single quote in the value is YAML-escaped by doubling.
    """
    escaped = value.replace("'", "''")
    return f"'{op} \"{escaped}\"'"


def _matcher_yaml(expect: Expect, indent: str) -> list[str]:
    if expect.kind == ANY_OF:
        lines = [f"{indent}- any_of:"]
        for branch in expect.branches:
            lines.extend(
                (
                    f"{indent}    - tool_call: {branch.tool}",
                    f"{indent}      args.{branch.arg}: {_op_expr(branch.op, branch.value)}",
                )
            )
        return lines
    if expect.kind == POSITIVE:
        return [
            f"{indent}- tool_call: {expect.tool}",
            f"{indent}  args.{expect.arg}: {_op_expr(expect.op, expect.value)}",
        ]
    return [
        f"{indent}- no_tool_call_matching:",
        f"{indent}    {expect.tool}.{expect.arg}: {_op_expr(expect.op, expect.value)}",
    ]


def infer_tier_or_phase(agent_path: str) -> str:
    """Infer the ``tier:``/``phase:`` YAML line for a scenario from its *agent_path*.

    Maps the skill the scenario exercises to a teatree FSM phase (resolved to a
    tier at run time) or, when no phase fits, the ``balanced`` tier. The single
    ``teatree.agents.model_tiering.TIER_MODELS`` constant owns the concrete model
    ids — this emits an ABSTRACT tier/phase only, never a model id.

    A phase that :data:`teatree.agents.model_tiering.DEFAULT_PHASE_MODELS`
    resolves to the ``frontier`` tier (``coding``/``reviewing``/``planning``/
    ``debugging``/``retrospecting``) is pinned to ``tier: balanced`` instead of
    emitted as ``phase:`` — a shipped scenario silently riding Opus throttled the
    metered CI eval lane's shared subscription-OAuth account (souliane/teatree
    run 28515055436). Reading ``DEFAULT_PHASE_MODELS`` (never duplicating its
    frontier set here) keeps this in lockstep with production phase tiering, so
    a future frontier phase is caught automatically rather than by memory.
    """
    p = agent_path.lower()
    phase = None
    if "planner" in p or "writing-plans" in p or "planning" in p:
        phase = "planning"
    elif "review-request" in p or "review_request" in p:
        phase = "requesting_review"
    elif "/code/" in p or p.endswith("/code"):
        phase = "coding"
    elif "/review/" in p or "/e2e-review/" in p:
        phase = "reviewing"
    elif "/test/" in p:
        phase = "testing"
    elif "/ship/" in p:
        phase = "shipping"
    elif "/retro/" in p:
        phase = "retrospecting"
    if phase is not None and DEFAULT_PHASE_MODELS.get(phase) != "frontier":
        return f"  phase: {phase}"
    return "  tier: balanced"


def scenario_yaml(scenario: Scenario) -> str:
    """Render one scenario as a YAML list entry the loader accepts."""
    tools = "[" + ", ".join(scenario.tools) + "]"
    lines = [
        f"- name: {scenario.name}",
        f"  scenario: {json.dumps(scenario.scenario, ensure_ascii=False)}",
        f"  agent_path: {scenario.agent_path}",
    ]
    if scenario.agent_sections:
        sections = "[" + ", ".join(json.dumps(s, ensure_ascii=False) for s in scenario.agent_sections) + "]"
        lines.append(f"  agent_sections: {sections}")
    tier_line = f"  tier: {scenario.tier}" if scenario.tier else infer_tier_or_phase(scenario.agent_path)
    lines += [
        tier_line,
        f"  max_turns: {scenario.max_turns}",
    ]
    if scenario.max_budget_usd is not None:
        lines.append(f"  max_budget_usd: {scenario.max_budget_usd}")
    if scenario.fixture:
        lines.append(f"  fixture: {scenario.fixture}")
    lines += [
        f"  tools: {tools}",
        f"  prompt: {json.dumps(scenario.prompt, ensure_ascii=False)}",
        "  expect:",
    ]
    for expect in scenario.expects:
        lines.extend(_matcher_yaml(expect, "    "))
    return "\n".join(lines) + "\n"


def _event(call: Call, turn_id: int) -> str:
    block = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": f"toolu_{turn_id:02d}", "name": call.tool, "input": call.args}],
        },
    }
    return json.dumps(block)


def _init(session: str) -> str:
    return json.dumps({"type": "system", "subtype": "init", "session_id": session, "model": "claude-sonnet-4-6"})


def _text(message: str) -> str:
    block = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": message}]}}
    return json.dumps(block)


def _result() -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1})


def _calls_for(scenario: Scenario, variant: str) -> list[Call]:
    if variant == "noop":
        return []
    calls: list[Call] = []
    for expect in scenario.expects:
        if variant == "pass" and expect.pass_call is not None:
            calls.append(expect.pass_call)
        if variant == "fail" and expect.fail_call is not None:
            calls.append(expect.fail_call)
    return calls


def fixture_stream(scenario: Scenario, variant: str) -> str:
    """Render the ``stream-json`` fixture for ``pass`` / ``fail`` / ``noop``.

    The ``pass`` fixture embeds every matcher's satisfying call; the ``fail``
    fixture embeds the violating call (a missing positive call, or a forbidden
    negative call); the ``noop`` fixture embeds none, so an only-negative
    scenario is exposed as vacuous by the anti-vacuous gate.
    """
    session = f"fixt-{scenario.name}-{variant}"
    lines = [_init(session), _text("working on it.")]
    for offset, call in enumerate(_calls_for(scenario, variant), start=1):
        lines.append(_event(call, offset))
    lines.append(_result())
    return "\n".join(lines) + "\n"
