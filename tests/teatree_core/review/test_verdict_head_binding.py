"""Which head a returned review verdict binds to when the branch moved (#4737).

A dispatch pins the head SHA when it arms the review, and the factory pushes to its own
PRs while that review runs. The recorder could only compare the reviewer's asserted SHA
against the pinned one, so a reviewer that judged the PR's CURRENT head was refused
exactly like one that wandered onto an unrelated tree — measured across three PRs and
nine discarded verdicts, every one of them newer than the dispatch head.

The control these pin alongside the fix: a SHA that is on neither head is still refused.
"""

from teatree.core.modelkit.forge_readability import HEAD_SHA_UNREADABLE, LiveHeadRead
from teatree.core.modelkit.task_failure_taxonomy import HEAD_SUPERSEDED_PREFIX
from teatree.core.review.verdict_head_binding import resolve_verdict_head
from teatree.utils.pr_ref import PrRef

_SLUG = "souliane/teatree"
_PR_ID = 4716
_DISPATCH = "bf526560a1c4e7f80d329b6157ae4c02f8d1b3e9"
_LIVE = "21023d20d7f4b6c1590ae83f2d47c168b95a0e34"
_ELSEWHERE = "7c1d05e3aa4926fb8130d75e4f0c26a91bd83f57"


class _Probe:
    """A live-head probe recording its calls, so 'never read the forge' is assertable."""

    def __init__(self, read: LiveHeadRead) -> None:
        self.read = read
        self.calls: list[tuple[str, int, str]] = []

    def __call__(self, *, slug: str, pr_id: int, host_kind: str) -> LiveHeadRead:
        self.calls.append((slug, pr_id, host_kind))
        return self.read


def _resolve(asserted: str, probe: _Probe) -> object:
    return resolve_verdict_head(
        asserted=asserted,
        dispatch_head=_DISPATCH,
        pr=PrRef(slug=_SLUG, pr_id=_PR_ID, host_kind="github"),
        read_live_head=probe,
    )


def _moved() -> _Probe:
    return _Probe(LiveHeadRead(sha=_LIVE, unreadable=False))


def _unmoved() -> _Probe:
    return _Probe(LiveHeadRead(sha=_DISPATCH, unreadable=False))


class TestTheDispatchHeadStillBindsWithoutAForgeRead:
    def test_the_asserted_dispatch_head_binds_and_never_reads_the_forge(self) -> None:
        probe = _moved()

        binding = _resolve(_DISPATCH, probe)

        assert binding.head == _DISPATCH
        assert binding.error == ""
        assert probe.calls == []

    def test_an_abbreviated_dispatch_head_binds_to_the_full_head(self) -> None:
        probe = _moved()

        binding = _resolve(_DISPATCH[:12], probe)

        assert binding.head == _DISPATCH
        assert probe.calls == []

    def test_an_omitted_head_is_refused_naming_the_dispatch_head(self) -> None:
        probe = _moved()

        binding = _resolve("", probe)

        assert binding.head == ""
        assert "reviewed_sha" in binding.error
        assert _DISPATCH in binding.error
        assert not binding.superseded
        assert probe.calls == []


class TestAVerdictAtTheLiveHeadIsRecordedThere:
    def test_the_live_head_binds_when_the_branch_advanced(self) -> None:
        probe = _moved()

        binding = _resolve(_LIVE, probe)

        assert binding.head == _LIVE
        assert binding.error == ""
        assert probe.calls == [(_SLUG, _PR_ID, "github")]

    def test_an_abbreviated_live_head_binds_to_the_full_live_head(self) -> None:
        binding = _resolve(_LIVE[:12], _moved())

        assert binding.head == _LIVE
        assert binding.error == ""

    def test_a_sha_shorter_than_gits_abbreviation_floor_never_binds(self) -> None:
        # Six chars could prefix an unrelated tree; the floor is the whole guard.
        binding = _resolve(_LIVE[:6], _moved())

        assert binding.head == ""
        assert binding.error != ""


class TestAWanderedReviewerIsStillRefused:
    """The control: the fix must not read as 'accept whatever the reviewer says'."""

    def test_an_unrelated_sha_on_an_unmoved_head_is_refused(self) -> None:
        binding = _resolve(_ELSEWHERE, _unmoved())

        assert binding.head == ""
        assert _ELSEWHERE in binding.error
        assert _DISPATCH in binding.error
        assert not binding.superseded

    def test_an_unrelated_sha_on_a_moved_head_is_refused_as_superseded(self) -> None:
        binding = _resolve(_ELSEWHERE, _moved())

        assert binding.head == ""
        assert binding.error.startswith(HEAD_SUPERSEDED_PREFIX)
        assert binding.superseded


class TestAnUnreadableForgeRefusesRatherThanGuesses:
    def test_an_unreadable_live_head_is_refused_and_is_not_a_supersede(self) -> None:
        binding = _resolve(_ELSEWHERE, _Probe(LiveHeadRead(sha="", unreadable=True)))

        assert binding.head == ""
        assert binding.error != ""
        # Not superseded: nothing proved the head moved, so the claim must survive.
        assert not binding.superseded
        assert not binding.error.startswith(HEAD_SUPERSEDED_PREFIX)

    def test_the_unreadable_sentinel_never_arrives_as_a_bindable_head(self) -> None:
        probe = _Probe(LiveHeadRead(sha=HEAD_SHA_UNREADABLE, unreadable=True))

        binding = _resolve(HEAD_SHA_UNREADABLE, probe)

        assert binding.head == ""
        assert binding.error != ""
