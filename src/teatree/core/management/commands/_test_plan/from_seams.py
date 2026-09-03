"""Assemble the ``scenario-plan`` test plan from overlay seams (``--from-seams``, #3329).

Core already owns every input: the authored scenarios
(:meth:`OverlayE2E.scenarios`), the run's captures (the artifacts dir), and the
run provenance (``Ticket.extra['e2e_recipe']`` — the per-repo SHAs and env core
recorded). This module is the fold that joins them into the plan the renderer
already knows how to draw, so an overlay ships the manifest and nothing else —
no assembler, no write command, no duplicate ``Scenario`` type.

Resolution and the fail-loud modes core should own live here (each was
re-implemented per overlay): default the spec to the recipe's recorded
``last_run.spec_path``; default the artifacts dir to the run's recorded root;
fail loud when a declared capture slot has no file, when a spec has no authored
scenarios, or when no per-repo SHAs are recorded.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from teatree.core.e2e_scenario import Scenario
from teatree.core.evidence.test_plan_blocked_gate import BlockedTestPlanPostError
from teatree.core.intake.e2e_workitem import load_recipe
from teatree.core.intake.resolve import WorktreeNotFoundError, resolve_worktree
from teatree.core.management.commands._test_plan.committed_captures import (
    evidence_dir_for,
    refuse_invalid_committed_captures,
)
from teatree.core.management.commands._test_plan.file_store import plan_path_for_ticket, write_plan
from teatree.core.management.commands._test_plan.render import (
    PlanState,
    TestPlanValidationError,
    empty_state,
    render_body,
)
from teatree.core.management.commands._test_plan.scenario import Scenario as RenderScenario
from teatree.core.management.commands._test_plan.scenario import ScenarioImage
from teatree.core.management.commands._test_plan.write import PlanWriteResult, artifact_ref, resolve_ticket
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay

_SCENARIO_TEMPLATE = "scenario-plan"


class FromSeamsError(TestPlanValidationError):
    """A ``--from-seams`` assembly precondition failed; nothing is posted.

    A subclass of :class:`TestPlanValidationError` so the command's single
    ``except`` arm surfaces every fail-loud case (no recorded SHAs, no authored
    scenarios, a declared capture slot with no file) as a non-zero exit with
    nothing written.
    """


@dataclass(frozen=True, slots=True)
class SeamsRun:
    """The run facts ``--from-seams`` folds, resolved from the recipe + CLI overrides."""

    ticket_number: str
    spec_path: str
    artifacts_root: Path
    env: str
    per_repo_shas: dict[str, str]
    ran_at: str


def resolve_seams_run(ticket: Ticket, *, spec_path: str, artifacts_dir: str) -> SeamsRun:
    """Resolve the run to assemble from, defaulting the spec + artifacts dir to the recipe's.

    Raises :class:`FromSeamsError` when no per-repo SHAs are recorded (there is
    no run to assemble), when no spec resolves, or when no artifacts dir resolves.
    """
    last_run = load_recipe(ticket).last_run or {}
    per_repo_shas = {str(k): str(v) for k, v in (last_run.get("per_repo_shas") or {}).items()}
    if not per_repo_shas:
        msg = (
            f"No per-repo SHAs recorded for {ticket} — run the e2e first "
            "(`t3 <overlay> e2e run <work-item>`) so a green run records the recipe."
        )
        raise FromSeamsError(msg)
    resolved_spec = spec_path or str(last_run.get("spec_path") or "")
    if not resolved_spec:
        msg = "No spec to assemble: pass --spec-path, or run the e2e so the recipe records last_run.spec_path."
        raise FromSeamsError(msg)
    resolved_artifacts = artifacts_dir or str(last_run.get("artifacts_dir") or "")
    if not resolved_artifacts:
        msg = "No artifacts dir: pass --artifacts-dir, or run the e2e so the recipe records the run's artifacts root."
        raise FromSeamsError(msg)
    return SeamsRun(
        ticket_number=ticket.ticket_number,
        spec_path=resolved_spec,
        artifacts_root=Path(resolved_artifacts),
        env=str(last_run.get("env") or "local"),
        per_repo_shas=per_repo_shas,
        ran_at=str(last_run.get("timestamp") or ""),
    )


def resolve_capture_file(run: SeamsRun, *, slot: str) -> Path:
    """Resolve a declared capture ``slot`` to a file under ``<root>/<ticket>/<env>/``.

    Tries ``<slot>`` then ``<slot>.png``. Raises :class:`FromSeamsError` naming
    the slot when neither exists — a declared capture with no file is a hard
    failure, not a silently-dropped image.
    """
    base = run.artifacts_root / run.ticket_number / run.env
    for candidate in (base / slot, base / f"{slot}.png"):
        if candidate.is_file():
            return candidate
    msg = f"Capture slot {slot!r} has no file under {base} (tried {slot} and {slot}.png)."
    raise FromSeamsError(msg)


def _render_scenario(scenario: Scenario, *, run: SeamsRun) -> RenderScenario:
    """Map one authored :class:`Scenario` to the render ``TypedDict``, citing its captures."""
    images: list[ScenarioImage] = []
    if not scenario.is_api:
        for capture in scenario.captures:
            filepath = resolve_capture_file(run, slot=capture.slot)
            image_md = artifact_ref(filepath, root=run.artifacts_root)
            images.append({"slot": capture.slot, "caption": capture.caption, "image_md": image_md})
    return {
        "surface": scenario.surface,
        "title": scenario.title,
        "preconditions": scenario.preconditions,
        "steps": list(scenario.steps),
        "expected": scenario.expected,
        "modality": "api" if scenario.is_api else "ui",
        "actual_pass": True,
        "images": images,
    }


def assemble_scenario_state(*, title: str, scenarios: tuple[Scenario, ...], run: SeamsRun) -> PlanState:
    """Fold the authored scenarios + the run's captures + the recorded SHAs into a plan state."""
    state = empty_state(ticket=run.ticket_number, title=title)
    state["template"] = _SCENARIO_TEMPLATE
    state["scenarios"] = [_render_scenario(scenario, run=run) for scenario in scenarios]
    side = "dev" if run.env == "dev" else "local"
    state[side]["commits"] = dict(run.per_repo_shas)
    state[side]["env"] = side
    state[side]["ran_at"] = run.ran_at
    shas = ", ".join(f"{repo_name} `{sha}`" for repo_name, sha in sorted(run.per_repo_shas.items()))
    state["environment"] = f"{run.env} — {shas}"
    return state


def _worktree_or_none() -> Worktree | None:
    try:
        return resolve_worktree()
    except WorktreeNotFoundError:
        return None


@dataclass(frozen=True, slots=True)
class FromSeamsRequest:
    """The CLI inputs for ``write-test-plan --from-seams``: ticket + run overrides.

    ``spec_path`` / ``artifacts_dir`` default (empty) to the recipe's recorded
    ``last_run``; ``title`` overrides the plan heading (empty → the issue URL).
    """

    ticket: str = ""
    spec_path: str = ""
    artifacts_dir: str = ""
    title: str = ""


def run_from_seams(
    request: FromSeamsRequest,
    *,
    write_err: Callable[[str], None],
) -> PlanWriteResult:
    """Assemble + write the ``scenario-plan`` file for a spec from the overlay seams.

    Resolves the ticket, folds ``overlay.e2e.scenarios(spec)`` + the run's
    captures + the recipe's recorded SHAs into the plan, and writes the ticket's
    single plan file. Captures already committed beside that plan are re-run
    through the same gate the manifest path applies, so this write path cannot
    be the one that lets an unvalidated screenshot reach a reviewer. Any
    fail-loud precondition writes to ``write_err`` and exits non-zero with
    nothing written.
    """
    try:
        return _assemble_and_write(request)
    except (TestPlanValidationError, BlockedTestPlanPostError) as err:
        write_err(str(err))
        raise SystemExit(1) from err


def _assemble_and_write(request: FromSeamsRequest) -> PlanWriteResult:
    resolved_ticket = resolve_ticket(request.ticket, _worktree_or_none())
    issue_url = str(resolved_ticket.issue_url)
    run = resolve_seams_run(resolved_ticket, spec_path=request.spec_path, artifacts_dir=request.artifacts_dir)
    scenarios = get_overlay().e2e.scenarios(run.spec_path)
    if not scenarios:
        msg = (
            f"Spec {run.spec_path!r} has no authored scenarios "
            "(overlay.e2e.scenarios returned none) — nothing to assemble."
        )
        raise FromSeamsError(msg)

    state = assemble_scenario_state(title=request.title.strip() or issue_url, scenarios=scenarios, run=run)
    body = render_body(state)
    path = plan_path_for_ticket(resolved_ticket)
    refuse_invalid_committed_captures(evidence_dir_for(path), incoming=[])
    action = "updated" if path.is_file() else "created"
    write_plan(path, body)
    return PlanWriteResult(path=str(path), envs=[run.env], action=action)
