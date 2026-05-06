import logging
import os
import subprocess  # noqa: S404
from pathlib import Path
from typing import TypedDict

from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.template.response import TemplateResponse
from django.views import View
from django_fsm import TransitionNotAllowed

from teatree.core.models import Session, Task, Ticket
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.views._startup import perform_sync
from teatree.utils.run import run_allowed_to_fail


class CancelTaskView(View):
    def post(self, request: HttpRequest, task_id: int) -> HttpResponse:
        try:
            with transaction.atomic():
                task = Task.objects.select_for_update().get(pk=task_id)
                if task.status in {Task.Status.COMPLETED, Task.Status.FAILED}:
                    return JsonResponse({"error": "Task already finished"}, status=409)
                if task.status == Task.Status.CLAIMED and request.POST.get("confirm") != "true":
                    return JsonResponse(
                        {"error": "Task is in progress. Pass confirm=true to cancel."},
                        status=409,
                    )
                task.fail()
        except Task.DoesNotExist:
            raise Http404 from None

        return JsonResponse({"task_id": task.pk, "status": task.status})


class ReopenTaskView(View):
    def post(self, _request: HttpRequest, task_id: int) -> HttpResponse:
        from django.db import transaction  # noqa: PLC0415

        try:
            with transaction.atomic():
                task = Task.objects.select_for_update().get(pk=task_id)
                task.reopen()
        except Task.DoesNotExist:
            raise Http404 from None
        except InvalidTransitionError as exc:
            return JsonResponse({"error": str(exc)}, status=409)

        return JsonResponse({"task_id": task.pk, "status": task.status})


class SyncFollowupView(View):
    def post(self, _request: HttpRequest) -> HttpResponse:
        result = perform_sync()
        return TemplateResponse(
            _request,
            "teatree/partials/sync_result.html",
            {"result": result},
        )


_ALLOWED_TRANSITIONS = {
    "scope",
    "start",
    "code",
    "test",
    "review",
    "ship",
    "request_review",
    "mark_merged",
    "retrospect",
    "mark_delivered",
    "rework",
    "ignore",
    "unignore",
}


class TicketTransitionView(View):
    def post(self, request: HttpRequest, ticket_id: int) -> HttpResponse:
        transition_name = request.POST.get("transition", "")
        if transition_name not in _ALLOWED_TRANSITIONS:
            return JsonResponse({"error": f"Unknown transition: {transition_name}"}, status=400)

        try:
            ticket = Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            raise Http404 from None

        method = getattr(ticket, transition_name, None)
        if method is None:
            return JsonResponse({"error": f"Invalid transition: {transition_name}"}, status=400)

        try:
            with transaction.atomic():
                method()
                ticket.save()
        except TransitionNotAllowed:
            return JsonResponse(
                {"error": f"Transition '{transition_name}' not allowed from state '{ticket.get_state_display()}'"},
                status=409,
            )

        return JsonResponse({"ticket_id": ticket.pk, "state": ticket.get_state_display()})


class CreateTaskView(View):
    def post(self, request: HttpRequest, ticket_id: int) -> HttpResponse:
        try:
            ticket = Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            raise Http404 from None

        phase = request.POST.get("phase", "coding")
        target = request.POST.get("target", Task.ExecutionTarget.HEADLESS)
        reason = request.POST.get("reason", "")

        session = Session.objects.filter(ticket=ticket).order_by("-pk").first()
        if session is None:
            session = Session.objects.create(ticket=ticket, agent_id="dashboard")

        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase=phase,
            execution_target=target,
            execution_reason=reason or f"Started from dashboard ({phase})",
        )

        # Headless tasks auto-enqueue via post_save signal
        if target == Task.ExecutionTarget.HEADLESS:
            return JsonResponse({"task_id": task.pk, "status": task.status})

        # Interactive: auto-launch if terminal params are provided
        terminal_mode = request.POST.get("terminal_mode", "")
        terminal_app = request.POST.get("terminal_app", "")
        if terminal_mode:
            return self._auto_launch(task, terminal_mode, terminal_app)

        return JsonResponse({"task_id": task.pk, "status": task.status})

    @staticmethod
    def _auto_launch(task: Task, terminal_mode: str, terminal_app: str) -> JsonResponse:
        try:
            task.claim(claimed_by="dashboard-auto-launch")
        except InvalidTransitionError as exc:
            return JsonResponse(
                {"task_id": task.pk, "status": task.status, "error": str(exc)},
                status=409,
            )

        try:
            from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415
            from teatree.core.views.launch import launch_interactive_task  # noqa: PLC0415

            overlay = get_overlay()
            skill_metadata = overlay.metadata.get_skill_metadata()
            return launch_interactive_task(
                task,
                skill_metadata,
                terminal_mode=terminal_mode,
                terminal_app=terminal_app,
            )
        except Exception:
            logging.getLogger(__name__).exception("Auto-launch failed for task %s", task.pk)
            task.complete_with_attempt(exit_code=1, error="Auto-launch failed")
            return JsonResponse({"error": "Auto-launch failed"}, status=500)


class _PullResult(TypedDict, total=False):
    ok: bool
    output: str
    error: str
    conflict: bool
    changed: bool


def _get_t3_repo() -> Path | None:
    """Resolve the teatree repo root from T3_REPO env var or package location."""
    env_path = os.environ.get("T3_REPO", "")
    if env_path:
        return Path(env_path).expanduser()
    from teatree import find_project_root  # noqa: PLC0415

    return find_project_root()


def _find_overlay_repo_dirs() -> list[tuple[str, Path]]:
    """Return (name, repo_root) for each loaded overlay with a local git repo."""
    import sys  # noqa: PLC0415

    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

    repos: list[tuple[str, Path]] = []
    try:
        overlays = get_all_overlays()
    except Exception:  # noqa: BLE001
        return repos
    for name, overlay in overlays.items():
        mod = sys.modules.get(type(overlay).__module__)
        if not mod or not getattr(mod, "__file__", None):
            continue
        mod_dir = Path(mod.__file__).parent  # type: ignore[arg-type]
        for parent in (mod_dir, *mod_dir.parents):
            if (parent / ".git").exists() or (parent / ".git").is_file():
                repos.append((name, parent))
                break
    return repos


_GIT_ENV = {**os.environ, "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true", "LC_ALL": "C"}

_TIMEOUT = 30

_PULL_NOOP_PREFIXES = ("Already up to date", "Already up-to-date")


def _git_pull_repo(repo_dir: Path) -> _PullResult:
    """Pull a single repo and return a result dict.

    Handles merge conflicts (aborts) and stale tracking branches
    (switches to main and retries).
    """
    try:
        result = run_allowed_to_fail(
            ["git", "pull"],
            cwd=repo_dir,
            env=_GIT_ENV,
            expected_codes=None,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timed out after 30s"}

    if result.returncode == 0:
        output = result.stdout.strip()
        changed = bool(output) and not output.startswith(_PULL_NOOP_PREFIXES)
        return {"ok": True, "output": output or "Already up to date.", "changed": changed}

    stderr = (result.stderr or result.stdout).strip()

    # Merge conflict — abort and report
    if "CONFLICT" in stderr or "fix conflicts" in stderr.lower():
        run_allowed_to_fail(
            ["git", "merge", "--abort"],
            cwd=repo_dir,
            env=_GIT_ENV,
            expected_codes=None,
            timeout=10,
        )
        return {"ok": False, "error": f"Merge conflict:\n{stderr}", "conflict": True}

    # Stale tracking branch — switch to main, pull, clean up
    if "no tracking information" in stderr or "doesn't have any remote" in stderr:
        switched = _switch_to_main_and_pull(repo_dir)
        if switched:
            return switched

    return {"ok": False, "error": stderr}


def _switch_to_main_and_pull(repo_dir: Path) -> _PullResult | None:
    """Switch to main branch, pull, and delete stale local branch."""
    # Find the stale branch name
    branch_result = run_allowed_to_fail(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_dir,
        expected_codes=None,
        timeout=10,
    )
    stale_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""

    for main_name in ("main", "master"):
        switch = run_allowed_to_fail(
            ["git", "checkout", main_name],
            cwd=repo_dir,
            env=_GIT_ENV,
            expected_codes=None,
            timeout=10,
        )
        if switch.returncode == 0:
            pull = run_allowed_to_fail(
                ["git", "pull"],
                cwd=repo_dir,
                env=_GIT_ENV,
                expected_codes=None,
                timeout=_TIMEOUT,
            )
            output = pull.stdout.strip() if pull.returncode == 0 else ""
            msg = f"Switched to {main_name}"
            if stale_branch and stale_branch != main_name:
                run_allowed_to_fail(
                    ["git", "branch", "-d", stale_branch],
                    cwd=repo_dir,
                    expected_codes=None,
                    timeout=10,
                )
                msg += f", deleted stale branch '{stale_branch}'"
            return {"ok": True, "output": f"{msg}. {output}".strip(), "changed": True}
    return None


class GitPullView(View):
    def post(self, _request: HttpRequest) -> HttpResponse:
        t3_repo = _get_t3_repo()
        if t3_repo is None or not t3_repo.is_dir():
            return JsonResponse({"error": "T3_REPO not found"}, status=400)

        results: dict[str, _PullResult] = {}

        # Pull teatree
        results["teatree"] = _git_pull_repo(t3_repo)

        # Pull overlay repos (skip if same as teatree)
        t3_resolved = t3_repo.resolve()
        for name, repo_dir in _find_overlay_repo_dirs():
            if repo_dir.resolve() == t3_resolved:
                continue
            results[name] = _git_pull_repo(repo_dir)

        # Build summary
        errors = {k: v for k, v in results.items() if not v.get("ok")}
        if errors:
            for name, err in errors.items():
                self._create_interactive_task(str(err.get("error", "")), name)
            return JsonResponse({"results": results, "errors": errors}, status=500)

        return JsonResponse({"ok": True, "results": results})

    @staticmethod
    def _create_interactive_task(error: str, repo_name: str) -> None:
        ticket, _created = Ticket.objects.get_or_create(
            overlay="teatree",
            issue_url="",
            defaults={"state": Ticket.State.STARTED},
        )
        session = Session.objects.create(ticket=ticket, agent_id="dashboard")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="maintenance",
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            execution_reason=f"git pull failed for {repo_name}:\n{error}",
        )


class SwitchBranchView(View):
    """Switch the teatree repo to a different branch. Uvicorn auto-reloads on file changes."""

    def get(self, _request: HttpRequest) -> HttpResponse:
        """Return list of local branches for the branch selector."""
        t3_repo = _get_t3_repo()
        if t3_repo is None or not t3_repo.is_dir():
            return JsonResponse({"error": "T3_REPO not found"}, status=400)

        try:
            result = run_allowed_to_fail(
                ["git", "branch", "--format=%(refname:short)"],
                cwd=t3_repo,
                expected_codes=None,
                timeout=10,
            )
            branches = [b.strip() for b in result.stdout.strip().split("\n") if b.strip()]
            current = run_allowed_to_fail(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=t3_repo,
                expected_codes=None,
                timeout=10,
            )
            current_branch = current.stdout.strip()
        except subprocess.TimeoutExpired:
            return JsonResponse({"error": "timeout"}, status=500)

        return JsonResponse({"branches": branches, "current": current_branch})

    def post(self, request: HttpRequest) -> HttpResponse:
        """Switch to the specified branch."""
        branch = request.POST.get("branch", "").strip()
        if not branch:
            return JsonResponse({"error": "No branch specified"}, status=400)

        t3_repo = _get_t3_repo()
        if t3_repo is None or not t3_repo.is_dir():
            return JsonResponse({"error": "T3_REPO not found"}, status=400)

        env = {**os.environ, "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}
        try:
            worktree_path = _branch_worktree_path(t3_repo, branch)
            if worktree_path:
                return JsonResponse(
                    {
                        "error": (
                            f"Branch '{branch}' is checked out at {worktree_path}. "
                            f"Run `t3 dashboard` from that worktree to test it."
                        ),
                        "worktree_path": worktree_path,
                    },
                    status=409,
                )
            result = run_allowed_to_fail(
                ["git", "checkout", branch],
                cwd=t3_repo,
                env=env,
                expected_codes=None,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            return JsonResponse({"error": "git operation timed out"}, status=500)

        if result.returncode != 0:
            error = (result.stderr or result.stdout).strip()
            return JsonResponse({"error": error}, status=500)

        return JsonResponse({"ok": True, "branch": branch})


def _branch_worktree_path(t3_repo: Path, branch: str) -> str:
    """Return the worktree path holding ``branch``, or empty string if none."""
    result = run_allowed_to_fail(
        ["git", "worktree", "list", "--porcelain"],
        cwd=t3_repo,
        expected_codes=None,
        timeout=10,
    )
    if result.returncode != 0:
        return ""

    target = f"branch refs/heads/{branch}"
    current_path = ""
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line.removeprefix("worktree ")
        elif line == target and current_path != str(t3_repo):
            return current_path
    return ""
