"""The solo-overlay sweep honours the standing substrate authorization (#3648).

The keystone/CLEAR path reads two standing opt-ins before it holds a substrate
merge — the explicit ``substrate_self_signoff`` setting and the config
delegation ``substrate_auto_merge_authorized_by``. The solo-overlay sweep path
held every substrate PR unconditionally, so the same PR under the same config
was merged by one path and blocked by the other.

These tests pin the fix from both ends: the sweep merges when a standing opt-in
is configured, still holds when none is, and — the regression a single-path test
would have missed — the sweep and the keystone reach the SAME authorization
decision for the same PR under the same config. The safety floor is asserted
explicitly: with no independent cold-review verdict at the live head, no standing
authorization merges a substrate PR through either path.
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from teatree.core.merge import MergePreconditionError, assert_merge_preconditions
from teatree.core.merge.substrate_standing import resolve_overlay_by_repo_identity, substrate_standing_authorization
from teatree.core.models import ConfigSetting, MergeClear, ReviewVerdict, Ticket
from teatree.loop.scanners.pr_sweep import PrSummary, PrSweepScanner
from teatree.loop.scanners.pr_sweep_adapters import NullMergeNotifier
from teatree.loop.scanners.pr_sweep_substrate import solo_overlay_substrate_authorized
from teatree.utils.pr_ref import PrRef

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

SLUG = "souliane/teatree"
OVERLAY = "t3-teatree"
HEAD = "c" * 40
MERGED_SHA = "abcdef1234567890abcdef1234567890abcdef12"
PR_ID = 3648
OWNER = "owner:standing"
SUBSTRATE_PATH = "src/teatree/core/merge/authorization.py"
_GREEN_ROLLUP = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'


def _gh_stub(argv: list[str]) -> tuple[int, str, str]:
    joined = " ".join(argv)
    if "headRefOid" in joined:
        return (0, HEAD, "")
    if "isDraft" in joined:
        return (0, "false", "")
    if "statusCheckRollup" in joined:
        return (0, _GREEN_ROLLUP, "")
    if "baseRefName" in joined or "required_status_checks" in joined:
        return (0, "main" if "baseRefName" in joined else '{"contexts": []}', "")
    if "pulls" in joined and "merge" in joined:
        return (0, '{"sha": "landed00deadbeef"}', "")
    return (0, "", "")


@pytest.fixture(autouse=True)
def _substrate_diff() -> Iterator[None]:
    with patch(
        "teatree.core.merge.ci_rollup.CodeHostQuery.pr_changed_paths",
        return_value=[SUBSTRATE_PATH],
    ):
        yield


@pytest.fixture(autouse=True)
def _required_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "teatree.core.merge.ci_rollup.CodeHostQuery.required_context_names",
        lambda *a, **k: {"test (3.13)"},
    )


@pytest.fixture(autouse=True)
def _repo_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.core.review.author_trust.repo_is_internal", lambda *a, **k: True)
    monkeypatch.setattr("teatree.core.merge.execution.assert_merge_provenance_trusted", lambda **_: None)


@contextmanager
def _teatree_owns_slug() -> Iterator[None]:
    """Pin ``SLUG`` to the bundled overlay so repo-identity resolution is deterministic."""
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: needs the app registry

    overlay = get_all_overlays()[OVERLAY]
    with patch.object(type(overlay), "get_workspace_repos", return_value=[SLUG]):
        yield


@contextmanager
def _config(**settings: object) -> Iterator[None]:
    rows = [ConfigSetting.objects.set_value(key, value, scope=OVERLAY) for key, value in settings.items()]
    try:
        yield
    finally:
        for row in rows:
            row.delete()


def _self_signoff_config() -> AbstractContextManager[None]:
    return _config(autonomy="full", substrate_self_signoff=True)


def _delegation_config() -> AbstractContextManager[None]:
    return _config(autonomy="full", substrate_auto_merge_authorized_by=OWNER)


def _no_optin_config() -> AbstractContextManager[None]:
    return _config(autonomy="full")


@dataclass(slots=True)
class _FakeApi:
    """``PrApiClient`` stand-in — the forge is the only unstoppable external here."""

    prs: list[PrSummary]
    merge_pr_calls: list[tuple[str, int, str]] = field(default_factory=list)

    def list_open_prs(self, *, slug: str) -> list[PrSummary]:
        return [pr for pr in self.prs if pr.slug == slug]

    def main_check_failed(self, *, slug: str, check_name: str) -> bool:
        return False

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> tuple[bool, str]:
        self.merge_pr_calls.append((slug, pr_id, expected_head_oid))
        return True, MERGED_SHA


@dataclass(slots=True)
class _FakeKeystone:
    """``MergeKeystone`` stand-in — the solo path never reaches it (no CLEAR row)."""

    calls: list[int] = field(default_factory=list)

    def merge_clear(self, *, clear_id: int, human_authorized: str = "") -> tuple[bool, str, str, str, str]:
        self.calls.append(clear_id)
        return True, MERGED_SHA, "", "", ""


@dataclass(slots=True)
class _FakePinger:
    calls: list[tuple[str, str]] = field(default_factory=list)

    def ping(self, *, text: str, idempotency_key: str) -> None:
        self.calls.append((text, idempotency_key))


def _open_pr() -> PrSummary:
    return PrSummary(
        slug=SLUG,
        number=PR_ID,
        head_sha=HEAD,
        is_draft=False,
        has_changes_requested=False,
        rollup=(
            {
                "__typename": "CheckRun",
                "name": "test (3.13)",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
        ),
        url=f"https://github.com/{SLUG}/pull/{PR_ID}",
        title="substrate change",
        author="souliane",
        same_repo=True,
    )


def _record_cold_review() -> ReviewVerdict:
    return ReviewVerdict.record(
        pr_id=PR_ID,
        slug=SLUG,
        reviewed_sha=HEAD,
        verdict="merge_safe",
        reviewer_identity="cold-reviewer",
    )


def _substrate_clear() -> MergeClear:
    ticket = Ticket.objects.create(
        overlay=OVERLAY,
        issue_url=f"https://github.com/{SLUG}/pull/{PR_ID}",
        state=Ticket.State.IN_REVIEW,
    )
    return MergeClear.objects.create(
        ticket=ticket,
        pr_id=PR_ID,
        slug=SLUG,
        reviewed_sha=HEAD,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.SUBSTRATE,
    )


def _sweep_merges(*, standing_authorizer: str = "") -> bool:
    """Run the solo-overlay sweep over one substrate PR; True iff it merged."""
    api = _FakeApi(prs=[_open_pr()])
    scanner = PrSweepScanner(
        repos=(SLUG,),
        api=api,
        keystone=_FakeKeystone(),
        notifier=NullMergeNotifier(),
        overlay=OVERLAY,
        solo_overlay=True,
        self_identities=("souliane",),
        substrate_pinger=_FakePinger(),
        substrate_standing_authorizer=standing_authorizer,
    )
    signals = scanner.scan()
    merged = [signal for signal in signals if signal.payload["merged"]]
    return bool(api.merge_pr_calls) and bool(merged)


def _keystone_refusal(*, human_authorized: str) -> str:
    """The keystone's refusal message for the equivalent substrate CLEAR, or ``""``."""
    clear = _substrate_clear()
    _record_cold_review()
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub):
        try:
            assert_merge_preconditions(
                clear=clear,
                executing_loop_identity="merge-loop",
                ref=PrRef(slug=SLUG, pr_id=PR_ID),
                human_authorized=human_authorized,
            )
        except MergePreconditionError as exc:
            return str(exc)
    return ""


def _keystone_authorizes(*, human_authorized: str = "") -> bool:
    """Run the keystone preconditions over the equivalent substrate CLEAR."""
    refusal = _keystone_refusal(human_authorized=human_authorized)
    # Anti-vacuity: a refusal must be the substrate hold, not an unrelated gate
    # tripping — otherwise "both paths blocked" would agree for the wrong reason.
    assert not refusal or "substrate" in refusal, f"refused for a non-substrate reason: {refusal}"
    return not refusal


class TestSweepHonoursStandingSubstrateAuthorization:
    """#3648: a standing opt-in lifts the sweep's substrate hold, exactly as it does the keystone's."""

    def test_self_signoff_optin_merges_substrate_through_the_sweep(self) -> None:
        # RED before the fix: the sweep held unconditionally, so a substrate PR
        # with a valid cold-review verdict never reached ``merge_pr_squash_bound``.
        _record_cold_review()
        with _teatree_owns_slug(), _self_signoff_config():
            assert _sweep_merges() is True

    def test_config_delegation_optin_merges_substrate_through_the_sweep(self) -> None:
        _record_cold_review()
        with _teatree_owns_slug(), _delegation_config():
            assert _sweep_merges(standing_authorizer=OWNER) is True

    def test_delegation_presented_id_must_match_the_configured_value(self) -> None:
        # Anti-vacuity: a stray presented id the config does not name authorizes
        # nothing — the delegation is config-sourced and revocable.
        _record_cold_review()
        with _teatree_owns_slug(), _delegation_config():
            assert _sweep_merges(standing_authorizer="someone-else") is False

    def test_no_standing_optin_still_holds_substrate(self) -> None:
        # Unchanged behaviour: with neither opt-in configured the sweep holds.
        _record_cold_review()
        with _teatree_owns_slug(), _no_optin_config():
            assert _sweep_merges() is False

    def test_self_signoff_below_full_autonomy_still_holds(self) -> None:
        _record_cold_review()
        with _teatree_owns_slug(), _config(autonomy="babysit", substrate_self_signoff=True):
            assert _sweep_merges() is False


class TestColdReviewFloorIsUnmoved:
    """The independent cold-review verdict stays a hard precondition in BOTH paths."""

    def test_sweep_never_merges_substrate_without_a_cold_review_verdict(self) -> None:
        # No ReviewVerdict recorded at the live head — the strongest standing
        # authorization must not merge. This is the floor, not the hold.
        with _teatree_owns_slug(), _self_signoff_config():
            assert _sweep_merges() is False

    def test_sweep_never_merges_on_a_stale_verdict(self) -> None:
        ReviewVerdict.record(
            pr_id=PR_ID,
            slug=SLUG,
            reviewed_sha="d" * 40,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
        )
        with _teatree_owns_slug(), _self_signoff_config():
            assert _sweep_merges() is False


class TestBothMergePathsAgree:
    """The regression a single-path test cannot catch: one policy, two paths.

    Drives the SAME PR and the SAME configuration through the solo-overlay sweep
    and the keystone/CLEAR path and asserts they reach the same authorization
    decision. The original bug is exactly a disagreement between these two.
    """

    def test_self_signoff_optin_authorizes_both_paths(self) -> None:
        _record_cold_review()
        with _teatree_owns_slug(), _self_signoff_config():
            sweep = _sweep_merges()
            keystone = _keystone_authorizes()
        assert (sweep, keystone) == (True, True)

    def test_config_delegation_authorizes_both_paths(self) -> None:
        _record_cold_review()
        with _teatree_owns_slug(), _delegation_config():
            sweep = _sweep_merges(standing_authorizer=OWNER)
            keystone = _keystone_authorizes(human_authorized=OWNER)
        assert (sweep, keystone) == (True, True)

    def test_no_optin_holds_on_both_paths(self) -> None:
        _record_cold_review()
        with _teatree_owns_slug(), _no_optin_config():
            sweep = _sweep_merges()
            keystone = _keystone_authorizes()
        assert (sweep, keystone) == (False, False)


class TestSharedPolicyFunction:
    """The two paths reuse ONE predicate — pinned directly on the extracted symbols."""

    def test_resolve_overlay_by_repo_identity_prefers_the_owning_overlay(self) -> None:
        with _teatree_owns_slug():
            assert resolve_overlay_by_repo_identity(SLUG, fallback="ignored") == OVERLAY

    def test_resolve_overlay_by_repo_identity_falls_back_to_the_stored_token(self) -> None:
        with _teatree_owns_slug():
            assert resolve_overlay_by_repo_identity("nobody/unclaimed", fallback=OVERLAY) == OVERLAY

    def test_substrate_standing_authorization_reports_the_self_signoff_grant(self) -> None:
        with _teatree_owns_slug(), _self_signoff_config():
            authorization = substrate_standing_authorization(overlay_name=OVERLAY)
        assert (authorization.self_signoff, authorization.delegated_by) == (True, "")
        assert bool(authorization) is True

    def test_substrate_standing_authorization_reports_the_matching_delegation(self) -> None:
        with _teatree_owns_slug(), _delegation_config():
            authorization = substrate_standing_authorization(overlay_name=OVERLAY, presented_authorizer=OWNER)
        assert authorization.delegated_by == OWNER
        assert bool(authorization) is True

    def test_substrate_standing_authorization_is_empty_without_an_optin(self) -> None:
        with _teatree_owns_slug(), _no_optin_config():
            authorization = substrate_standing_authorization(overlay_name=OVERLAY, presented_authorizer=OWNER)
        assert bool(authorization) is False

    def test_substrate_standing_authorization_on_an_unresolved_overlay_is_empty(self) -> None:
        assert bool(substrate_standing_authorization(overlay_name="  ", presented_authorizer=OWNER)) is False

    def test_solo_overlay_substrate_authorized_reads_the_shared_policy(self) -> None:
        with _teatree_owns_slug(), _self_signoff_config():
            authorized = solo_overlay_substrate_authorized(pr=_open_pr(), overlay=OVERLAY, presented_authorizer="")
        assert authorized is True

    def test_solo_overlay_substrate_authorized_is_false_without_an_optin(self) -> None:
        with _teatree_owns_slug(), _no_optin_config():
            authorized = solo_overlay_substrate_authorized(pr=_open_pr(), overlay=OVERLAY, presented_authorizer="")
        assert authorized is False
