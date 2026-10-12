import json
from dataclasses import dataclass, field

from teatree.backends.github.pr_content import GhPrContentReader
from teatree.core.evidence.forge_content import ContentVerdictKind, PrContentSnapshot, classify_content

SLUG = "example-org/example-repo"
MERGE = "1" * 40
TIP = "2" * 40
OLD_HEAD = "3" * 40
NEW_HEAD = "4" * 40
OLD_BASE = "5" * 40
MERGE_BASE = "6" * 40
LIVE_HEAD = "7" * 40
LIVE_BASE = "8" * 40

BASE_TREE = {"src/a.py": "a0", "src/b.py": "b0", "docs/old.md": "o0"}
HEAD_TREE = {"src/a.py": "a1", "src/b.py": "b1", "src/new.py": "n1"}
STALE_HEAD_TREE = {"src/a.py": "a-stale"}
MAIN_TREE = {"src/a.py": "a9", "src/b.py": "b9", "docs/old.md": "o9"}

NO_COMMON_ANCESTOR = (
    1,
    json.dumps({"message": f"No common ancestor between {MERGE[:8]} and {NEW_HEAD}.", "status": "404"}),
    f"gh: No common ancestor between {MERGE[:8]} and {NEW_HEAD}. (HTTP 404)",
)


def _event(kind: str, before: str, after: str, at: str) -> dict[str, object]:
    return {"__typename": kind, "createdAt": at, "beforeCommit": {"oid": before}, "afterCommit": {"oid": after}}


def _graphql(*, head: str = MERGE, base: str = MERGE, merged: bool = True, events: list | None = None) -> str:
    timeline = (
        events
        if events is not None
        else [
            _event("HeadRefForcePushedEvent", OLD_HEAD, MERGE, "2026-10-01T10:00:00Z"),
            _event("BaseRefForcePushedEvent", OLD_BASE, MERGE, "2026-10-09T23:29:48Z"),
            _event("HeadRefForcePushedEvent", NEW_HEAD, MERGE, "2026-10-09T23:29:50Z"),
        ]
    )
    return json.dumps(
        {
            "data": {
                "repository": {
                    "defaultBranchRef": {"name": "main", "target": {"oid": TIP}},
                    "pullRequest": {
                        "merged": merged,
                        "mergeCommit": {"oid": MERGE} if merged else None,
                        "headRefOid": head,
                        "baseRefOid": base,
                        "timelineItems": {"nodes": timeline},
                    },
                }
            }
        }
    )


def _tree(blobs: dict[str, str], *, truncated: bool = False) -> str:
    entries = [{"path": path, "type": "blob", "sha": sha} for path, sha in blobs.items()]
    return json.dumps({"tree": [*entries, {"path": "src", "type": "tree", "sha": "d" * 40}], "truncated": truncated})


def _compare(status: str, merge_base: str = MERGE_BASE) -> tuple[int, str, str]:
    return 0, json.dumps({"status": status, "merge_base_commit": {"sha": merge_base}}), ""


@dataclass
class FakeGh:
    graphql: tuple[int, str, str] = (0, _graphql(), "")
    compares: dict[str, tuple[int, str, str]] = field(
        default_factory=lambda: {
            f"{MERGE}...{NEW_HEAD}": NO_COMMON_ANCESTOR,
            f"{OLD_BASE}...{NEW_HEAD}": _compare("diverged"),
            f"{TIP}...{NEW_HEAD}": NO_COMMON_ANCESTOR,
        }
    )
    trees: dict[str, tuple[int, str, str]] = field(
        default_factory=lambda: {
            MERGE_BASE: (0, _tree(BASE_TREE), ""),
            NEW_HEAD: (0, _tree(HEAD_TREE), ""),
            OLD_HEAD: (0, _tree(STALE_HEAD_TREE), ""),
            MERGE: (0, _tree(MAIN_TREE), ""),
            TIP: (0, _tree(MAIN_TREE), ""),
        }
    )
    tips: list[str] = field(default_factory=lambda: [TIP])
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        endpoint = argv[1]
        if endpoint == "graphql":
            return self.graphql
        prefix = f"repos/{SLUG}/"
        assert endpoint.startswith(prefix), endpoint
        path = endpoint.removeprefix(prefix)
        if path.startswith("compare/"):
            return self.compares.get(path.removeprefix("compare/"), (1, "", "gh: Not Found (HTTP 404)"))
        if path.startswith("git/trees/"):
            return self.trees[path.removeprefix("git/trees/").removesuffix("?recursive=1")]
        if path == "git/ref/heads/main":
            return 0, json.dumps({"object": {"sha": self.tips[0]}}), ""
        raise AssertionError(argv)


def _snapshot(gh: FakeGh) -> PrContentSnapshot:
    return GhPrContentReader(gh).snapshot(slug=SLUG, pr_id=41)


class TestRewrittenHead:
    def test_the_newest_head_force_push_is_the_content_head(self) -> None:
        snapshot = _snapshot(FakeGh())

        assert snapshot.unreadable == ""
        assert snapshot.content_head == NEW_HEAD
        assert snapshot.merge_commit == MERGE
        assert snapshot.default_tip == TIP

    def test_no_common_ancestor_retries_from_the_base_force_push(self) -> None:
        gh = FakeGh()

        snapshot = _snapshot(gh)

        assert snapshot.merge_base == MERGE_BASE
        compared = [argv[1].rsplit("/", 1)[-1] for argv in gh.calls if "/compare/" in argv[1]]
        assert compared[:2] == [f"{MERGE}...{NEW_HEAD}", f"{OLD_BASE}...{NEW_HEAD}"]

    def test_the_merge_commit_of_a_head_rewritten_to_its_base_is_not_a_delivery(self) -> None:
        snapshot = _snapshot(FakeGh())

        assert snapshot.merge_commit_on_default is False
        assert snapshot.content_head_on_default is False
        assert classify_content(snapshot).kind is ContentVerdictKind.ABSENT
        assert classify_content(snapshot).changed == ("docs/old.md", "src/a.py", "src/b.py", "src/new.py")

    def test_no_recorded_earlier_head_is_unreadable(self) -> None:
        gh = FakeGh(graphql=(0, _graphql(events=[]), ""))

        assert "no earlier head" in _snapshot(gh).unreadable

    def test_a_retry_that_still_finds_no_common_ancestor_is_unreadable(self) -> None:
        gh = FakeGh()
        gh.compares[f"{OLD_BASE}...{NEW_HEAD}"] = NO_COMMON_ANCESTOR

        assert _snapshot(gh).unreadable


class TestGenuineMerge:
    def _gh(self, *, merge_commit_status: str) -> FakeGh:
        return FakeGh(
            graphql=(0, _graphql(head=LIVE_HEAD, base=LIVE_BASE), ""),
            compares={
                f"{LIVE_BASE}...{LIVE_HEAD}": _compare("ahead"),
                f"{TIP}...{LIVE_HEAD}": _compare("diverged"),
                f"{TIP}...{MERGE}": _compare(merge_commit_status),
            },
            trees={
                MERGE_BASE: (0, _tree(BASE_TREE), ""),
                LIVE_HEAD: (0, _tree(HEAD_TREE), ""),
                MERGE: (0, _tree(MAIN_TREE), ""),
                TIP: (0, _tree(MAIN_TREE), ""),
            },
        )

    def test_a_head_that_carried_a_diff_is_its_own_content_head(self) -> None:
        snapshot = _snapshot(self._gh(merge_commit_status="diverged"))

        assert snapshot.content_head == LIVE_HEAD
        assert snapshot.merge_commit_on_default is False

    def test_a_merge_commit_still_in_the_default_history_is_carried(self) -> None:
        snapshot = _snapshot(self._gh(merge_commit_status="behind"))

        assert snapshot.merge_commit_on_default is True
        assert classify_content(snapshot).kind is ContentVerdictKind.CARRIED_BY_MERGE

    def test_a_content_head_in_the_default_history_is_present(self) -> None:
        gh = self._gh(merge_commit_status="diverged")
        gh.compares[f"{TIP}...{LIVE_HEAD}"] = _compare("identical")

        assert classify_content(_snapshot(gh)).kind is ContentVerdictKind.PRESENT_AT_TIP


class TestUnreadable:
    def test_a_graphql_errors_payload_is_unreadable(self) -> None:
        payload = json.dumps({"data": None, "errors": [{"message": "Something went wrong"}]})

        assert "Something went wrong" in _snapshot(FakeGh(graphql=(0, payload, ""))).unreadable

    def test_a_rate_limited_or_forbidden_read_is_unreadable(self) -> None:
        for failure in ((1, "", "gh: API rate limit exceeded (HTTP 429)"), (1, "", "gh: Forbidden (HTTP 403)")):
            gh = FakeGh()
            gh.trees[TIP] = failure
            assert _snapshot(gh).unreadable, failure
            assert _snapshot(FakeGh(graphql=failure)).unreadable, failure

    def test_a_truncated_tree_is_unreadable(self) -> None:
        gh = FakeGh()
        gh.trees[NEW_HEAD] = (0, _tree(HEAD_TREE, truncated=True), "")

        assert "truncated" in _snapshot(gh).unreadable

    def test_a_compare_that_fails_for_another_reason_is_unreadable(self) -> None:
        gh = FakeGh()
        gh.compares[f"{TIP}...{NEW_HEAD}"] = (1, "", "gh: Server Error (HTTP 502)")

        assert _snapshot(gh).unreadable

    def test_a_compare_status_it_does_not_know_is_unreadable(self) -> None:
        for status in ("unexpected", ""):
            gh = FakeGh()
            gh.compares[f"{TIP}...{NEW_HEAD}"] = _compare(status)

            snapshot = _snapshot(gh)

            assert f"unknown status {status!r}" in snapshot.unreadable
            assert classify_content(snapshot).kind is ContentVerdictKind.UNKNOWN

    def test_a_payload_that_does_not_say_merged_reads_as_not_merged(self) -> None:
        payload = json.loads(_graphql())
        del payload["data"]["repository"]["pullRequest"]["merged"]

        assert _snapshot(FakeGh(graphql=(0, json.dumps(payload), ""))).merged is False

    def test_a_default_branch_that_moved_during_the_probe_is_unreadable(self) -> None:
        gh = FakeGh(tips=["9" * 40])

        assert "moved" in _snapshot(gh).unreadable

    def test_an_unmerged_pr_reads_nothing_more(self) -> None:
        gh = FakeGh(graphql=(0, _graphql(merged=False, head=LIVE_HEAD, base=LIVE_BASE), ""))

        snapshot = _snapshot(gh)

        assert snapshot.merged is False
        assert len(gh.calls) == 1
