"""Durable, plain-language feature status for the check-in report."""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal, TypedDict, cast

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.utils import timezone

from teatree.core._checking_gather import pr_url_for
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.merge_clear import MergeAudit
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.ticket import Ticket
from teatree.core.models.worker_generation import WorkerGeneration, WorkerGenerationQuerySet
from teatree.core.overlay_loader import get_overlay
from teatree.core.ref_render import short_title
from teatree.core.review.mr_ci_state import ci_state_from_status
from teatree.core.review.mr_triage import CiState
from teatree.core.review.pr_inventory import PrInventoryEntry, PrInventoryError, merged_entries, read_inventory
from teatree.core.review.pr_next_action import UPDATE_BRANCH
from teatree.paths import teatree_source_root
from teatree.utils.git_remote_ops import remote_slug
from teatree.utils.git_run import git_env_without_overrides, run_with_status
from teatree.utils.run import SUBPROCESS_UNREACHABLE, run_bounded_group
from teatree.utils.slack_links import slack_permalink

FEATURE_CAP = 8
UNDEPLOYED_CARRYOVER_WINDOW = timedelta(days=7)
_CLICKABLE_SCHEMES = ("https://", "http://")
logger = logging.getLogger(__name__)
_NUMBERED_WORD = re.compile(r"(?<!\w)(?:#?\d+|v\d+)(?:\.\d+)*(?!\w)", re.IGNORECASE)
_LINK_LABEL_BRACKET = re.compile(r"([\[\]])")
_DIGIT_RUN = re.compile(r"\d{2,}")
_LANE_TITLE_PATTERNS = (
    ("Multi-user", re.compile(r"\b(?:multi[- ]user|tenant sharing)\b", re.IGNORECASE)),
    ("Agent communication", re.compile(r"\b(?:agent communication|mailbox|agent messages?)\b", re.IGNORECASE)),
    ("Coordinator", re.compile(r"\bcoordinator\b", re.IGNORECASE)),
    ("Webhooks", re.compile(r"\bwebhooks?\b", re.IGNORECASE)),
    ("Evals", re.compile(r"\b(?:evals?|evaluations?)\b", re.IGNORECASE)),
)


class FeatureLinkDict(TypedDict):
    label: str
    url: str
    state: str


class FeatureBlockDict(TypedDict):
    title: str
    merged: list[FeatureLinkDict]
    left: list[FeatureLinkDict]
    owner_waits: list[FeatureLinkDict]
    pending_delivery: int
    live: bool | Literal["partly"]


class StatusByFeatureDict(TypedDict):
    features: list[FeatureBlockDict]
    more: int
    not_yet_deployed: list[FeatureLinkDict]
    deploy_state: str


@dataclass(frozen=True, slots=True)
class FeatureLink:
    label: str
    url: str
    state: str = ""

    def to_dict(self) -> FeatureLinkDict:
        return {"label": self.label, "url": self.url, "state": self.state}

    def render(self) -> str:
        return markdown_link(self.label, self.url) if self.url else self.label


@dataclass(slots=True)
class FeatureBlock:
    title: str
    merged: list[FeatureLink] = field(default_factory=list)
    left: list[FeatureLink] = field(default_factory=list)
    owner_waits: list[FeatureLink] = field(default_factory=list)
    unlinked_owner_waits: int = 0
    live: bool | Literal["partly"] = False
    merge_shas: list[tuple[str, str]] = field(default_factory=list)
    earlier_undeployed: list[FeatureLink] = field(default_factory=list)
    pending_deployment: bool = False
    deployment_unknown: bool = False
    recent_id: int = 0

    def to_dict(self) -> FeatureBlockDict:
        return {
            "title": self.title,
            "merged": [item.to_dict() for item in self.merged],
            "left": [item.to_dict() for item in self.left],
            "owner_waits": [item.to_dict() for item in self.owner_waits],
            "pending_delivery": self.unlinked_owner_waits,
            "live": self.live,
        }

    def render(self, deploy_state: str) -> str:
        block_deploy_state = "unknown" if self.deployment_unknown else deploy_state
        if self.merged:
            merged = (
                "merged and live"
                if self.live is True
                else f"{_render_links(self.merged, 2)} merged; deployment {block_deploy_state}"
            )
        elif self.earlier_undeployed:
            merged = f"{_render_links(self.earlier_undeployed, 2)} merged earlier; deployment {block_deploy_state}"
        else:
            merged = "no merge since the last check"
        if self.left:
            left = _render_links(self.left, 2, with_state=True)
            next_step = "the factory has the next step"
        else:
            left = "nothing left"
            next_step = (
                "the coordinator has the next step"
                if (self.merged or self.earlier_undeployed) and self.live is not True
                else "done"
            )
        if self.owner_waits:
            waits = _render_links(self.owner_waits, 2)
            next_step = f"waiting for your Slack reply: {waits}"
        if self.unlinked_owner_waits:
            pending = "waiting for Slack delivery of your question"
            next_step = f"{next_step}; {pending}" if self.owner_waits else pending
        return f"- {self.title}: {merged}; {left}. {next_step}."


@dataclass(frozen=True, slots=True)
class StatusByFeature:
    features: list[FeatureBlock] = field(default_factory=list)
    more: int = 0
    not_yet_deployed: list[FeatureLink] = field(default_factory=list)
    deploy_state: str = "unknown"

    def to_dict(self) -> StatusByFeatureDict:
        return {
            "features": [feature.to_dict() for feature in self.features],
            "more": self.more,
            "not_yet_deployed": [item.to_dict() for item in self.not_yet_deployed],
            "deploy_state": self.deploy_state,
        }

    def render(self) -> list[str]:
        if not self.features:
            return []
        lines = ["Status by feature", *(feature.render(self.deploy_state) for feature in self.features)]
        if self.more:
            lines.append(f"…and {self.more} more features")
        if self.not_yet_deployed:
            names = _render_links(self.not_yet_deployed, 3)
            lines.append(f"Not yet deployed ({self.deploy_state}): {names}")
        return lines


def markdown_link(label: str, url: str) -> str:
    escaped = _LINK_LABEL_BRACKET.sub(r"\\\1", label)
    return f"[{escaped}]({url})"


def plain_title(title: str) -> str:
    return short_title(_DIGIT_RUN.sub("", _NUMBERED_WORD.sub("", title))).strip(" -:,.—") or "Work in progress"


def _render_links(items: list[FeatureLink], cap: int, *, with_state: bool = False) -> str:
    rendered = ", ".join(f"{item.render()} ({item.state})" if with_state else item.render() for item in items[:cap])
    return f"{rendered}, and {len(items) - cap} more" if len(items) > cap else rendered


def _feature_key(ticket: Ticket | None) -> str:
    if ticket is None:
        return ""
    if lane := _lane_name(ticket):
        return f"{ticket.overlay}:lane:{lane.casefold()}"
    return ticket.issue_url if ticket.repo_namespaced_key else ""


def _canonical_lane_name(name: str) -> str:
    for lane, pattern in _LANE_TITLE_PATTERNS:
        if pattern.fullmatch(name):
            return lane
    return plain_title(name).title()


def _lane_name(ticket: Ticket) -> str:
    extra = ticket.extra or {}
    labels = extra.get("labels")
    if isinstance(labels, list):
        for label in labels:
            if isinstance(label, str) and label.casefold().startswith("lane:"):
                name = label.partition(":")[2].replace("-", " ").strip()
                if name:
                    return _canonical_lane_name(name)
    issue_title = extra.get("issue_title")
    for title in (ticket.short_description, issue_title if isinstance(issue_title, str) else ""):
        for lane, pattern in _LANE_TITLE_PATTERNS:
            if pattern.match(title):
                return lane
    return ""


def _pr_label(ticket: Ticket | None, url: str) -> str:
    if ticket is None:
        return "the merged pull request"
    prs = (ticket.extra or {}).get("prs")
    entry = prs.get(url) if isinstance(prs, dict) else None
    title = entry.get("title") if isinstance(entry, dict) else None
    return plain_title(title if isinstance(title, str) and title else ticket.short_description)


def _pr_state(pr: PullRequest, observed: PrInventoryEntry | None) -> str:
    prs = (pr.ticket.extra or {}).get("prs")
    entry = prs.get(pr.url) if isinstance(prs, dict) else None
    cached_pipeline = entry.get("pipeline_status") if isinstance(entry, dict) else ""
    observed_pipeline = observed.ci.casefold() if observed is not None else ""
    pipeline = observed_pipeline if observed_pipeline not in {"", "unknown"} else cached_pipeline
    pipeline_status = pipeline if isinstance(pipeline, str) else ""
    ci_state = ci_state_from_status("success" if observed_pipeline == "green" else pipeline_status)
    if ci_state == CiState.FAILED:
        state = "tests failed"
    elif ci_state == CiState.PENDING:
        state = "waiting for the tests"
    elif observed is not None and (
        observed.decision in {"flag_conflict", "needs_branch_update"} or observed.next_task == UPDATE_BRANCH
    ):
        state = "needs a merge of main"
    elif ci_state == CiState.UNKNOWN and (pipeline or pr.state == PullRequest.State.APPROVED):
        state = "tests status unknown"
    elif pr.state == PullRequest.State.APPROVED:
        state = "ready to merge"
    elif pr.state == PullRequest.State.REVIEW_REQUESTED:
        state = "waiting for review"
    else:
        state = "being fixed"
    return state


def _fresh_pr_inventory(overlay_names: list[str]) -> dict[str, PrInventoryEntry]:
    now = timezone.now()
    try:
        inventory = read_inventory()
    except PrInventoryError:
        logger.warning("PR inventory unreadable during check-in; using recorded PR state", exc_info=True)
        inventory = ()
    snapshots = (
        snapshot
        for snapshot in inventory
        if snapshot.verified
        and now - snapshot.observed_at <= timedelta(minutes=30)
        and (
            not overlay_names
            or (snapshot.overlay.split(":", 2)[1] if snapshot.overlay.startswith("own:") else snapshot.overlay)
            in overlay_names
        )
    )
    return merged_entries(snapshots)


_ACTIVE_TICKET_STATES: dict[str, str] = {
    Ticket.State.WORK_STARTED: "being built",
    Ticket.State.PLAN_RECORDED: "being built",
    Ticket.State.CODED: "waiting for the tests",
    Ticket.State.TESTED: "waiting for review",
    Ticket.State.SELF_REVIEWED: "ready to open a pull request",
    Ticket.State.PR_OPENED: "waiting for review",
    Ticket.State.REVIEW_REQUESTED: "waiting for review",
}


def _deployment_states(merge_shas: set[str], serving_sha: str) -> dict[str, bool | None]:
    if not serving_sha or not merge_shas:
        return dict.fromkeys(merge_shas)
    try:
        repo = str(teatree_source_root())
        env = git_env_without_overrides()
        ancestors = run_with_status(repo=repo, args=["rev-list", serving_sha], env=env, timeout=3)
        objects = run_bounded_group(
            ["git", "-C", repo, "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            env=env,
            stdin_text="\n".join(sorted(merge_shas)) + "\n",
            expected_codes=None,
            timeout=3,
        )
    except (*SUBPROCESS_UNREACHABLE, ValueError):
        return dict.fromkeys(merge_shas)
    if ancestors.returncode or objects.returncode:
        return dict.fromkeys(merge_shas)
    deployed = set(ancestors.stdout.splitlines())
    types = dict(line.rsplit(" ", 1) for line in objects.stdout.splitlines() if " " in line)
    return {sha: sha in deployed if types.get(sha) == "commit" else None for sha in merge_shas}


def _add_owner_waits(block_for: Callable[[Ticket | None], FeatureBlock], overlay_names: list[str]) -> None:
    questions = DeferredQuestion.owner_pending().select_related("parked_task__ticket")
    if overlay_names:
        questions = questions.filter(Q(parked_task__isnull=True) | Q(parked_task__ticket__overlay__in=overlay_names))
    for question in questions:
        ticket = question.parked_task.ticket if question.parked_task else None
        url = slack_permalink(question.slack_channel, question.slack_ts)
        block = block_for(ticket)
        if url:
            block.owner_waits.append(
                FeatureLink(label=plain_title(question.question), url=url, state="waiting for your Slack reply")
            )
        else:
            block.unlinked_owner_waits += 1


def _clickable_ticket_url(ticket: Ticket) -> str:
    stored = (ticket.extra or {}).get("pr_urls")
    candidates = [ticket.issue_url, *(stored if isinstance(stored, list) else [])]
    return next((url for url in candidates if isinstance(url, str) and url.startswith(_CLICKABLE_SCHEMES)), "")


def _add_active_tickets(
    block_for: Callable[[Ticket | None], FeatureBlock], overlay_names: list[str], represented_ticket_ids: set[int]
) -> None:
    active_tickets = Ticket.objects.filter(role=Ticket.Role.AUTHOR, state__in=_ACTIVE_TICKET_STATES)
    if overlay_names:
        active_tickets = active_tickets.filter(overlay__in=overlay_names)
    for ticket in active_tickets.exclude(pk__in=represented_ticket_ids):
        if url := _clickable_ticket_url(ticket):
            block_for(ticket).left.append(
                FeatureLink(
                    label=plain_title(ticket.short_description), url=url, state=_ACTIVE_TICKET_STATES[ticket.state]
                )
            )


def _add_open_prs(block_for: Callable[[Ticket | None], FeatureBlock], overlay_names: list[str]) -> set[int]:
    prs = (
        PullRequest.objects.live()
        .filter(ticket__role=Ticket.Role.AUTHOR)
        .exclude(ticket__state__in=[Ticket.State.DELIVERED, Ticket.State.REVIEW_DELIVERED, Ticket.State.IGNORED])
        .select_related("ticket")
        .order_by("-pk")
    )
    if overlay_names:
        prs = prs.filter(ticket__overlay__in=overlay_names)
    inventory = _fresh_pr_inventory(overlay_names)
    represented: set[int] = set()
    for pr in prs:
        represented.add(pr.ticket_id)
        block_for(pr.ticket).left.append(
            FeatureLink(label=_pr_label(pr.ticket, pr.url), url=pr.url, state=_pr_state(pr, inventory.get(pr.url)))
        )
    return represented


def _overlay_code_host(overlay_name: str) -> str:
    try:
        return get_overlay(overlay_name).config.code_host or ""
    except (ImproperlyConfigured, ImportError, LookupError, AttributeError):
        return ""


def _carryover_merges(
    block_for: Callable[[Ticket | None], FeatureBlock],
    *,
    merged: list[tuple[MergeAudit, str, str]],
    local_slug: str,
    earlier: list[MergeAudit],
    states: dict[str, bool | None],
) -> tuple[list[FeatureLink], bool]:
    if not local_slug:
        return [], False
    recent_audit_ids = {audit.pk for audit, _slug, _host in merged}
    undeployed: list[FeatureLink] = []
    unknown = False
    for audit in earlier:
        if audit.pk in recent_audit_ids:
            continue
        slug = audit.repo_slug or audit.clear.slug
        if slug.casefold() != local_slug.casefold():
            continue
        state = states.get(audit.merged_sha)
        if state is True:
            continue
        ticket = cast("Ticket", audit.clear.ticket)
        url = pr_url_for(ticket, repo_slug=slug, pr_id=audit.clear.pr_id, code_host=_overlay_code_host(ticket.overlay))
        link = FeatureLink(label=_pr_label(ticket, url), url=url)
        block = block_for(ticket)
        block.earlier_undeployed.append(link)
        block.pending_deployment = True
        block.deployment_unknown = block.deployment_unknown or state is None
        undeployed.append(link)
        unknown = unknown or state is None
    return undeployed, unknown


def _recent_deployment(
    blocks: dict[str, FeatureBlock], *, local_slug: str, states: dict[str, bool | None]
) -> tuple[list[FeatureLink], bool]:
    undeployed: list[FeatureLink] = []
    unknown = False
    for block in blocks.values():
        live = [
            states.get(sha) if local_slug and slug.casefold() == local_slug.casefold() else None
            for slug, sha in block.merge_shas
        ]
        block.deployment_unknown = block.deployment_unknown or any(
            slug.casefold() != local_slug.casefold() for slug, _sha in block.merge_shas
        )
        unknown = unknown or any(
            state is None and slug.casefold() == local_slug.casefold()
            for (slug, _sha), state in zip(block.merge_shas, live, strict=True)
        )
        block.live = (
            True
            if live and all(state is True for state in live) and not block.earlier_undeployed
            else "partly"
            if True in live
            else False
        )
        block.pending_deployment = block.pending_deployment or any(
            slug.casefold() == local_slug.casefold() and state is not True
            for (slug, _sha), state in zip(block.merge_shas, live, strict=True)
        )
        undeployed.extend(
            item
            for item, (slug, _sha), state in zip(block.merged, block.merge_shas, live, strict=True)
            if slug.casefold() == local_slug.casefold() and state is not True
        )
    return undeployed, unknown


def _serving_sha() -> str:
    serving = WorkerGeneration.objects.serving()
    return serving.sha if serving is not None else ""


def _deployment_summary(
    block_for: Callable[[Ticket | None], FeatureBlock],
    blocks: dict[str, FeatureBlock],
    merged: list[tuple[MergeAudit, str, str]],
    overlay_names: list[str],
) -> tuple[list[FeatureLink], str]:
    serving_sha = _serving_sha()
    local_slug = remote_slug(repo=str(teatree_source_root()))
    earlier_qs = MergeAudit.objects.filter(
        clear__ticket__isnull=False, merged_at__gte=timezone.now() - UNDEPLOYED_CARRYOVER_WINDOW
    )
    if overlay_names:
        earlier_qs = earlier_qs.filter(clear__ticket__overlay__in=overlay_names)
    earlier = list(earlier_qs.select_related("clear", "clear__ticket")) if serving_sha and local_slug else []
    merge_shas = {
        audit.merged_sha
        for audit in earlier
        if (audit.repo_slug or audit.clear.slug).casefold() == local_slug.casefold() and audit.merged_sha
    }
    merge_shas.update(
        sha
        for block in blocks.values()
        for slug, sha in block.merge_shas
        if slug.casefold() == local_slug.casefold() and sha
    )
    states = _deployment_states(merge_shas, serving_sha)
    undeployed, unknown = _carryover_merges(
        block_for, merged=merged, local_slug=local_slug, earlier=earlier, states=states
    )
    recent_undeployed, recent_unknown = _recent_deployment(blocks, local_slug=local_slug, states=states)
    undeployed.extend(recent_undeployed)
    unknown = unknown or recent_unknown
    if WorkerGeneration.objects.filter(
        state=WorkerGeneration.State.STARTING,
        started_at__gte=timezone.now() - WorkerGenerationQuerySet.STARTING_LEASE_MARGIN,
    ).exists():
        deploy_state = "running"
    elif not serving_sha or not local_slug or unknown:
        deploy_state = "unknown"
    else:
        deploy_state = "waiting" if undeployed else "verified"
    return undeployed, deploy_state


def build_feature_status(
    *,
    merged: list[tuple[MergeAudit, str, str]],
    overlay_names: list[str],
) -> StatusByFeature:
    blocks: dict[str, FeatureBlock] = {}

    def block_for(ticket: Ticket | None) -> FeatureBlock:
        key = _feature_key(ticket)
        if key not in blocks:
            title = (
                _lane_name(ticket) or plain_title(ticket.short_description)
                if key and ticket is not None
                else "Other maintenance"
            )
            blocks[key] = FeatureBlock(title=title)
        if ticket is not None:
            blocks[key].recent_id = max(blocks[key].recent_id, ticket.pk)
        return blocks[key]

    for audit, repo_slug, code_host in merged:
        ticket = audit.clear.ticket
        url = pr_url_for(ticket, repo_slug=repo_slug, pr_id=audit.clear.pr_id, code_host=code_host)
        block = block_for(ticket)
        block.merged.append(FeatureLink(label=_pr_label(ticket, url), url=url))
        block.merge_shas.append((repo_slug, audit.merged_sha))

    represented_ticket_ids = {audit.clear.ticket_id for audit, _repo, _host in merged if audit.clear.ticket_id}

    represented_ticket_ids.update(_add_open_prs(block_for, overlay_names))

    _add_active_tickets(block_for, overlay_names, represented_ticket_ids)
    _add_owner_waits(block_for, overlay_names)

    undeployed, deploy_state = _deployment_summary(block_for, blocks, merged, overlay_names)
    ordered = sorted(
        blocks.items(),
        key=lambda item: (
            not bool(item[1].owner_waits or item[1].unlinked_owner_waits),
            not any(link.state == "tests failed" for link in item[1].left),
            not item[1].pending_deployment,
            not bool(item[1].left),
            -item[1].recent_id,
            not item[0],
            item[0],
        ),
    )
    features = [block for _key, block in ordered[:FEATURE_CAP]]
    return StatusByFeature(
        features=features,
        more=max(0, len(ordered) - FEATURE_CAP),
        not_yet_deployed=undeployed,
        deploy_state=deploy_state,
    )
