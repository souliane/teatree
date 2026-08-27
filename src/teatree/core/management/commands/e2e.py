"""E2E test commands: trigger CI, run from external repo, run from project."""

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Annotated, cast

import typer
from django_typer.management import command

from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.management.commands import _e2e_discovery as _disc
from teatree.core.management.commands import _e2e_in_tree as _in_tree
from teatree.core.management.commands import _e2e_lanes as _lanes
from teatree.core.management.commands import _e2e_resolvers as _resolvers
from teatree.core.management.commands import _e2e_run_workitem as _workitem
from teatree.core.management.commands import _e2e_runners as _runners
from teatree.core.management.commands._test_plan import committed_captures as _committed_captures
from teatree.core.management.commands._test_plan import from_seams as _from_seams
from teatree.core.management.commands._test_plan import tracked as _tracked_manifest
from teatree.core.management.commands._test_plan import write as _test_plan_write
from teatree.core.management.commands._test_plan.write import TestPlanValidationError
from teatree.core.management.refusal_exit import RefusalExitTyperCommand
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.utils.run import run_streamed

# Re-exports for back-compat with tests and external callers (#1322 split).
_ticket_frontend_projects = _disc.ticket_frontend_projects
_discover_frontend_port = _disc.discover_frontend_port
_resolve_linked_worktree = _disc.resolve_linked_worktree
_linked_env_cache = _disc.linked_env_cache
_compose_frontend_port = _disc.compose_frontend_port
_detect_local_port = _disc.detect_local_port
_clone_or_update_e2e_repo = _runners.clone_or_update_e2e_repo
_build_e2e_env = _runners.build_e2e_env
E2eBranchNotFoundError = _runners.E2eBranchNotFoundError
E2eSpecsRemoteUnreachableError = _runners.E2eSpecsRemoteUnreachableError
PlaywrightOptions = _runners.PlaywrightOptions


# Shared typer.Option declarations for ``write-test-plan``.
_SKIP_HELP = "User-authorised bypass of the capture preflight (red-box / duplicate gates). Not for routine use."
_SKIP_VALIDATION_OPTION = typer.Option(default=False, help=_SKIP_HELP)
_NO_VIDEO_HELP = "Accept a stills-only manifest (screenshots, no video). Refused by default — capture video:'on'."
_ALLOW_NO_VIDEO_OPTION = typer.Option(default=False, help=_NO_VIDEO_HELP)
_JSON_HELP = "Emit the written plan's {path, envs, action} as JSON on stdout (human summary -> stderr)."
_EMBED_HELP = "Commit the run's captures beside the plan (for a plan issued outside this repo) instead of citing them."
_EMBED_CAPTURES_OPTION = typer.Option(default=False, help=_EMBED_HELP)


@dataclass
class DispatchOptions:
    """Common flags forwarded from ``e2e run`` to the resolved runner.

    Bundles the runner-shared flags so internal dispatch methods stay below
    the project's per-function argument cap without per-call ``noqa``.
    """

    test_path: str = ""
    target: str = ""
    update_snapshots: bool = False
    docker: bool = True
    linked_to: int = 0
    branch: str = ""


# #4234: `trigger-ci` RETURNS its refusal — the seam is what stops a CI lane treating an
# un-triggered pipeline as triggered.
class Command(MachineOutputCommand, RefusalExitTyperCommand):
    """Run E2E specs and post their evidence — the overlay-agnostic e2e verbs."""

    @command()
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def run(  # noqa: PLR0913 — wide signature by design: each parameter is a distinct required input
        self,
        work_item: Annotated[
            str,
            typer.Argument(help="Ticket reference (pk, issue number, or issue URL) — the #794 keystone."),
        ] = "",
        test_path: str = "",
        *,
        at: str = "",
        target: str = "",
        update_snapshots: bool = False,
        docker: bool = True,
        linked_to: int = 0,
        branch: str = _runners.BRANCH_OPTION,
    ) -> str:
        """Run E2E tests — the one command that works for every overlay.

        ``work_item`` (the #794 keystone) is a Ticket reference — a pk, an
        issue number, or an issue URL. When given, ``e2e run <work-item>``
        resolves the work item by its Ticket natural key, applies the default
        environment ladder, auto-provisions at the resolved ref, runs, and
        records ``{sha, result, timestamp}`` to the DB-durable recipe so a
        rerun never re-discovers prerequisites serially. ``--at
        last-green|main`` overrides the ladder. When ``work_item`` is empty
        the legacy cwd-resolved behaviour is unchanged.

        Otherwise dispatches to the ``project`` runner (in-repo
        pytest-playwright) or the ``external`` runner (remote playwright repo)
        based on what the overlay's ``get_e2e_config()`` returns. The overlay
        declares ``"runner": "project"`` or ``"runner": "external"``; when
        absent, ``test_dir`` implies ``project`` and ``project_path`` implies
        ``external`` for compatibility.

        ``--target dev|qa|local`` selects the dual-env target and is forwarded to
        whichever runner handles the overlay (see ``external`` for semantics).
        ``--branch``/``--ref`` overrides the ``external`` runner's specs ref.

        ``--linked-to <ticket-pk>`` (#1322): when the e2e cache repo is not
        DB-linked to the backend worktree (a frequent shape for
        out-of-tree test repos), name the backend ticket explicitly so
        frontend discovery, ``COMPOSE_PROJECT_NAME``, and the env cache
        feeding ``e2e.env_extras`` all route at the linked stack.
        ``0`` means "no link" (default — back-compat).

        Runner-specific flags (``--repo``, ``--playwright-args``) stay on the
        explicit ``external`` subcommand to keep this entry point overlay-agnostic.
        """
        opts = DispatchOptions(
            test_path=test_path,
            target=target,
            update_snapshots=update_snapshots,
            docker=docker,
            linked_to=linked_to,
            branch=branch,
        )
        if work_item:
            return _workitem.run_work_item(
                work_item=work_item,
                at=at,
                test_path=opts.test_path,
                dispatch=lambda: self._dispatch_runner(opts),
                write_err=self.stderr.write,
            )
        return self._dispatch_runner(opts)

    def _dispatch_runner(self, opts: DispatchOptions) -> str:
        overlay = get_overlay()
        e2e_config = overlay.metadata.get_e2e_config()
        runner = e2e_config.get("runner") or self._infer_runner(e2e_config)
        if runner == "project":
            return self.project(
                test_path=opts.test_path,
                target=opts.target,
                docker=opts.docker,
                update_snapshots=opts.update_snapshots,
            )
        if runner == "external":
            return self.external(
                test_path=opts.test_path,
                target=opts.target,
                update_snapshots=opts.update_snapshots,
                linked_to=opts.linked_to,
                branch=opts.branch,
            )
        self.stderr.write(
            f"Overlay e2e_config has no runner ({e2e_config}). "
            "Set 'runner' to 'project' or 'external' in get_e2e_config().",
        )
        raise SystemExit(2)

    @staticmethod
    def _infer_runner(e2e_config: dict[str, str]) -> str:
        if "test_dir" in e2e_config or "settings_module" in e2e_config:
            return "project"
        if "project_path" in e2e_config:
            return "external"
        return ""

    @command(name="trigger-ci")
    def trigger_ci(self, branch: str = "") -> dict[str, object]:
        """Trigger E2E tests on a remote CI pipeline."""
        from teatree.core.backend_factory import ci_service_from_overlay  # noqa: PLC0415 — lazy command import

        overlay = get_overlay()
        config = overlay.metadata.get_e2e_config()
        if not config:
            self.stderr.write("  trigger-ci refused: no E2E config in the overlay (get_e2e_config).")
            raise SystemExit(1)

        ci = ci_service_from_overlay()
        if ci is None:
            self.stderr.write("  trigger-ci refused: no CI service configured.")
            raise SystemExit(1)

        project = config.get("project_path", overlay.metadata.get_ci_project_path())
        ref = branch or config.get("ref", "main")
        variables = {"E2E": "true"}
        return ci.trigger_pipeline(project=project, ref=ref, variables=variables)

    def _run_preflight(self, env: dict[str, str]) -> None:
        """Run overlay-declared preflight checks. Exit non-zero on first failure."""
        overlay = get_overlay()
        checks = overlay.e2e.preflight(customer=env.get("CUSTOMER") or None, base_url=env.get("BASE_URL") or None)
        for check in checks:
            try:
                check()
            except RuntimeError as exc:
                self.stderr.write(f"E2E preflight failed: {exc}")
                raise SystemExit(1) from exc

    def _require_frontend_port(self, worktree: Worktree, linked_ticket: Ticket | None) -> int:
        return _resolvers.require_frontend_port(worktree, linked_ticket, write=self.stderr.write)

    def _resolve_target_env(
        self,
        resolved_target: str,
        linked_ticket: Ticket | None,
    ) -> tuple[str | None, str | None, dict[str, str] | None]:
        return _resolvers.resolve_target_env(
            resolved_target,
            linked_ticket,
            write=self.stderr.write,
            require_port=self._require_frontend_port,
        )

    def _resolve_linked_ticket(self, linked_to: int) -> Ticket | None:
        return _resolvers.resolve_linked_ticket(linked_to, write=self.stderr.write)

    def _resolve_artifacts_dir(self, explicit: str) -> str:
        return _resolvers.resolve_artifacts_dir(explicit, write=self.stderr.write)

    def _resolve_target(self, target: str) -> str:
        return _resolvers.resolve_target(target, write=self.stderr.write)

    @command()
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def external(  # noqa: PLR0913 — wide signature by design: each parameter is a distinct required input
        self,
        test_path: str = "",
        *,
        repo: str = "",
        target: str = "",
        update_snapshots: bool = False,
        playwright_args: str = "",
        linked_to: int = 0,
        branch: str = _runners.BRANCH_OPTION,
        artifacts_dir: str = "",
        no_evidence: bool = False,
    ) -> str:
        """Run Playwright tests from an external specs repo (the overlay's own, or --repo).

        Two sources for the Playwright working directory (first match wins):

        - ``--repo <name>``: clone the named entry from the DB-home ``e2e_repos`` config and use its ``e2e_dir``.
        - else the overlay's ``get_e2e_config`` repo (its ``url`` cloned at ``ref``), when declared.

        ``--branch``/``--ref`` overrides the specs ref (the ``--repo`` default or the
        overlay ``ref``) to run from an open MR's branch.

        ``--target dev|qa|local`` is deterministic: remote targets keep the
        pre-set ``BASE_URL`` and never scan local ports; ``local`` always
        discovers the local frontend even if a stray ``BASE_URL`` is exported.
        Empty preserves back-compat: infer ``dev`` if ``BASE_URL`` is set, else ``local``.

        The resolved value is exported as ``T3_E2E_TARGET`` so a dual-mode
        spec branches on ``process.env.T3_E2E_TARGET`` rather than
        re-deriving the target from a ``BASE_URL`` host regex.

        Discovers the frontend port from docker-compose (or local process)
        and reads the tenant variant from the env cache.

        ``--linked-to <ticket-pk>`` (#1322): when the e2e cache repo's
        auto-registered worktree is not DB-linked to the backend stack
        (``auto:<branch>`` ticket, different ticket, or no worktree row at
        all), name the backend ticket explicitly. Discovery,
        ``COMPOSE_PROJECT_NAME``, and the env cache feeding
        ``e2e.env_extras`` all route at the linked stack. ``0`` means
        "no link" (default — back-compat with the resolved-worktree path).

        Extra Playwright flags (--config, --timeout, --grep, etc.) can be
        passed via --playwright-args: ``--playwright-args="--config x.ts --timeout 120000"``.
        The overlay also contributes per-spec args via
        ``e2e.playwright_args(test_path)`` (e.g. ``-c <config>`` chosen by
        the spec's lane); overlay args go first, an explicit ``--playwright-args``
        follows so a caller can override.

        The runner exports the out-of-repo ``T3_E2E_ARTIFACTS_DIR``
        (``--artifacts-dir`` overrides; refused when it resolves inside a repo)
        and the ``T3_E2E_CAPTURE_EVIDENCE`` flag (``--no-evidence`` opts out).
        """
        overlay_repo = _runners.overlay_e2e_repo(get_overlay().metadata.get_e2e_config())
        try:
            specs_path = _runners.resolve_external_specs_path(repo, branch, overlay_repo=overlay_repo)
        except _runners.E2eSpecsResolutionError as exc:
            self.stderr.write(str(exc))
            raise SystemExit(exc.exit_code) from exc
        except _runners.SpecsCheckoutBusyError as exc:
            self.stderr.write(str(exc))
            raise SystemExit(1) from exc

        linked_ticket = self._resolve_linked_ticket(linked_to)
        resolved_target = self._resolve_target(target)
        frontend_url, worktree_compose_project, env_cache_override = self._resolve_target_env(
            resolved_target,
            linked_ticket,
        )

        overlay_args = get_overlay().e2e.playwright_args(test_path)
        # shlex.split, not str.split: a quoted flag value (``--grep "smoke test"``)
        # must stay ONE Playwright argument, not fracture on the inner space (F3.6).
        caller_args = shlex.split(playwright_args) if playwright_args else []
        opts = PlaywrightOptions(
            test_path=test_path,
            update_snapshots=update_snapshots,
            extra=[*overlay_args, *caller_args],
        )
        env = _build_e2e_env(
            frontend_url,
            target=resolved_target,
            context=_runners.make_e2e_env_context(
                test_path,
                worktree_compose_project,
                env_cache_override,
                artifacts_dir=self._resolve_artifacts_dir(artifacts_dir),
                capture_evidence=not no_evidence,
            ),
        )

        self.stdout.write(f"  Running from: {specs_path}")
        self.stdout.write(f"  Target: {resolved_target}")
        self.stdout.write(f"  BASE_URL: {env['BASE_URL']}")
        if env.get("CUSTOMER"):
            self.stdout.write(f"  CUSTOMER: {env['CUSTOMER']}")

        self._run_preflight(env)

        cmd = ["npx", "playwright", "test", *opts.to_args()]
        rc = run_streamed(cmd, cwd=specs_path, env=env, check=False)
        if rc == 0:
            return "E2E passed."
        self.stderr.write(f"E2E failed (exit {rc}).")
        raise SystemExit(rc)

    @command()
    def project(
        self,
        test_path: str = "",
        *,
        target: str = "",
        docker: bool = True,
        update_snapshots: bool = False,
    ) -> str:
        """Run E2E tests from the project's own test directory.

        ``--target dev|qa|local`` is exported as ``T3_E2E_TARGET`` for the in-repo
        suite (same contract as the ``external`` runner); empty falls back to
        ``BASE_URL``-based inference. The runner also exports the out-of-repo
        ``T3_E2E_ARTIFACTS_DIR`` and the ``T3_E2E_CAPTURE_EVIDENCE`` flag on every
        managed run (#3331); the ``external`` runner carries the
        ``--artifacts-dir`` / ``--no-evidence`` overrides.

        Pass ``--update-snapshots`` to regenerate ``pytest-playwright-visual``
        baselines. Always do this inside the Docker image (the default) — the
        CI runner's Chromium renders fonts at different heights than macOS, so
        locally-generated baselines mismatch in CI.
        """
        opts = _runners.ProjectRunOptions(
            test_path=test_path,
            resolved_target=self._resolve_target(target),
            docker=docker,
            update_snapshots=update_snapshots,
            artifacts_dir=self._resolve_artifacts_dir(""),
            capture_evidence=True,
        )
        return _runners.run_project_suite(opts, write_err=self.stderr.write)

    @command(name="in-tree")
    def in_tree(self, test_path: str = "", *, config: str = "") -> str:
        """Run a Playwright config that lives in THIS checkout's e2e dir — no stack, no credentials.

        The third source beside ``project`` (the repo's own pytest suite) and
        ``external`` (a cloned specs repo run against a live stack). What
        distinguishes it is the absence of every precondition those two carry:
        no specs clone, no frontend port, no env cache, no tenant, no
        credentials. The run is exactly ``npx playwright test -c <config>
        [<filter>]`` in ``<checkout>/<e2e_dir>``, with the ambient environment
        untouched — so a browserless CI lane (a static-analysis or unit lane)
        reproduces locally byte-for-byte, in the CONTRIBUTOR's own worktree.

        The checkout is the one the command was invoked from, so a lane runs
        against the branch under review rather than a cached clone of the
        default ref.

        ``--test-path`` is the Playwright filter — a spec, a line-scoped spec
        (``x.spec.ts:42``) or a directory; omitted, the whole config runs.
        Repo-relative (``e2e/contrib/tests/x.spec.ts``), e2e-dir-relative
        (``contrib/tests/x.spec.ts``) and absolute forms all work.

        The config comes from the overlay's per-spec lane mapping
        (``e2e.playwright_args``); ``--config`` overrides it. When neither
        yields one the command REFUSES rather than let Playwright fall back to
        the default config, whose global setup typically logs in and aborts.
        """
        overlay = get_overlay()
        try:
            plan = _in_tree.resolve_run(
                test_path=test_path,
                config=config,
                e2e_dir=overlay.metadata.get_e2e_config().get("e2e_dir", "e2e"),
                overlay_args=overlay.e2e.playwright_args(test_path),
            )
        except _in_tree.InTreeResolutionError as exc:
            self.stderr.write(str(exc))
            raise SystemExit(2) from exc

        self.stdout.write(f"  Running from: {plan.run_dir}")
        self.stdout.write(f"  Command: {shlex.join(plan.command)}")

        rc = run_streamed(plan.command, cwd=plan.run_dir, check=False)
        if rc == 0:
            return "E2E passed."
        self.stderr.write(f"E2E failed (exit {rc}).")
        raise SystemExit(rc)

    @command(name="write-test-plan")
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def write_test_plan(  # noqa: PLR0913 — django-typer command: every param is a distinct user-facing CLI flag; the arg list IS the public `e2e write-test-plan` surface
        self,
        *,
        manifest: str = "",
        ticket: str = "",
        body_file: str = "",
        skip_validation: bool = _SKIP_VALIDATION_OPTION,
        allow_no_video: bool = _ALLOW_NO_VIDEO_OPTION,
        embed_captures: bool = _EMBED_CAPTURES_OPTION,
        json_output: Annotated[bool, typer.Option("--json", help=_JSON_HELP)] = False,
    ) -> _test_plan_write.PlanWriteResult:
        """Write (or update) the ticket's plan at ``test-plans/<repo>-<ticket>.md`` in the e2e repo.

        ONE file per ticket, in the repo that owns the specs it describes — the
        plan is reviewed and merged with them, never posted to the forge. A
        re-run merges the env(s) it supplies over what the file already records.
        ``--manifest`` is the JSON path/string and the plan's only content
        source (ticket, title, MRs, template, per-env commits + run instant, gap,
        captures); ``--ticket`` selects the issue; ``--skip-validation`` bypasses
        the capture preflight; ``--allow-no-video`` permits a stills-only
        manifest (refused by default); ``--body-file`` writes a pre-authored body
        verbatim (mutually exclusive with ``--manifest``); ``--embed-captures``
        commits the captures beside the plan for a plan issued outside this repo.
        Captures already committed beside the plan are re-validated on every
        write, so a hand-placed screenshot cannot skip the preflight.
        See :mod:`._test_plan.write`.
        """
        flags = _test_plan_write.TestPlanFlags(
            ticket=ticket,
            manifest=manifest,
            body_file=body_file,
            skip_validation=skip_validation,
            allow_no_video=allow_no_video,
            embed_captures=embed_captures,
        )
        result = _test_plan_write.run_write_test_plan(flags, write_err=self.stderr.write)
        self._emit_plan_write(result, json_output=json_output)
        return result

    @command(name="verify-plan-captures")
    def verify_plan_captures(
        self,
        *,
        plans_dir: str = "",
        skip_validation: bool = _SKIP_VALIDATION_OPTION,
    ) -> list[str]:
        """Verify every capture committed under ``test-plans/evidence/`` passes the preflight.

        The standing gate over evidence already in git — wire it into a repo's
        pre-commit config or CI so a capture placed beside a plan by hand meets
        the same red-box and duplicate bar ``write-test-plan`` enforces.
        ``--plans-dir`` defaults to ``test-plans`` under the directory ``t3`` was
        invoked from, and refuses loudly when that directory does not exist.
        Exits non-zero naming every offending evidence directory, and refuses
        outright when there is nothing to look at — an absent or image-less
        ``evidence`` tree included.
        """
        self.print_result = False
        try:
            root = Path(plans_dir) if plans_dir else _committed_captures.resolve_default_plans_dir()
            failures = _committed_captures.verify_plans_dir(root, skip=skip_validation)
        except TestPlanValidationError as err:
            self.stderr.write(str(err))
            raise SystemExit(1) from err
        for failure in failures:
            self.stderr.write(failure)
        if failures:
            raise SystemExit(1)
        self.stderr.write(f"  Committed captures under {root} all carry a highlight box and are distinct.")
        return failures

    @command(name="write-plan-from-seams")
    def write_plan_from_seams(
        self,
        *,
        ticket: str = "",
        spec_path: str = "",
        artifacts_dir: str = "",
        title: str = "",
        json_output: Annotated[bool, typer.Option("--json", help=_JSON_HELP)] = False,
    ) -> _test_plan_write.PlanWriteResult:
        """Assemble the ``scenario-plan`` file from the overlay seams instead of a manifest (#3329).

        Folds ``overlay.e2e.scenarios``, the run's captures, and the recipe's
        recorded SHAs. ``--spec-path`` / ``--artifacts-dir`` default to the
        recipe's recorded ``last_run``.
        """
        request = _from_seams.FromSeamsRequest(
            ticket=ticket, spec_path=spec_path, artifacts_dir=artifacts_dir, title=title
        )
        result = _from_seams.run_from_seams(request, write_err=self.stderr.write)
        self._emit_plan_write(result, json_output=json_output, source="from-seams")
        return result

    def _emit_plan_write(
        self, result: _test_plan_write.PlanWriteResult, *, json_output: bool, source: str = ""
    ) -> None:
        """Route a plan write through the machine-output seam: payload to stdout, summary to stderr."""
        self.print_result = False
        emit(
            result,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=_test_plan_write.summary_line(result, source=source),
        )

    @command(name="tracked-manifest")
    def tracked_manifest(self, *, manifest: str = "") -> str:
        """Print a manifest's authored half (run provenance stripped) for a private test repo to commit."""
        return _tracked_manifest.run_tracked_manifest(
            manifest=manifest, write_out=self.stdout.write, write_err=self.stderr.write
        )

    @command()
    def lanes(
        self,
        *,
        json_output: Annotated[
            bool, typer.Option("--json", help="Emit the {lane: [spec]} split as a JSON CI matrix.")
        ] = False,
        names: bool = False,
        lane: str = "",
    ) -> dict[str, list[str]]:
        """Emit the ``{lane: [spec, ...]}`` split derived from the overlay's registered specs (#3329).

        Core folds ``overlay.e2e.spec_paths()`` by ``run_provenance`` so a CI
        matrix derives from the manifest the overlay already registered.
        ``--json`` prints the object (a CI matrix); ``--names`` prints every spec
        one per line (a shell loop); ``--lane <n>`` filters to one lane.
        """
        self.print_result = False
        return _lanes.run_lanes(
            _lanes.LaneOptions(as_json=json_output, names=names, lane=lane),
            overlay=get_overlay(),
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
        )
