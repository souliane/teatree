"""Read-only Rule -1 proof for a checkout the writer fence refused."""

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

from teatree.core import forge_pr_probe
from teatree.core.cleanup.process_table import read_process_table
from teatree.core.modelkit.owner_action import OwnerActionKind
from teatree.core.models import Task, Worktree
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.question_text import question_fingerprint
from teatree.core.models.task_handoff import release_checkout_park
from teatree.core.models.worktree_occupancy import occupancy_holder
from teatree.core.worktree.checkout_attest import declined_attest_tasks, retire_card
from teatree.core.worktree.checkout_recovery import pr_owned_by_ticket
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.utils import git
from teatree.utils.forge import forge_from_remote
from teatree.utils.git_remote import host_from_remote
from teatree.utils.run import SUBPROCESS_UNREACHABLE, run_allowed_to_fail
from teatree.utils.url_slug import pr_ref_from_url

_CONFLICT_MARKER = re.compile(rb"(?m)^(?:<<<<<<< |>>>>>>> )")
_GIT_OPERATIONS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply")
_MAX_SCAN_BYTES = 8_000_000


class CheckoutAutoRefusedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CheckoutAutoProof:
    safe: bool
    reason: str
    pull_request: str = ""
    forge_head: str = ""


@dataclass(frozen=True, slots=True)
class _ForgeBranch:
    slug: str
    host: str
    head: str


def _docker(*args: str) -> str | None:
    try:
        result = run_allowed_to_fail(["docker", *args], expected_codes=None, timeout=15)
    except SUBPROCESS_UNREACHABLE:
        return None
    return result.stdout if result.returncode == 0 else None


def _container_checkout_path(mount: Mapping[str, object], checkout: Path) -> Path | None:
    source = mount.get("Source")
    destination = mount.get("Destination")
    if not isinstance(source, str) or not isinstance(destination, str):
        return None
    source_path = Path(os.path.realpath(source))
    if checkout.is_relative_to(source_path):
        return Path(destination) / checkout.relative_to(source_path)
    if source_path.is_relative_to(checkout):
        return Path(destination)
    return None


def _container_pids(container: str) -> list[str] | None:
    top = _docker("top", container, "-eo", "pid")
    if top is None:
        return None
    pids = [line.split(maxsplit=1)[0] for line in top.splitlines()[1:] if line.strip()]
    return pids if all(pid.isdigit() for pid in pids) else None


def _container_cwds_clear(container: str, visible: list[Path], checkout: Path, proc_root: Path) -> bool:
    pids = _container_pids(container)
    if pids is None:
        return False
    for pid in pids:
        process = proc_root / pid
        try:
            cwd = Path(process.joinpath("cwd").readlink())
        except FileNotFoundError:
            relisted = _container_pids(container)
            if process.exists() or relisted is None or pid in relisted:
                return False
            continue
        except OSError:
            return False
        if cwd.is_relative_to(checkout) or any(cwd.is_relative_to(mapped) for mapped in visible):
            return False
    return True


def _visible_checkout_paths(container: str, checkout: Path) -> list[Path] | None:
    raw_mounts = _docker("inspect", "--format", "{{json .Mounts}}", container)
    if raw_mounts is None:
        return None
    try:
        mounts = json.loads(raw_mounts)
    except ValueError:
        return None
    if not isinstance(mounts, list):
        return None
    return [
        mapped
        for mount in mounts
        if isinstance(mount, dict)
        if (mapped := _container_checkout_path(mount, checkout)) is not None
    ]


def container_processes_clear(checkout: Path, proc_root: Path) -> bool:
    inventory = _docker("ps", "-q", "--no-trunc")
    if inventory is None:
        return False
    for container in inventory.splitlines():
        visible = _visible_checkout_paths(container, checkout)
        if visible is None or (visible and not _container_cwds_clear(container, visible, checkout, proc_root)):
            return False
    return True


def _conflicts_clear(path: Path) -> bool:
    repo = str(path)
    if git.run_strict(repo=repo, args=["ls-files", "-u"]):
        return False
    for operation in _GIT_OPERATIONS:
        operation_path = git.run_strict(repo=repo, args=["rev-parse", "--git-path", operation])
        resolved_operation = Path(operation_path)
        if (resolved_operation if resolved_operation.is_absolute() else path / resolved_operation).exists():
            return False
    paths = set(filter(None, git.run_strict(repo=repo, args=["ls-files", "-z"]).split("\0")))
    paths.update(
        filter(None, git.run_strict(repo=repo, args=["ls-files", "--others", "--exclude-standard", "-z"]).split("\0"))
    )
    for relative in paths:
        candidate = path / relative
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if candidate.stat().st_size > _MAX_SCAN_BYTES or _CONFLICT_MARKER.search(candidate.read_bytes()):
            return False
    return True


def _refuse(message: str) -> NoReturn:
    raise CheckoutAutoRefusedError(message)


def _verified_forge_branch(path: Path, branch: str) -> _ForgeBranch:
    origin = git.remote_slug(repo=str(path))
    origin_url = git.remote_url(repo=str(path))
    origin_host = host_from_remote(origin_url)
    if not origin or not origin_host or forge_from_remote(origin_url) != "github":
        _refuse("the checkout's forge branch is unavailable")
    forge_head = forge_pr_probe.github_exact_branch_head(path, origin, branch, origin_host)
    if not forge_head:
        _refuse("the checkout's forge branch head could not be verified")
    return _ForgeBranch(origin, origin_host, forge_head)


def _verified_pr(task: Task, path: Path, branch: str, forge: _ForgeBranch) -> forge_pr_probe.GithubOpenPrHead:
    pr_head = forge_pr_probe.github_open_pr_head(path, branch)
    ref = pr_ref_from_url(pr_head.url) if pr_head is not None else None
    identity_matches = (
        pr_head is not None
        and ref is not None
        and ref.host_kind == "github"
        and ref.slug.casefold() == forge.slug.casefold()
        and forge.host == (urlsplit(pr_head.url).hostname or "").lower()
        and pr_head.repository.casefold() == forge.slug.casefold()
        and pr_head.branch == branch
    )
    if not identity_matches or pr_head is None or ref is None:
        _refuse("the ticket's pull request identity could not be verified")
    if not pr_owned_by_ticket(task, ref, pr_head.url):
        _refuse("the ticket does not own the pull request")
    if forge.head != pr_head.head:
        _refuse("the pull request and forge branch heads disagree")
    return pr_head


def _prove_checkout(task: Task, worktree: Worktree, state: CheckoutState) -> CheckoutAutoProof:
    path = Path(worktree.worktree_path)
    if not worktree.branch or state.branch != worktree.branch or state.branch == "HEAD":
        _refuse("the checkout is on the wrong branch")
    if occupancy_holder(worktree) is not None:
        _refuse("the checkout has an active holder")
    table = read_process_table(exclude_ancestors=True)
    if not table.usable or table.gaps or table.eviction_gaps or table.holds(path):
        _refuse("a host process is present or cannot be ruled out")
    if not container_processes_clear(path.resolve(), Path(table.source)):
        _refuse("a container process is present or cannot be ruled out")
    if not _conflicts_clear(path):
        _refuse("the checkout has conflict markers or a merge in progress")
    forge = _verified_forge_branch(path, state.branch)
    head = git.run_strict(repo=str(path), args=["rev-parse", "--verify", "HEAD"])
    clean_behind = (
        state.uncommitted == 0
        and head != forge.head
        and git.check(repo=str(path), args=["merge-base", "--is-ancestor", head, forge.head])
    )
    if clean_behind:
        if checkout_state(path, ticket=task.ticket).digest != state.digest:
            _refuse("the checkout changed while it was being checked")
        return CheckoutAutoProof(safe=True, reason="cleanly behind the forge branch", forge_head=forge.head)
    pr_head = _verified_pr(task, path, state.branch, forge)
    if not git.check(repo=str(path), args=["merge-base", "--is-ancestor", pr_head.head, head]):
        _refuse("the checkout neither contains nor is cleanly behind the pull request head")
    if checkout_state(path, ticket=task.ticket).digest != state.digest:
        _refuse("the checkout changed while it was being checked")
    return CheckoutAutoProof(
        safe=True,
        reason="contains the pull request head",
        pull_request=pr_head.url,
        forge_head=pr_head.head,
    )


def prove_checkout_for_rule1(task: Task, worktree: Worktree, state: CheckoutState) -> CheckoutAutoProof:
    try:
        return _prove_checkout(task, worktree, state)
    except CheckoutAutoRefusedError as exc:
        return CheckoutAutoProof(safe=False, reason=str(exc))
    except (OSError, RuntimeError, ValueError) as exc:
        return CheckoutAutoProof(safe=False, reason=f"the checkout proof could not be read: {exc}")


def _record_attestation(worktree: Worktree, state: CheckoutState, proof: CheckoutAutoProof) -> None:
    worktree.merge_extra(
        set_keys={
            "attested_state": state.digest,
            "rule1_checkout_audit": {
                "actor": "agent:checkout-rule1",
                "reason": proof.reason,
                "digest": state.digest,
                "pull_request": proof.pull_request,
                "forge_head": proof.forge_head,
                "at": timezone.now().isoformat(),
            },
        }
    )
    worktree.refresh_from_db()
    if worktree.get_extra().get("attested_state") != state.digest:
        _refuse("the checkout attestation could not be confirmed")


def resolve_checkout_park(task: Task, worktree: Worktree, state: CheckoutState) -> tuple[CheckoutAutoProof, str]:
    proof = (
        CheckoutAutoProof(safe=False, reason="the owner declined this checkout state")
        if task.pk in declined_attest_tasks(worktree, state.digest, task_id=task.pk)
        else prove_checkout_for_rule1(task, worktree, state)
    )
    marker = f"checkout:{question_fingerprint(f'{worktree.pk}:{state.digest}:{proof.safe}:{proof.reason}')}"
    with transaction.atomic():
        obsolete = (
            DeferredQuestion.owner_pending()
            .select_for_update()
            .filter(
                evidence__action__kind=OwnerActionKind.ATTEST,
                evidence__action__target=str(worktree.pk),
            )
        )
        for card in obsolete:
            retire_card(card, "The checkout decision was recorded under Rule -1", resolver_id="agent:checkout-rule1")
        row = DeferredQuestion.objects.filter(dedupe_marker=marker).order_by("-created_at", "-pk").first() or (
            DeferredQuestion.record("May this checkout state be used?", dedupe_marker=marker, parked_task=task)
        )
        if not row.is_pending:
            if row.answer_text == "Attested":
                if checkout_state(Path(worktree.worktree_path), ticket=task.ticket).digest != state.digest:
                    _refuse("the checkout changed before attestation")
                _record_attestation(worktree, state, proof)
                if not release_checkout_park(task):
                    _refuse("the checkout park changed before its answer could be applied")
                return proof, marker
            return (
                CheckoutAutoProof(safe=False, reason=row.dismissed_reason or "a prior decision skipped this state"),
                marker,
            )
        if not proof.safe:
            row.mark_stale(proof.reason, resolver_id="agent:checkout-rule1")
            return proof, marker
        if checkout_state(Path(worktree.worktree_path), ticket=task.ticket).digest != state.digest:
            row.mark_stale("the checkout changed before attestation", resolver_id="agent:checkout-rule1")
            return CheckoutAutoProof(safe=False, reason=row.dismissed_reason), marker
        _record_attestation(worktree, state, proof)
        applied = row.apply_answer("Attested", resolved_via=DeferredQuestion.ResolvedVia.DELEGATED)
        if applied is None or not release_checkout_park(task):
            _refuse("the checkout park changed before its answer could be applied")
        DeferredQuestionAudit.objects.create(
            question=applied,
            action="delegated_answer",
            answer_text="Attested",
            resolver_id="agent:checkout-rule1",
            note=f"Rule -1; {proof.reason}; PR {proof.pull_request} at {proof.forge_head}",
        )
        DeferredQuestion.objects.filter(pk=applied.pk, applied_at__isnull=True).update(applied_at=timezone.now())
    return proof, marker
