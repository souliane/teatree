"""Assemble the ``scenario-plan`` test plan from overlay seams (``--from-seams``, #3329).

Core already owns every input: the authored scenarios
(:meth:`OverlayE2E.scenarios`), the run's captures (the artifacts dir), and the
run provenance (``Ticket.extra['e2e_recipe']`` — the per-repo SHAs and env core
recorded). This module is the fold that joins them into the note the renderer
already knows how to draw, so an overlay ships the manifest and nothing else —
no assembler, no post command, no duplicate ``Scenario`` type.

Resolution and the fail-loud modes core should own live here (each was
re-implemented per overlay): default the spec to the recipe's recorded
``last_run.spec_path``; default the artifacts dir to the run's recorded root;
fail loud when a declared capture slot has no file, when a spec has no authored
scenarios, or when no per-repo SHAs are recorded.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.e2e_scenario import Scenario
from teatree.core.intake.e2e_workitem import load_recipe
from teatree.core.intake.resolve import WorktreeNotFoundError, resolve_worktree
from teatree.core.management.commands._shared_code_host import NO_CODE_HOST_MESSAGE
from teatree.core.management.commands._test_plan.post import (
    _ON_BEHALF_ACTION,
    PostTestPlanResult,
    _create_or_update_note,
    _resolve_ticket,
    _verified_embed,
    find_existing_note,
)
from teatree.core.management.commands._test_plan.render import (
    TestPlanState,
    TestPlanValidationError,
    empty_state,
    render_body,
)
from teatree.core.management.commands._test_plan.scenario import Scenario as RenderScenario
from teatree.core.management.commands._test_plan.scenario import ScenarioImage
from teatree.core.models import Ticket, Worktree
from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError, on_behalf_block_message
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post
from teatree.core.overlay_loader import get_overlay
from teatree.core.send_proxy import forge_from_url, route_forge_write

_SCENARIO_TEMPLATE = "scenario-plan"


class FromSeamsError(TestPlanValidationError):
    """A ``--from-seams`` assembly precondition failed; nothing is posted.

    A subclass of :class:`TestPlanValidationError` so the command's single
    ``except`` arm surfaces every fail-loud case (no recorded SHAs, no authored
    scenarios, a declared capture slot with no file) as a non-zero exit with no
    host side effect.
    """


@dataclass(frozen=True, slots=True)
class SeamsRun:
    """The run facts ``--from-seams`` folds, resolved from the recipe + CLI overrides."""

    ticket_number: str
    spec_path: str
    artifacts_root: Path
    env: str
    per_repo_shas: dict[str, str]


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


def _render_scenario(host: CodeHostBackend, *, repo: str, scenario: Scenario, run: SeamsRun) -> RenderScenario:
    """Map one authored :class:`Scenario` to the render ``TypedDict``, uploading its captures."""
    images: list[ScenarioImage] = []
    if not scenario.is_api:
        for capture in scenario.captures:
            filepath = resolve_capture_file(run, slot=capture.slot)
            image_md = _verified_embed(
                host, repo=repo, filepath=str(filepath), label=f"{scenario.surface} — {capture.slot}"
            )
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


def assemble_scenario_state(
    host: CodeHostBackend, *, repo: str, title: str, scenarios: tuple[Scenario, ...], run: SeamsRun
) -> TestPlanState:
    """Fold the authored scenarios + the run's captures + the recorded SHAs into a note state."""
    state = empty_state(ticket=run.ticket_number, title=title)
    state["template"] = _SCENARIO_TEMPLATE
    state["scenarios"] = [_render_scenario(host, repo=repo, scenario=scenario, run=run) for scenario in scenarios]
    side = "dev" if run.env == "dev" else "local"
    state[side]["commits"] = dict(run.per_repo_shas)
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
    """The CLI inputs for ``post-test-plan --from-seams``: ticket + run overrides.

    ``spec_path`` / ``artifacts_dir`` default (empty) to the recipe's recorded
    ``last_run``; ``title`` overrides the note heading (empty → the issue URL).
    """

    ticket: str = ""
    spec_path: str = ""
    artifacts_dir: str = ""
    title: str = ""


def run_from_seams(
    request: FromSeamsRequest,
    *,
    write_out: Callable[[str], None],
    write_err: Callable[[str], None],
) -> PostTestPlanResult:
    """Assemble + post the ``scenario-plan`` note for a spec from the overlay seams.

    Resolves the ticket, folds ``overlay.e2e.scenarios(spec)`` + the run's
    captures + the recipe's recorded SHAs into the note, and creates-or-updates
    the single test-plan note through the on-behalf gate. Any fail-loud
    precondition writes to ``write_err`` and exits non-zero before any host side
    effect.
    """
    host = code_host_from_overlay()
    if host is None:
        write_err(NO_CODE_HOST_MESSAGE)
        raise SystemExit(1)
    try:
        result = _assemble_and_post(host, request)
    except (TestPlanValidationError, OnBehalfPostBlockedError) as err:
        write_err(str(err))
        raise SystemExit(1) from err
    write_out(f"  Test plan {result['action']} (from-seams) on {result['issue_url']} (comment {result['comment_id']}).")
    return result


def _assemble_and_post(host: CodeHostBackend, request: FromSeamsRequest) -> PostTestPlanResult:
    resolved_ticket = _resolve_ticket(request.ticket, _worktree_or_none())
    issue_url = str(resolved_ticket.issue_url)
    run = resolve_seams_run(resolved_ticket, spec_path=request.spec_path, artifacts_dir=request.artifacts_dir)
    scenarios = get_overlay().e2e.scenarios(run.spec_path)
    if not scenarios:
        msg = (
            f"Spec {run.spec_path!r} has no authored scenarios "
            "(overlay.e2e.scenarios returned none) — nothing to assemble."
        )
        raise FromSeamsError(msg)

    if on_behalf_block_message(issue_url, _ON_BEHALF_ACTION):
        raise OnBehalfPostBlockedError(issue_url, _ON_BEHALF_ACTION)
    upload_repo = host.repo_for_issue_url(issue_url)
    state = assemble_scenario_state(
        host, repo=upload_repo, title=request.title.strip() or issue_url, scenarios=scenarios, run=run
    )
    body = render_body(state)
    body = route_forge_write(
        forge=forge_from_url(issue_url), repo=upload_repo, text=body, action=_ON_BEHALF_ACTION, target=issue_url
    )
    existing = find_existing_note(host.list_issue_comments(issue_url=issue_url), ticket_id=run.ticket_number)
    match_id = existing.comment_id if existing else None
    result, action, comment_id = _create_or_update_note(host, issue_url=issue_url, match_id=match_id, body=body)
    notify_user_on_behalf_post(
        target=issue_url,
        action=_ON_BEHALF_ACTION,
        destination=issue_url,
        artifact_url=str(result.get("web_url") or result.get("html_url") or issue_url),
        summary=f"Test plan (from-seams, {run.env}) on {issue_url}",
    )
    return PostTestPlanResult(issue_url=issue_url, comment_id=comment_id, envs=[run.env], action=action)
