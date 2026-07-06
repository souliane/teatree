"""Cross-repo "my open MRs" Slack reminder — assembly + channel routing (TODO-276).

Generalises a personal one-off reminder script into a reusable teatree
capability: list every open MR/PR the user authors across all repos one
code-host token can see, route each to a Slack channel by a configured
repo→channel map (:class:`~teatree.config_mr_reminder.MrReminderConfig`),
and assemble one mrkdwn message per channel.

This module is the pure domain core — it builds the per-channel messages
and never touches Slack. The thin ``t3 <overlay> mr-reminder`` management
command owns the only external boundary: posting each assembled message
through the messaging backend. Splitting assembly from egress keeps the
routing + rendering testable with no Slack mock at all (the routing is
deterministic; only the post is an unstoppable external).

Routing reuses the host-stripped leading-segment-prefix matcher
:func:`teatree.hooks._repo_visibility.slug_namespace_matches` (the same
grammar ``private_repos`` uses), so an organisation-namespace pattern
(``acme-engineering``) routes every ``acme-engineering/*`` repo and the
most-specific pattern wins when several match.
"""

from dataclasses import dataclass, field
from urllib.parse import urlparse

from teatree.config_mr_reminder import MrReminderConfig
from teatree.core.backend_protocols import CodeHostBackend
from teatree.hooks._repo_visibility import slug_namespace_matches
from teatree.slack_mrkdwn import normalize_slack_message
from teatree.types import RawAPIDict
from teatree.utils.url_slug import slug_from_issue_or_pr_url


def _str_field(data: RawAPIDict, *names: str) -> str:
    for name in names:
        value = data.get(name)
        if isinstance(value, str):
            return value
    return ""


def _int_field(data: RawAPIDict, *names: str) -> int:
    for name in names:
        value = data.get(name)
        if isinstance(value, int):
            return value
    return 0


def route_slug(slug: str, config: MrReminderConfig) -> str:
    """Return the Slack channel for repo *slug*, or ``""`` when unrouted.

    The most-specific configured pattern wins: among every
    ``slug_namespace_matches`` hit, the one with the most ``/``-separated
    segments is chosen, so an exact ``owner/repo`` entry beats the
    ``owner`` namespace entry that also matches. When no pattern matches,
    falls back to ``config.default_channel`` (``""`` keeps an unrouted MR
    out of every channel rather than guessing).
    """
    if not slug:
        return config.default_channel
    best_channel = ""
    best_specificity = -1
    for pattern, channel in config.channels:
        if slug_namespace_matches(pattern, slug):
            specificity = pattern.count("/")
            if specificity > best_specificity:
                best_specificity = specificity
                best_channel = channel
    return best_channel or config.default_channel


@dataclass(frozen=True, slots=True)
class MrLine:
    """One open MR/PR in the reminder: its repo, number, title, URL, status."""

    slug: str
    iid: int
    title: str
    url: str = ""
    status: str = ""

    def render(self) -> str:
        ref = f"!{self.iid}" if self.iid else "MR"
        label = f"{self.slug} {ref}" if self.slug else ref
        link = f"<{self.url}|{label}>" if self.url else label
        suffix = f" — {self.status}" if self.status else ""
        return f"- {link}: {self.title}{suffix}"


@dataclass(frozen=True, slots=True)
class ChannelMessage:
    """The assembled reminder for one Slack channel."""

    channel: str
    lines: tuple[MrLine, ...]

    def render(self, *, header: str = "Your open MRs") -> str:
        body = "\n".join(line.render() for line in self.lines)
        return normalize_slack_message(f"*{header}* ({len(self.lines)})\n{body}")


def _mr_line(pr: RawAPIDict) -> MrLine:
    url = _str_field(pr, "web_url", "html_url")
    slug = slug_from_issue_or_pr_url(urlparse(url).path) if url else ""
    return MrLine(
        slug=slug,
        iid=_int_field(pr, "iid", "number"),
        title=_str_field(pr, "title"),
        url=url,
        status=_str_field(pr, "state"),
    )


def _resolve_authors(host: CodeHostBackend, identities: tuple[str, ...]) -> tuple[str, ...]:
    if identities:
        return tuple(dict.fromkeys(identities))
    user = host.current_user()
    return (user,) if user else ()


def _collect_unique_prs(host: CodeHostBackend, authors: tuple[str, ...]) -> list[RawAPIDict]:
    seen_urls: set[str] = set()
    prs: list[RawAPIDict] = []
    for author in authors:
        for pr in host.list_my_prs(author=author):
            url = _str_field(pr, "web_url", "html_url")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            prs.append(pr)
    return prs


@dataclass(frozen=True, slots=True)
class MrReminder:
    """The full cross-repo reminder: per-channel messages + the unrouted leftovers."""

    messages: tuple[ChannelMessage, ...] = ()
    unrouted: tuple[MrLine, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return sum(len(m.lines) for m in self.messages) + len(self.unrouted)


def build_mr_reminder(
    host: CodeHostBackend,
    *,
    config: MrReminderConfig,
    identities: tuple[str, ...] = (),
) -> MrReminder:
    """Build the per-channel reminder from the user's open MRs across repos.

    Lists open MRs authored by each identity (deduped by URL), routes each
    to a channel via :func:`route_slug`, and groups them into one
    :class:`ChannelMessage` per destination. MRs whose slug routes nowhere
    (no pattern match and no ``default_channel``) collect in ``unrouted``
    so the caller can surface them rather than silently dropping work.

    Channel ordering follows first-appearance of each channel across the
    MR list, so the output is deterministic for a given MR ordering.
    """
    authors = _resolve_authors(host, identities)
    if not authors:
        return MrReminder()

    grouped: dict[str, list[MrLine]] = {}
    order: list[str] = []
    unrouted: list[MrLine] = []
    for pr in _collect_unique_prs(host, authors):
        line = _mr_line(pr)
        channel = route_slug(line.slug, config)
        if not channel:
            unrouted.append(line)
            continue
        if channel not in grouped:
            grouped[channel] = []
            order.append(channel)
        grouped[channel].append(line)

    messages = tuple(ChannelMessage(channel=channel, lines=tuple(grouped[channel])) for channel in order)
    return MrReminder(messages=messages, unrouted=tuple(unrouted))
