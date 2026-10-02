"""The question-SURFACE axis: which question/answer surface a scenario grades.

The contract teatree must guarantee is that a question **reaches the user and an
answer comes back** — headless-first, over Slack, with a ``DeferredQuestion`` as the
durable record and the inbox loop draining the reply. The Claude-interactive
``AskUserQuestion`` tool call is ONE implementation of that contract, and its wire
shape belongs to a bundled ``claude`` CLI generation: a CLI at/after 2.1.204 renders
the call as a markdown ``**AskUserQuestion**`` chip rather than a ``tool_use`` block,
which no ``tool_call: AskUserQuestion`` matcher can ever see.

So a scenario that HARD-REQUIRES that tool call is pinned to a CLI rendering, not to
the contract. It stays in the catalog and stays graded — Claude Code interactive is
used heavily and is worth measuring — but it is labelled ``surface: interactive`` and
its verdict is ADVISORY. Only ``surface: headless`` scenarios gate.

The exemption weighs a VERDICT, so it presupposes one. A run that measured nothing —
the harness itself failed — has none, and :mod:`teatree.eval.harness_failure` is that
separate axis: it is read by the unconditional ``RunGuards.hooks_registered`` guard that
runs BESIDE each lane's verdict, so nothing here can reach it (souliane/teatree#3922).

This module is the structural sibling of :mod:`teatree.eval.matcher_vacuity`: a pure,
fixture-free predicate over the loaded specs, so the mislabelled shape is a fast RED at
test time rather than a red suite the next time the SDK moves.
"""

from teatree.eval.models import INTERACTIVE_SURFACE, AnyOf, EvalSpec, ExpectItem, Matcher, canonicalize_tool

#: The interactive-only tool whose captured wire shape depends on the bundled CLI.
INTERACTIVE_QUESTION_TOOL = "AskUserQuestion"

#: Every verdict the advisory exemption must reach, NAMED — never counted.
#:
#: Prose that counted these ("at all three aggregation points", then "four") was
#: falsified twice by lanes nobody remembered to recount, and a stale count reads
#: as a covered invariant. Naming each one instead makes an omission visible: a new
#: lane is missing from a list, not hidden inside a number that still parses.
#: ``tests/conformance/test_advisory_verdict_points.py`` resolves every symbol here
#: and reds when the docs fall back to counting.
#:
#: The exemption is applied inside each of these; the FIRST four are the in-process
#: ``t3 eval run`` exits, the fifth is the ``eval-ci-heal`` combine job's second
#: gate over the merged artifact (souliane/teatree#3855, souliane/teatree#3921).
ADVISORY_EXEMPT_VERDICT_POINTS: tuple[str, ...] = (
    "teatree.cli.eval.all._ai_lane_result",
    "teatree.cli.eval.multi_trial.run_pass_at_k_lane",
    "teatree.cli.eval.multi_trial.run_model_matrix_lane",
    "teatree.cli.eval.run_modes.finalize_single_run",
    "teatree.cli.eval.escalate.EscalationOutcome.is_hard_red",
    "teatree.eval.green_proof.evaluate_green_proof",
)

#: The non-verdict consumer that must honour the exemption too: these names are the
#: fixer-DISPATCH set, so an advisory red here burns an agent on a bundled-CLI
#: rendering change no repo edit can fix (souliane/teatree#3921).
ADVISORY_EXEMPT_CONSUMERS: tuple[str, ...] = ("teatree.loop.ci_eval_heal_advance.red_scenario_names",)


def requires_interactive_tool_call(spec: EvalSpec) -> bool:
    """Whether *spec* cannot pass without an ``AskUserQuestion`` tool call being captured.

    Only a REQUIRED positive matcher counts. An :class:`~teatree.eval.models.AnyOf`
    alternative has another satisfiable branch, and a negative matcher is satisfied by
    a run that emits no chip at all — neither depends on the tool-call rendering.
    """
    return any(_is_required_interactive_matcher(matcher) for matcher in spec.matchers)


def _is_required_interactive_matcher(matcher: ExpectItem) -> bool:
    """Whether *matcher* can only pass when an ``AskUserQuestion`` call is captured.

    The name comparison is CASE-INSENSITIVE while the grader's is not: a guard that
    under-flags silently re-opens the SDK coupling, whereas over-flagging only makes
    one extra scenario advisory. So a lowercase ``askuserquestion`` — which the
    grader could never match anyway — is still treated as interactive-requiring.
    """
    if isinstance(matcher, AnyOf) or not isinstance(matcher, Matcher):
        return False
    tool = canonicalize_tool(matcher.tool)
    return matcher.kind == "positive" and tool.casefold() == INTERACTIVE_QUESTION_TOOL.casefold()


def is_advisory(spec: EvalSpec) -> bool:
    """Whether *spec*'s verdict is reported but never gates a lane."""
    return spec.surface == INTERACTIVE_SURFACE


def mislabelled_interactive_specs(specs: list[EvalSpec]) -> list[EvalSpec]:
    """The offenders: specs hard-requiring the interactive tool call yet still blocking.

    This is the guard that REPLACES the ``claude-agent-sdk`` Dependabot quarantine
    (souliane/teatree#3125, souliane/teatree#3855). The quarantine froze a whole
    dependency so a bundled-CLI rendering change could not red the suite; labelling
    every such scenario advisory removes the coupling at its source, and this
    predicate is what stops a NEW scenario from silently re-introducing it.
    """
    return [spec for spec in specs if requires_interactive_tool_call(spec) and not is_advisory(spec)]
