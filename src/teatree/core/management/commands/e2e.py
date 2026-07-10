"""E2E test commands: trigger CI, run from external repo, run from project."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.core.intake.resolve import resolve_worktree
from teatree.core.management.commands import _e2e_discovery as _disc
from teatree.core.management.commands import _e2e_runners as _runners
from teatree.core.management.commands._test_plan import post as _test_plan_post
from teatree.core.management.commands._test_plan import tracked as _tracked_manifest
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.core.worktree.worktree_env import compose_project
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
PlaywrightOptions = _runners.PlaywrightOptions


# Shared typer.Option declarations for ``post-test-plan`` and its deprecated alias.
_MRS_HELP = "MR/PR URL(s) the test plan covers (repeat or comma-separate). Supplements the manifest's 'mrs'."
_MRS_OPTION = typer.Option(None, "--mrs", help=_MRS_HELP)
_SKIP_HELP = "User-authorised bypass of the image preflight (red-box / duplicate gates). Not for routine use."
_SKIP_VALIDATION_OPTION = typer.Option(default=False, help=_SKIP_HELP)
_TEMPLATE_HELP = "Body template: capture-matrix (default), browser-click-first, or link-api. Overrides the manifest's."
_TEMPLATE_OPTION = typer.Option("", "--template", help=_TEMPLATE_HELP)
_NO_VIDEO_HELP = "Post a stills-only manifest (screenshots, no video). Refused by default — capture video:'on' instead."
_ALLOW_NO_VIDEO_OPTION = typer.Option(default=False, help=_NO_VIDEO_HELP)


@dataclass
class DispatchOptions:
    """Common flags forwarded from ``e2e run`` to the resolved runner.

    Bundles the runner-shared flags so internal dispatch methods stay below
    the project's per-function argument cap without per-call ``noqa``.
    """

    test_path: str = ""
    target: str = ""
    headed: bool = False
    update_snapshots: bool = False
    docker: bool = True
    linked_to: int = 0
    branch: str = ""


class Command(TyperCommand):
    @command()
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def run(  # noqa: PLR0913
        self,
        work_item: Annotated[
            str,
            typer.Argument(help="Ticket reference (pk, issue number, or issue URL) — the #794 keystone."),
        ] = "",
        test_path: str = "",
        *,
        at: str = "",
        target: str = "",
        headed: bool = False,
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
            headed=headed,
            update_snapshots=update_snapshots,
            docker=docker,
            linked_to=linked_to,
            branch=branch,
        )
        if work_item:
            return self._run_work_item(work_item, at=at, opts=opts)
        return self._dispatch_runner(opts)

    def _run_work_item(
        self,
        work_item: str,
        *,
        at: str,
        opts: DispatchOptions,
    ) -> str:
        """#794 keystone: resolve work item → ladder → run → record provenance.

        Deterministic outcome: either the e2e result, or a precise readiness
        failure naming the exact provisioning gap (which repo at which ref).
        Auto-provisioning of the missing repo set is the larger follow-up; the
        MVP runs an already-present workspace as-is and records the run's
        SHA-set + result to the durable recipe keyed by ``issue_url``.
        """
        from teatree.core.intake.e2e_workitem import (  # noqa: PLC0415
            record_run,
            resolve_environment,
            resolve_run_provenance,
        )
        from teatree.core.models import Ticket  # noqa: PLC0415
        from teatree.utils import git  # noqa: PLC0415

        try:
            ticket = Ticket.objects.resolve(work_item)
        except Ticket.DoesNotExist:
            self.stderr.write(
                f"No work item matching {work_item!r} (looked up by pk and issue_url). "
                "Provision it first: t3 <overlay> workspace ticket <issue_url>",
            )
            raise SystemExit(2) from None

        resolution = resolve_environment(ticket, at=at)
        if resolution.rung != "existing":
            refs = ", ".join(f"{repo}@{ref}" for repo, ref in sorted(resolution.provision_at.items()))
            self.stderr.write(
                f"E2E readiness failed for {ticket}: workspace not present on disk.\n"
                f"Ladder rung '{resolution.rung}' requires provisioning: {refs or '(no repos in recipe)'}.\n"
                "Provision the work item first: t3 <overlay> workspace ticket <issue_url>",
            )
            raise SystemExit(1)

        per_repo_shas: dict[str, str] = {}
        for repo, wt_path in resolution.repo_dirs.items():
            try:
                per_repo_shas[repo] = git.head_sha(repo=wt_path)
            except Exception:  # noqa: BLE001
                per_repo_shas[repo] = ""

        os.environ["T3_ORIG_CWD"] = next(iter(resolution.repo_dirs.values()))
        provenance = resolve_run_provenance(get_overlay(), opts.test_path)

        try:
            result = self._dispatch_runner(opts)
        except SystemExit as exc:
            record_run(ticket, result="red", per_repo_shas=per_repo_shas, provenance=provenance)
            raise SystemExit(exc.code) from exc
        record_run(ticket, result="green", per_repo_shas=per_repo_shas, provenance=provenance)
        return result

    def _dispatch_runner(self, opts: DispatchOptions) -> str:
        overlay = get_overlay()
        e2e_config = overlay.metadata.get_e2e_config()
        runner = e2e_config.get("runner") or self._infer_runner(e2e_config)
        if runner == "project":
            return self.project(
                test_path=opts.test_path,
                target=opts.target,
                headed=opts.headed,
                docker=opts.docker,
                update_snapshots=opts.update_snapshots,
            )
        if runner == "external":
            return self.external(
                test_path=opts.test_path,
                target=opts.target,
                headed=opts.headed,
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
        from teatree.core.backend_factory import ci_service_from_overlay  # noqa: PLC0415

        overlay = get_overlay()
        config = overlay.metadata.get_e2e_config()
        if not config:
            return {"error": "No E2E config in the overlay (get_e2e_config)."}

        ci = ci_service_from_overlay()
        if ci is None:
            return {"error": "No CI service configured."}

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
        port = _discover_frontend_port(worktree, linked_ticket=linked_ticket)
        if port is None:
            probed = ", ".join(_ticket_frontend_projects(worktree, linked_ticket=linked_ticket)) or "none"
            self.stderr.write(
                f"Frontend not running (no docker `frontend` service in [{probed}], "
                "no local process on 4200). Run `t3 <overlay> worktree start` first.",
            )
            raise SystemExit(1)
        return port

    def _resolve_target_env(
        self,
        resolved_target: str,
        linked_ticket: Ticket | None,
    ) -> tuple[str | None, str | None, dict[str, str] | None]:
        """Build the per-target trio passed to ``_build_e2e_env``."""
        if resolved_target in {"dev", "qa"}:
            if not os.environ.get("BASE_URL"):
                self.stderr.write(
                    f"--target {resolved_target} requires BASE_URL (the deployed environment URL) to be set.",
                )
                raise SystemExit(1)
            return None, None, None

        if linked_ticket is not None:
            linked_wt = _resolve_linked_worktree(linked_ticket)
            if linked_wt is not None:
                port = self._require_frontend_port(linked_wt, linked_ticket)
                return f"http://localhost:{port}", compose_project(linked_wt), _linked_env_cache(linked_wt)

        worktree = resolve_worktree()
        port = self._require_frontend_port(worktree, linked_ticket)
        return f"http://localhost:{port}", compose_project(worktree), None

    def _resolve_linked_ticket(self, linked_to: int) -> Ticket | None:
        """Resolve ``--linked-to <pk>`` to a Ticket or exit on misconfig.

        ``0`` means "no link" — return None (back-compat path). A non-zero
        pk that misses must fail fast: silently falling through would mask
        the user's intent to route at a specific backend stack.
        """
        if not linked_to:
            return None
        try:
            return Ticket.objects.get(pk=linked_to)
        except Ticket.DoesNotExist:
            self.stderr.write(
                f"--linked-to ticket pk={linked_to} not found. "
                "Pass the backend ticket's pk (see `t3 <overlay> ticket list`).",
            )
            raise SystemExit(2) from None

    def _resolve_target(self, target: str) -> str:
        """Resolve the dual-env target deterministically.

        Explicit values are ``dev`` / ``qa`` / ``local``. Empty preserves the
        back-compat inference: ``BASE_URL`` means remote ``dev``, else ``local``.
        """
        normalized = target.strip().lower()
        if normalized in {"dev", "qa", "local"}:
            return normalized
        if normalized:
            self.stderr.write(f"--target must be 'dev', 'qa', or 'local', got {target!r}.")
            raise SystemExit(2)
        return "dev" if os.environ.get("BASE_URL") else "local"

    @command()
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def external(  # noqa: PLR0913
        self,
        test_path: str = "",
        *,
        repo: str = "",
        target: str = "",
        headed: bool = False,
        update_snapshots: bool = False,
        playwright_args: str = "",
        linked_to: int = 0,
        branch: str = _runners.BRANCH_OPTION,
    ) -> str:
        """Run Playwright tests from an external repo (overlay repo, T3_PRIVATE_TESTS, or --repo).

        Three sources for the Playwright working directory (first match wins):

        - ``--repo <name>``: clone the named entry from the DB-home ``e2e_repos`` config and use its ``e2e_dir``.
        - else the overlay's ``get_e2e_config`` repo (its ``url`` cloned at ``ref``), when declared.
        - else the ``T3_PRIVATE_TESTS`` env var / the DB-home ``private_tests`` directory.

        ``--branch``/``--ref`` overrides a clone's specs ref (the ``--repo`` default or the
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
        """
        overlay_repo = _runners.overlay_e2e_repo(get_overlay().metadata.get_e2e_config())
        try:
            private_tests_path = _runners.resolve_external_specs_path(repo, branch, overlay_repo=overlay_repo)
        except _runners.E2eSpecsResolutionError as exc:
            self.stderr.write(str(exc))
            raise SystemExit(exc.exit_code) from exc

        linked_ticket = self._resolve_linked_ticket(linked_to)
        resolved_target = self._resolve_target(target)
        frontend_url, worktree_compose_project, env_cache_override = self._resolve_target_env(
            resolved_target,
            linked_ticket,
        )

        overlay_args = get_overlay().e2e.playwright_args(test_path)
        caller_args = playwright_args.split() if playwright_args else []
        opts = PlaywrightOptions(
            test_path=test_path,
            update_snapshots=update_snapshots,
            headed=headed,
            extra=[*overlay_args, *caller_args],
        )
        env = _build_e2e_env(
            frontend_url,
            headed=headed,
            target=resolved_target,
            context=_runners.make_e2e_env_context(test_path, worktree_compose_project, env_cache_override),
        )

        self.stdout.write(f"  Running from: {private_tests_path}")
        self.stdout.write(f"  Target: {resolved_target}")
        self.stdout.write(f"  BASE_URL: {env['BASE_URL']}")
        if env.get("CUSTOMER"):
            self.stdout.write(f"  CUSTOMER: {env['CUSTOMER']}")

        self._run_preflight(env)

        cmd = ["npx", "playwright", "test", *opts.to_args()]
        rc = run_streamed(cmd, cwd=private_tests_path, env=env, check=False)
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
        headed: bool = False,
        docker: bool = True,
        update_snapshots: bool = False,
    ) -> str:
        """Run E2E tests from the project's own test directory.

        ``--target dev|qa|local`` is exported as ``T3_E2E_TARGET`` for the in-repo
        suite (same contract as the ``external`` runner); empty falls back to
        ``BASE_URL``-based inference.

        Pass ``--update-snapshots`` to regenerate ``pytest-playwright-visual``
        baselines. Always do this inside the Docker image (the default) — the
        CI runner's Chromium renders fonts at different heights than macOS, so
        locally-generated baselines mismatch in CI.
        """
        resolved_target = self._resolve_target(target)
        try:
            worktree = resolve_worktree()
            wt_path = (worktree.extra or {}).get("worktree_path", ".") if worktree else "."
        except Exception:  # noqa: BLE001
            wt_path = "."
        overlay = get_overlay()
        e2e_config = overlay.metadata.get_e2e_config()
        settings_module = e2e_config.get("settings_module", "e2e.settings")
        test_dir = test_path or e2e_config.get("test_dir", "e2e/")

        if docker and not Path("/.dockerenv").exists():
            compose_file = Path(wt_path) / "dev" / "docker-compose.yml"
            if compose_file.is_file():
                cmd = [
                    "docker",
                    "compose",
                    "-f",
                    str(compose_file),
                    "run",
                    "--rm",
                    "-e",
                    f"T3_E2E_TARGET={resolved_target}",
                    "e2e",
                    test_dir,
                ]
                if update_snapshots:
                    cmd.append("--update-snapshots")
                rc = run_streamed(cmd, cwd=wt_path, check=False)
                if rc == 0:
                    return "E2E passed."
                self.stderr.write(f"E2E failed (exit {rc}).")
                raise SystemExit(rc)

        cmd = ["uv", "run", "pytest", test_dir]
        cmd.extend(["-o", f"DJANGO_SETTINGS_MODULE={settings_module}", "--no-cov", "-p", "no:tach", "-v"])
        if update_snapshots:
            cmd.append("--update-snapshots")

        env = {**os.environ, "DJANGO_SETTINGS_MODULE": settings_module, "T3_E2E_TARGET": resolved_target}
        if headed:
            env.pop("CI", None)
        else:
            env["CI"] = "1"

        rc = run_streamed(cmd, cwd=wt_path, env=env, check=False)
        if rc == 0:
            return "E2E passed."
        self.stderr.write(f"E2E failed (exit {rc}).")
        raise SystemExit(rc)

    @command(name="post-test-plan")
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def post_test_plan(  # noqa: PLR0913 — CLI entrypoint; each flag is a distinct user-facing option
        self,
        *,
        manifest: str = "",
        ticket: str = "",
        title: str = "",
        mrs: list[str] = _MRS_OPTION,
        skip_validation: bool = _SKIP_VALIDATION_OPTION,
        body_file: str = "",
        template: str = _TEMPLATE_OPTION,
        allow_no_video: bool = _ALLOW_NO_VIDEO_OPTION,
    ) -> _test_plan_post.PostTestPlanResult:
        """Post (or update) the ticket's single test-plan note from a manifest.

        ONE note per ticket (never an MR); a re-run merges the env(s) it
        supplies over the prior state. ``--manifest`` is the JSON path/string
        (ticket, MRs, per-env commits, gap, captures); ``--ticket`` selects the
        issue; ``--title`` overrides the heading; ``--template``
        (``capture-matrix`` / ``browser-click-first`` / ``link-api``) selects
        the body shape, overriding the manifest's ``template``;
        ``--skip-validation`` bypasses the image preflight; ``--allow-no-video``
        permits a stills-only manifest (refused by default); ``--body-file``
        posts a pre-authored body verbatim (no upload; mutually exclusive with
        ``--manifest``). See :mod:`._test_plan.post`. ``post-evidence`` is a hidden,
        deprecated alias.
        """
        return _test_plan_post.run_post_test_plan(
            manifest=manifest,
            ticket=ticket,
            title=title,
            mrs=mrs,
            skip_validation=skip_validation,
            write_out=self.stdout.write,
            write_err=self.stderr.write,
            body_file=body_file,
            template=template,
            allow_no_video=allow_no_video,
        )

    @command(name="tracked-manifest")
    def tracked_manifest(self, *, manifest: str = "") -> str:
        """Print a manifest's authored half (run provenance stripped) for a private test repo to commit."""
        return _tracked_manifest.run_tracked_manifest(
            manifest=manifest, write_out=self.stdout.write, write_err=self.stderr.write
        )

    @command(name="retract-evidence")
    def retract_evidence(
        self,
        *,
        ticket: str = "",
    ) -> None:
        """Withdraw the ticket's single test-plan note."""
        return _test_plan_post.run_retract_evidence(
            ticket=ticket,
            write_out=self.stdout.write,
            write_err=self.stderr.write,
        )

    @command(name="post-evidence", hidden=True, deprecated=True)
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def post_evidence(  # noqa: PLR0913 — CLI entrypoint, each flag is a distinct user-facing option
        self,
        *,
        manifest: str = "",
        ticket: str = "",
        title: str = "",
        mrs: list[str] = _MRS_OPTION,
        skip_validation: bool = _SKIP_VALIDATION_OPTION,
        body_file: str = "",
        template: str = _TEMPLATE_OPTION,
        allow_no_video: bool = _ALLOW_NO_VIDEO_OPTION,
    ) -> _test_plan_post.PostTestPlanResult:
        """Deprecated alias for ``post-test-plan`` (renamed; kept one release for back-compat)."""
        return self.post_test_plan(
            manifest=manifest,
            ticket=ticket,
            title=title,
            mrs=mrs,
            skip_validation=skip_validation,
            body_file=body_file,
            template=template,
            allow_no_video=allow_no_video,
        )
