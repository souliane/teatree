"""Durable, plain-language feature status for the check-in report."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from django.db.models import Q
from django.utils import timezone

from teatree.core._checking_gather import pr_url_for
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.merge_clear import MergeAudit
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.ticket import Ticket
from teatree.core.models.worker_generation import WorkerGeneration, WorkerGenerationQuerySet
from teatree.core.ref_render import short_title
from teatree.paths import teatree_source_root
from teatree.utils.git_run import run_with_status
from teatree.utils.slack_links import slack_permalink

FEATURE_CAP = 8
_NUMBERED_WORD = re.compile(r"\S*\d\S*")


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
        return f"[{self.label}]({self.url})" if self.url else self.label


@dataclass(slots=True)
class FeatureBlock:
    title: str
    merged: list[FeatureLink] = field(default_factory=list)
    left: list[FeatureLink] = field(default_factory=list)
    owner_waits: list[FeatureLink] = field(default_factory=list)
    unlinked_owner_waits: int = 0
    live: bool | Literal["partly"] = False
    merge_shas: list[str] = field(default_factory=list)

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
        if self.merged:
            merged = (
                "merged and live"
                if self.live is True
                else f"{', '.join(item.render() for item in self.merged[:2])} merged; deployment {deploy_state}"
            )
        else:
            merged = "no merge since the last check"
        if self.left:
            left = ", ".join(f"{item.render()} ({item.state})" for item in self.left[:2])
            next_step = "the factory has the next step"
        else:
            left = "nothing left"
            next_step = "the coordinator has the next step" if self.merged and self.live is not True else "done"
        if self.owner_waits:
            waits = ", ".join(item.render() for item in self.owner_waits[:2])
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
            names = ", ".join(item.render() for item in self.not_yet_deployed[:3])
            lines.append(f"Not yet deployed ({self.deploy_state}): {names}")
        return lines


def plain_title(title: str) -> str:
    return short_title(_NUMBERED_WORD.sub("", title)).strip(" -:,.—") or "Work in progress"


def _feature_key(ticket: Ticket | None) -> str:
    return ticket.issue_url if ticket is not None and ticket.repo_namespaced_key else ""


def _pr_label(ticket: Ticket | None, url: str) -> str:
    if ticket is None:
        return "the merged pull request"
    prs = (ticket.extra or {}).get("prs")
    entry = prs.get(url) if isinstance(prs, dict) else None
    title = entry.get("title") if isinstance(entry, dict) else None
    return plain_title(title if isinstance(title, str) and title else ticket.short_description)


def _pr_state(pr: PullRequest) -> str:
    prs = (pr.ticket.extra or {}).get("prs")
    entry = prs.get(pr.url) if isinstance(prs, dict) else None
    pipeline = entry.get("pipeline_status") if isinstance(entry, dict) else ""
    if pipeline in {"failed", "pending", "running"}:
        return "waiting for the tests"
    if pr.state == PullRequest.State.APPROVED:
        return "ready to merge"
    if pr.state == PullRequest.State.REVIEW_REQUESTED:
        return "waiting for review"
    return "being fixed"


def _live_state(merged_sha: str, serving_sha: str) -> bool | None:
    if not serving_sha or not merged_sha:
        return None
    try:
        result = run_with_status(
            repo=str(teatree_source_root()), args=["merge-base", "--is-ancestor", merged_sha, serving_sha], timeout=3
        )
    except (OSError, ValueError):
        return None
    return {0: True, 1: False}.get(result.returncode)


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


def build_feature_status(
    *,
    merged: list[tuple[MergeAudit, str, str]],
    overlay_names: list[str],
) -> StatusByFeature:
    blocks: dict[str, FeatureBlock] = {}

    def block_for(ticket: Ticket | None) -> FeatureBlock:
        key = _feature_key(ticket)
        if key not in blocks:
            title = plain_title(ticket.short_description) if key and ticket is not None else "Other maintenance"
            blocks[key] = FeatureBlock(title=title)
        return blocks[key]

    for audit, repo_slug, code_host in merged:
        ticket = audit.clear.ticket
        url = pr_url_for(ticket, repo_slug=repo_slug, pr_id=audit.clear.pr_id, code_host=code_host)
        block = block_for(ticket)
        block.merged.append(FeatureLink(label=_pr_label(ticket, url), url=url))
        block.merge_shas.append(audit.merged_sha)

    prs = PullRequest.objects.live().select_related("ticket").order_by("-pk")
    if overlay_names:
        prs = prs.filter(ticket__overlay__in=overlay_names)
    for pr in prs:
        block_for(pr.ticket).left.append(
            FeatureLink(label=_pr_label(pr.ticket, pr.url), url=pr.url, state=_pr_state(pr))
        )

    _add_owner_waits(block_for, overlay_names)

    serving = WorkerGeneration.objects.serving()
    serving_sha = serving.sha if serving is not None else ""
    starting = WorkerGeneration.objects.filter(
        state=WorkerGeneration.State.STARTING,
        started_at__gte=timezone.now() - WorkerGenerationQuerySet.STARTING_LEASE_MARGIN,
    ).exists()
    undeployed: list[FeatureLink] = []
    unknown = False
    for block in blocks.values():
        live = [_live_state(sha, serving_sha) for sha in block.merge_shas]
        unknown = unknown or None in live
        block.live = True if live and all(state is True for state in live) else "partly" if True in live else False
        if block.merged and block.live is not True:
            undeployed.extend(block.merged)
    if starting:
        deploy_state = "running"
    elif not serving_sha or unknown:
        deploy_state = "unknown"
    else:
        deploy_state = "waiting" if undeployed else "verified"
    ordered = sorted(blocks.items(), key=lambda item: (not bool(item[1].merged), not item[0], item[0]))
    features = [block for _key, block in ordered[:FEATURE_CAP]]
    return StatusByFeature(
        features=features,
        more=max(0, len(ordered) - FEATURE_CAP),
        not_yet_deployed=undeployed,
        deploy_state=deploy_state,
    )
