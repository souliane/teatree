"""The HARNESS-failure axis: a run that MEASURED NOTHING, distinct from a verdict.

:mod:`teatree.eval.surface` answers "does this scenario's verdict gate a lane?" — a
question that presupposes a verdict. A harness failure has none to exempt: the wiring
the scenario exists to measure never came up, so the run graded the raw model instead
of the system under test. ``hooks_not_registered`` is the shipped instance — a
``production_hooks`` scenario whose captured stream carried zero hook lifecycle events,
i.e. the plugin never registered (:func:`teatree.eval.production_hooks.has_hook_events`).

Both axes reach the same verdict points, and the advisory exemption used to swallow this
one: 6 of the 7 ``production_hooks`` scenarios are ``surface: interactive``, so on the
nightly ``clean_room`` shard EVERY hooked scenario was advisory and the fail-loud could
never gate (souliane/teatree#3922). The two are therefore kept separate rather than
composed — the surface decides a verdict's weight, and this axis is read by the
unconditional ``RunGuards.hooks_registered`` guard that runs BESIDE the verdict, never
inside it, so no surface exemption can reach it.

Pure and import-free by design: every shape that carries a terminal reason
(:class:`~teatree.eval.report.ScenarioResult`, :class:`~teatree.eval.pass_at_k.PassAtKResult`,
:class:`~teatree.eval.matrix.MatrixRow`) imports THIS, so it can import none of them.
"""

#: The terminal reason stamped on a ``production_hooks`` run that captured zero hook
#: events — the shipped plugin never registered, so the lane silently degraded back to
#: raw-model measurement.
HOOKS_NOT_REGISTERED_REASON = "hooks_not_registered"

#: Every terminal reason meaning the HARNESS failed rather than the agent. Distinct from
#: :data:`~teatree.eval.models.CAP_TERMINAL_REASONS` (a run that measured something and
#: was truncated) — these measured nothing at all.
HARNESS_FAILURE_REASONS: frozenset[str] = frozenset({HOOKS_NOT_REGISTERED_REASON})

#: Every lane that drives the runner and must therefore call the guard, NAMED — never
#: counted, for the reason :data:`~teatree.eval.surface.ADVISORY_EXEMPT_VERDICT_POINTS`
#: is named: a stale count still parses and still reads like a covered invariant.
#: ``tests/conformance/test_advisory_verdict_points.py`` resolves every symbol here and
#: asserts its module actually calls the guard.
HARNESS_FAILURE_GUARD_POINTS: tuple[str, ...] = (
    "teatree.cli.eval.single_trial.run_single_trial",
    "teatree.cli.eval.multi_trial.run_pass_at_k_lane",
    "teatree.cli.eval.multi_trial.run_model_matrix_lane",
    "teatree.cli.eval.all.run_full_suite",
    "teatree.cli.eval.ladder.ladder",
    "teatree.cli.eval.benchmark.benchmark",
)

#: Every fold that builds a :class:`~teatree.eval.matrix.MatrixRow` out of a
#: :class:`~teatree.eval.pass_at_k.PassAtKResult`, NAMED for the same reason as the guard
#: points above. Calling the guard is not sufficient on its own: the guard reads the
#: row's OWN flag, so a fold that omits it hands over a clean row and the call is vacuous
#: — the lane reports green having measured nothing, which is #3922 again on that lane
#: alone. Each name must mention ``harness_failed``.
HARNESS_FAILURE_FOLD_POINTS: tuple[str, ...] = (
    "teatree.cli.eval.multi_trial._matrix_trial",
    "teatree.eval.ladder._row_from",
)

#: The two producers of the serialized ``advisory`` flag. A lane exits on the guard
#: above, but the flag outlives the process: the ``eval-ci-heal`` combine job re-gates a
#: MERGED artifact (:func:`teatree.eval.green_proof.evaluate_green_proof`) and the fixer
#: dispatch reads the same rows, so a row that measured nothing must never be written
#: advisory or the second gate blesses a shard the first one failed.
HARNESS_FAILURE_ADVISORY_CARVE_OUTS: tuple[str, ...] = (
    "teatree.eval.summary_json._ScenarioRow.as_json",
    "teatree.cli.eval.escalate.escalate_failures",
)


def measured_nothing(terminal_reason: str) -> bool:
    """Whether *terminal_reason* marks a harness failure — the run measured nothing."""
    return terminal_reason in HARNESS_FAILURE_REASONS
