"""Cross-repo "my open MRs" reminder — routing + assembly (TODO-276).

The pure domain core: repo-slug → channel routing (most-specific-pattern
wins; organisation-namespace prefix; ``default_channel`` fallback) and
the per-channel message assembly across heterogeneous GitHub/GitLab MR
shapes. No Slack is touched here — the host is a hand-rolled stub that
returns canned ``list_my_prs`` payloads (the unstoppable forge boundary),
nothing else is mocked.
"""

from dataclasses import dataclass, field

from teatree.config_mr_reminder import MrReminderConfig
from teatree.core.review.mr_reminder import ChannelMessage, MrLine, build_mr_reminder, route_slug
from teatree.types import RawAPIDict


@dataclass
class StubHost:
    """A minimal CodeHostBackend stand-in: only the two methods the reminder calls."""

    prs: list[RawAPIDict] = field(default_factory=list)
    user: str = "me"
    calls: list[str] = field(default_factory=list)

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        self.calls.append(author)
        return list(self.prs)


_CONFIG = MrReminderConfig(
    channels=(("souliane/teatree", "C_TEATREE"), ("acme-engineering", "C_ACME")),
    default_channel="C_FALLBACK",
)


class TestRouteSlug:
    def test_exact_slug_routes_to_its_channel(self) -> None:
        assert route_slug("souliane/teatree", _CONFIG) == "C_TEATREE"

    def test_namespace_prefix_routes_every_repo_under_org(self) -> None:
        assert route_slug("acme-engineering/widget", _CONFIG) == "C_ACME"
        assert route_slug("acme-engineering/sub/deep", _CONFIG) == "C_ACME"

    def test_unmatched_slug_falls_back_to_default(self) -> None:
        assert route_slug("other-org/repo", _CONFIG) == "C_FALLBACK"

    def test_empty_default_drops_unmatched(self) -> None:
        cfg = MrReminderConfig(channels=(("a/b", "C_AB"),))
        assert route_slug("x/y", cfg) == ""

    def test_most_specific_pattern_wins_over_namespace(self) -> None:
        cfg = MrReminderConfig(
            channels=(("acme", "C_ORG"), ("acme/secret", "C_SECRET")),
            default_channel="C_FALLBACK",
        )
        assert route_slug("acme/secret", cfg) == "C_SECRET"
        assert route_slug("acme/public", cfg) == "C_ORG"

    def test_namespace_entry_does_not_match_substring_of_segment(self) -> None:
        cfg = MrReminderConfig(channels=(("acme", "C_ACME"),), default_channel="C_FALLBACK")
        # "acme" must NOT match "acme-fork/repo" (superset segment) — only "acme/*".
        assert route_slug("acme-fork/repo", cfg) == "C_FALLBACK"

    def test_empty_slug_falls_back_to_default(self) -> None:
        assert route_slug("", _CONFIG) == "C_FALLBACK"

    def test_equal_specificity_tie_keeps_first_match(self) -> None:
        # Two same-specificity (one-segment) namespace patterns both match;
        # the first configured wins (no later equal-specificity overwrite).
        cfg = MrReminderConfig(channels=(("org", "C_FIRST"), ("org", "C_SECOND")))
        assert route_slug("org/repo", cfg) == "C_FIRST"


class TestMrLineRender:
    def test_renders_clickable_slug_ref_title_and_status(self) -> None:
        line = MrLine(slug="o/r", iid=7, title="feat x", url="https://h/o/r/-/merge_requests/7", status="opened")
        rendered = line.render()
        assert "<https://h/o/r/-/merge_requests/7|o/r !7>" in rendered
        assert "feat x" in rendered
        assert "opened" in rendered

    def test_renders_without_url_as_plain_text(self) -> None:
        line = MrLine(slug="o/r", iid=7, title="feat x")
        assert line.render() == "- o/r !7: feat x"


class TestBuildMrReminder:
    def test_groups_mrs_by_routed_channel(self) -> None:
        host = StubHost(
            prs=[
                {"iid": 1, "title": "a", "web_url": "https://gitlab.com/souliane/teatree/-/merge_requests/1"},
                {"number": 2, "title": "b", "html_url": "https://github.com/acme-engineering/widget/pull/2"},
                {"iid": 3, "title": "c", "web_url": "https://gitlab.com/souliane/teatree/-/merge_requests/3"},
            ],
        )
        reminder = build_mr_reminder(host, config=_CONFIG)
        by_channel = {m.channel: m for m in reminder.messages}
        assert set(by_channel) == {"C_TEATREE", "C_ACME"}
        assert len(by_channel["C_TEATREE"].lines) == 2
        assert len(by_channel["C_ACME"].lines) == 1
        assert reminder.total == 3
        assert reminder.unrouted == ()

    def test_unrouted_mrs_collected_when_no_default(self) -> None:
        cfg = MrReminderConfig(channels=(("souliane/teatree", "C_TEATREE"),))
        host = StubHost(
            prs=[
                {"iid": 1, "title": "a", "web_url": "https://gitlab.com/souliane/teatree/-/merge_requests/1"},
                {"number": 2, "title": "b", "html_url": "https://github.com/random/repo/pull/2"},
            ],
        )
        reminder = build_mr_reminder(host, config=cfg)
        assert [m.channel for m in reminder.messages] == ["C_TEATREE"]
        assert len(reminder.unrouted) == 1
        assert reminder.unrouted[0].slug == "random/repo"

    def test_dedupes_duplicate_url_within_one_authors_list(self) -> None:
        url = "https://gitlab.com/souliane/teatree/-/merge_requests/1"
        host = StubHost(
            prs=[
                {"iid": 1, "title": "a", "web_url": url},
                {"iid": 1, "title": "a dup", "web_url": url},
                {"iid": 2, "title": "b", "web_url": "https://gitlab.com/souliane/teatree/-/merge_requests/2"},
            ],
        )
        reminder = build_mr_reminder(host, config=_CONFIG)
        assert reminder.total == 2

    def test_mr_without_url_routes_via_empty_slug_to_default(self) -> None:
        # A urlless MR has no stable identity to dedup on and an empty slug,
        # so it falls to default_channel (the slug-less leftover path).
        host = StubHost(prs=[{"iid": 8, "title": "no url"}])
        reminder = build_mr_reminder(host, config=_CONFIG)
        assert reminder.total == 1
        assert reminder.messages[0].channel == "C_FALLBACK"

    def test_mr_without_number_field_renders_with_zero_iid(self) -> None:
        host = StubHost(
            prs=[{"title": "no number", "web_url": "https://gitlab.com/souliane/teatree/-/merge_requests/5"}],
        )
        reminder = build_mr_reminder(host, config=_CONFIG)
        line = reminder.messages[0].lines[0]
        assert line.iid == 0
        assert (
            line.render() == "- <https://gitlab.com/souliane/teatree/-/merge_requests/5|souliane/teatree MR>: no number"
        )

    def test_dedupes_mrs_across_identities_by_url(self) -> None:
        url = "https://gitlab.com/souliane/teatree/-/merge_requests/1"
        host = StubHost(prs=[{"iid": 1, "title": "a", "web_url": url}])
        reminder = build_mr_reminder(host, config=_CONFIG, identities=("alias-1", "alias-2"))
        assert reminder.total == 1
        assert host.calls == ["alias-1", "alias-2"]

    def test_falls_back_to_current_user_when_no_identities(self) -> None:
        host = StubHost(
            prs=[{"iid": 1, "title": "a", "web_url": "https://gitlab.com/souliane/teatree/-/merge_requests/1"}],
            user="souliane",
        )
        build_mr_reminder(host, config=_CONFIG)
        assert host.calls == ["souliane"]

    def test_no_resolvable_author_yields_empty_reminder(self) -> None:
        host = StubHost(prs=[{"iid": 1, "title": "a"}], user="")
        reminder = build_mr_reminder(host, config=_CONFIG)
        assert reminder.messages == ()
        assert reminder.total == 0
        assert host.calls == []

    def test_channel_message_render_carries_all_lines(self) -> None:
        msg = ChannelMessage(
            channel="C1",
            lines=(MrLine(slug="o/r", iid=1, title="a", url="https://h/o/r/-/merge_requests/1"),),
        )
        rendered = msg.render(header="My MRs")
        assert "My MRs" in rendered
        assert "o/r !1" in rendered
