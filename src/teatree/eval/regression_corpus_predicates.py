"""Real code-path predicates for the deterministic regression corpus.

Each ``_check_*`` function calls the REAL gate/checker code on a constructed
must-block input and a must-allow input and returns ``True`` only when both
directions hold. Split out of :mod:`teatree.eval.regression_corpus` to keep that
module under the module-health LOC cap; the corpus wires these predicates into
its ``RegressionCheck`` table and runs them.

The migration-fork predicate (``_count_core_leaves`` /
``_check_migration_graph_single_leaf``) deliberately stays in
``regression_corpus`` so its anti-vacuous test can patch the leaf counter on
that module's namespace.
"""

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from teatree.eval.regression_corpus_fixtures import (
    StubBackend,
    seed_repo_behind_but_clean,
    seed_repo_on_branch,
    seed_repo_with_diverging_target,
    unused_pid,
    without_git_overrides,
)
from teatree.eval.regression_corpus_fixtures import git as _git

_SHA_A = "a" * 40
_SHA_B = "b" * 40


@contextmanager
def _staged_overlay_autonomy(overlay_name: str, autonomy: str) -> Iterator[None]:
    """Run the block with *overlay_name*'s effective autonomy pinned to *autonomy*.

    An eval-isolation helper for assertions whose outcome depends on the
    overlay's effective autonomy (the substrate-merge carve-out). ``autonomy`` is
    DB-home under the #1775 partition — it resolves SOLELY from the
    ``ConfigSetting`` store, not from ``[overlays.<name>]`` TOML — so this stages
    it through that store's resolver seam rather than a hermetic ``~/.teatree.toml``
    (a ``[overlays.<name>]`` / ``[teatree]`` ``autonomy`` key is ignored on read
    now — its home is the DB).

    It pins the per-overlay DB scope for *overlay_name* (alias-tolerant) to the
    staged raw value — exercising the real ``_coerce_db_rows`` parser path — and
    neutralises the global DB scope and the env tier to ``{}`` for the block so a
    live ``ConfigSetting`` row on the host (an overlay such as ``t3-teatree``
    pinned to ``full``) or a ``T3_*`` env var cannot win over the staged value.
    All seams are restored on exit.
    """
    from unittest.mock import patch  # noqa: PLC0415

    from teatree.config.settings import OverlayEntry  # noqa: PLC0415

    canonical = OverlayEntry.canonical_overlay_name(overlay_name)

    def _staged_overlay_rows(name: str = "") -> dict[str, str]:
        return {"autonomy": autonomy} if OverlayEntry.canonical_overlay_name(name) == canonical else {}

    with (
        patch("teatree.config.resolution._load_global_rows", return_value={}),
        patch("teatree.config.resolution._load_overlay_rows", side_effect=_staged_overlay_rows),
        patch("teatree.config.resolution._env_setting_overrides", return_value={}),
    ):
        yield


def _check_branch_currency_conflict_only() -> bool:
    """§940: the CLEAR-side gate blocks ONLY on a real conflict, never behind-alone.

    Pre-fix the gate refused any behind branch (the behind-only ritual #940
    removed). The fixed ``sha_conflicts_with_target`` must:
    * return a finding when the reviewed SHA truly conflicts with the target, and
    * return ``None`` when the SHA is merely behind but conflict-free.
    """
    from teatree.core.worktree.branch_currency import sha_conflicts_with_target  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as raw:
        work = Path(raw)
        conflict_repo, conflict_sha = seed_repo_with_diverging_target(work)
        conflict = sha_conflicts_with_target(str(conflict_repo), conflict_sha, "origin/main")
        clean_repo, clean_sha = seed_repo_behind_but_clean(work)
        clean = sha_conflicts_with_target(str(clean_repo), clean_sha, "origin/main")
    return conflict is not None and bool(conflict.conflicting_paths) and clean is None


def _check_merge_precondition_substrate_human_authorize() -> bool:
    """Substrate floor: a below-full substrate CLEAR never merges without the recorded human authorizer.

    Exercises the real ``_assert_clear_authorized`` guard (the network-free
    §17.4.3 identity/substrate block) against an actionable, green,
    independently-reviewed substrate ``MergeClear``:
    * presenting no ``--human-authorized`` must RAISE (the floor holds), and
    * presenting the recorded authorizer must NOT raise on that guard.

    The CLEAR's overlay (resolved from its ``slug``) is pinned to ``babysit``
    via the DB-home autonomy seam (#1775) so the check is deterministic
    regardless of the developer's live config. Substrate is held under EVERY tier
    (including ``full`` — verified by
    :func:`_check_merge_precondition_substrate_full_autonomy_holds`); this check is
    the must-block direction for a below-full overlay.
    """
    return _exercise_substrate_authorize(autonomy="babysit", expect_cleared_without_human=False)


def _check_merge_precondition_substrate_full_autonomy_holds() -> bool:
    """Ping-and-hold: a substrate CLEAR under an ``autonomy = full`` overlay is HELD, never auto-merged.

    The owner's directive — substrate (merge keystone, architecture spec,
    governance doc) PINGS-and-HOLDS so they authorize every such merge. With the
    CLEAR's overlay pinned to ``full`` and NO ``--human-authorized`` presented, the
    standing grant does NOT cover substrate, so ``_assert_clear_authorized`` MUST
    raise (the loop edge then pings the owner). This is the inverse of the prior
    carve-out: substrate is excluded from the standing grant entirely, so a
    mislabeled or genuine substrate change can never auto-merge silently under
    full autonomy. The only path that clears it is a per-PR human authorizer.
    """
    return _exercise_substrate_authorize(autonomy="full", expect_cleared_without_human=False)


def _exercise_substrate_authorize(*, autonomy: str, expect_cleared_without_human: bool) -> bool:
    from teatree.core.merge import MergePreconditionError, _assert_clear_authorized  # noqa: PLC0415
    from teatree.core.models import MergeClear  # noqa: PLC0415
    from teatree.core.models.merge_clear import ClearRequest  # noqa: PLC0415
    from teatree.core.overlay_loader import infer_overlay_for_url  # noqa: PLC0415

    slug, pr_id, reviewer, executor = "souliane/teatree", 4242, "cold-reviewer", "loop-session"
    overlay_name = infer_overlay_for_url(slug) or "t3-teatree"
    clear = MergeClear.issue(
        ClearRequest(
            pr_id=pr_id,
            slug=slug,
            reviewed_sha=_SHA_A,
            reviewer_identity=reviewer,
            gh_verify_result="green",
            blast_class="substrate",
            human_authorizer="the-user",
            executing_loop_identity=executor,
        )
    )

    with _staged_overlay_autonomy(overlay_name, autonomy):
        try:
            _assert_clear_authorized(
                clear=clear,
                executing_loop_identity=executor,
                slug=slug,
                pr_id=pr_id,
            )
        except MergePreconditionError:
            cleared_without_human = False
        else:
            cleared_without_human = True

    return cleared_without_human is expect_cleared_without_human


def _check_merge_precondition_maker_is_not_checker() -> bool:
    """maker≠checker: a CLEAR self-issued by the executing loop is refused at merge time.

    A row written via ``.objects.create()`` bypasses the issue-time guard, so
    the merge-time ``_assert_clear_authorized`` re-check is the last line of
    defence — it must refuse a CLEAR whose reviewer equals the executing loop.
    """
    from teatree.core.merge import MergePreconditionError, _assert_clear_authorized  # noqa: PLC0415
    from teatree.core.models import MergeClear  # noqa: PLC0415

    slug, pr_id, identity = "souliane/teatree", 4343, "loop-session"
    clear = MergeClear.objects.create(
        pr_id=pr_id,
        slug=slug,
        reviewed_sha=_SHA_B,
        reviewer_identity=identity,
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
    )
    try:
        _assert_clear_authorized(
            clear=clear,
            executing_loop_identity=identity,
            slug=slug,
            pr_id=pr_id,
        )
    except MergePreconditionError:
        return True
    return False


def _check_loop_owner_lease_pid_anchored() -> bool:
    """#1604/#1722: an alive DIFFERENT-PROCESS foreign owner past its TTL is never hijacked.

    The pre-fix lease released on TTL lapse alone, so a fresh session stole a
    busy owner's loop. The pid-anchored ``claim_ownership`` must refuse a
    DIFFERENT-process foreign claim while the owner's pid is alive (even past
    TTL — a genuine hijack is always a different OS process), and grant the
    claim once the owner's pid is dead and the TTL has lapsed.

    A same-process claim with a rotated session id is NOT a hijack but a
    post-compaction self-reclaim (#2835), so the foreign owner here is modelled
    with a DIFFERENT alive pid (``os.getppid()``, the alive parent) than the
    claiming process (``os.getpid()``).
    """
    from datetime import timedelta  # noqa: PLC0415

    from django.utils import timezone  # noqa: PLC0415

    from teatree.core.models import LoopLease  # noqa: PLC0415

    name = "regression-lease"
    foreign_alive_pid = os.getppid()
    LoopLease.objects.filter(name=name).delete()
    LoopLease.objects.claim_ownership(name, session_id="owner-session", owner_pid=foreign_alive_pid, ttl_seconds=1800)
    # Force the TTL to have lapsed; only the alive foreign pid now protects the lease.
    LoopLease.objects.filter(name=name).update(lease_expires_at=timezone.now() - timedelta(seconds=10))
    won_against_alive, _ = LoopLease.objects.claim_ownership(
        name, session_id="thief-session", owner_pid=os.getpid(), ttl_seconds=1800
    )

    dead_pid = unused_pid()
    LoopLease.objects.filter(name=name).update(
        session_id="dead-owner",
        owner_pid=dead_pid,
        lease_expires_at=timezone.now() - timedelta(seconds=10),
    )
    won_against_dead, _ = LoopLease.objects.claim_ownership(
        name, session_id="successor-session", owner_pid=os.getpid(), ttl_seconds=1800
    )
    LoopLease.objects.filter(name=name).delete()
    return won_against_alive is False and won_against_dead is True


def _check_account_switch_detect_and_recover() -> bool:
    """#1916: the full `/login` switch-and-verify cycle, both directions.

    Drives the REAL :class:`AccountSwitchRecovery` under a hermetic home with
    the cache-reset and backends-provider seams stubbed (no network, no ``pass``).
    must-detect: active fingerprint B != recorded A → switch reported, cache
    invalidated, connectors re-probed, new account recorded. must-not-fire:
    active fingerprint == recorded → no switch, no cache reset. verify: a switch
    whose connector probes unreachable surfaces ``all_reachable is False``.

    Anti-vacuous: reverting detection (always ``switched=False``) fails the
    must-detect leg RED; a probe that ignored ``auth_test`` fails the verify leg.
    """
    from teatree.core.account_switch import AccountSwitchRecovery, record_fingerprint  # noqa: PLC0415

    reset_calls = {"n": 0}

    def _fake_reset() -> None:
        reset_calls["n"] += 1

    reachable = AccountSwitchRecovery(reset_caches=_fake_reset, backends=lambda: [StubBackend(ok=True)])
    unreachable_recovery = AccountSwitchRecovery(reset_caches=_fake_reset, backends=lambda: [StubBackend(ok=False)])

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / ".claude.json").write_text('{"oauthAccount": {"accountUuid": "uuid-B"}}', encoding="utf-8")

        same = reachable.run(home=home)  # records uuid-B (first run, no switch)
        if same.switched or reset_calls["n"] != 0:
            return False

        record_fingerprint("uuid-A", home=home)
        switched = reachable.run(home=home)
        if not (switched.switched and reset_calls["n"] == 1 and switched.all_reachable):
            return False

        record_fingerprint("uuid-A", home=home)
        unreachable = unreachable_recovery.run(home=home)

    return unreachable.switched and unreachable.all_reachable is False


def _check_private_repo_allowlist_path_segment_match() -> bool:
    """#1953: the private-repo allowlist matches PATH SEGMENTS, never a substring.

    Pre-fix the allowlist used case-insensitive substring containment, so an
    allowlisted org name appearing ANYWHERE in a PUBLIC slug (an alias-glued
    ``<org>-mirror/x`` owner) falsely downgraded it to private — relaxing the
    public-leak gate on a public surface. The fixed
    :func:`slug_is_allowlisted_private` (via :func:`slug_namespace_matches`) must:
    * match the allowlisted ``org/secret`` slug, its path-segment child
        ``org/secret/sub``, and the bare org ``secretorg`` for its repo, but
    * NOT match a PUBLIC slug that merely contains the org as a substring of a
        longer owner segment (``secretorg-mirror/x``).
    """
    from teatree.hooks._repo_visibility import slug_is_allowlisted_private  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as raw:
        cfg = Path(raw) / ".teatree.toml"
        cfg.write_text('[teatree]\nprivate_repos = ["org/secret", "secretorg"]\n', encoding="utf-8")
        matches_exact = slug_is_allowlisted_private("org/secret", cfg)
        matches_path_segment_child = slug_is_allowlisted_private("org/secret/sub", cfg)
        matches_org_repo = slug_is_allowlisted_private("secretorg/repo", cfg)
        matches_substring_alias = slug_is_allowlisted_private("secretorg-mirror/x", cfg)
    return matches_exact and matches_path_segment_child and matches_org_repo and not matches_substring_alias


def _check_banned_terms_scanner_fails_closed_on_crash() -> bool:
    """#1954: the banned-terms scanner FAILS CLOSED when the shell scanner dies.

    Pre-fix a crashing/timed-out scanner read as ``None`` (ALLOW) — a security
    gate failing open on a crash. The fixed :func:`scan_text` must:
    * return :data:`SCANNER_UNAVAILABLE_MARKER` (gate BLOCKS) when the shell
        scanner raises, never ``None``, and
    * return ``None`` on a genuine no-op (no config / no script to run).
    """
    from unittest.mock import patch  # noqa: PLC0415

    from teatree.hooks import banned_terms_scanner  # noqa: PLC0415
    from teatree.hooks.banned_terms_scanner import SCANNER_UNAVAILABLE_MARKER, scan_text  # noqa: PLC0415
    from teatree.utils.run import CommandFailedError  # noqa: PLC0415

    def _crashing_scanner(*_args: object, **_kwargs: object) -> object:
        raise CommandFailedError(cmd=["check-banned-terms.sh"], returncode=2, stdout="", stderr="boom")

    with tempfile.TemporaryDirectory() as raw:
        cfg = Path(raw) / ".teatree.toml"
        cfg.write_text('[teatree]\nbanned_terms = ["acmecorp"]\n', encoding="utf-8")
        with patch.object(banned_terms_scanner, "run_allowed_to_fail", _crashing_scanner):
            on_crash = scan_text("we ship to acmecorp", config_path=cfg)
        on_no_config = scan_text("we ship to acmecorp", config_path=Path(raw) / "absent.toml")
    return on_crash == SCANNER_UNAVAILABLE_MARKER and on_no_config is None


def _check_forge_resolves_by_host_not_token() -> bool:
    """#2085: the forge backend is keyed on the repo ORIGIN HOST, not token precedence.

    Pre-fix the backend was chosen by which PAT happened to be configured, so a
    github.com repo resolved to GitLab when only a GitLab token was present. The
    fixed :func:`forge_from_remote` must classify purely by host:
    * a github.com remote → ``"github"``,
    * a gitlab.com / self-hosted-gitlab remote → ``"gitlab"``, and
    * an unrecognised host → ``""`` — regardless of configured PATs.
    """
    from teatree.utils.forge import forge_from_remote  # noqa: PLC0415

    github = forge_from_remote("git@github.com:souliane/teatree.git")
    gitlab_dotcom = forge_from_remote("git@gitlab.com:acme/widgets.git")
    gitlab_self_hosted = forge_from_remote("https://gitlab.example.com/acme/widgets")
    unknown = forge_from_remote("git@git.example.org:acme/widgets.git")
    return github == "github" and gitlab_dotcom == "gitlab" and gitlab_self_hosted == "gitlab" and not unknown


def _check_ship_branch_reconcile_renamed() -> bool:
    """#1587: pre-push gates reconcile a renamed/stale recorded branch.

    Pre-fix the gates read the stale ``<N>-ticket`` recorded ref, so the
    ``origin/main..<stale>`` range query silently skipped. The fixed
    :func:`resolve_and_reconcile_branch` must:
    * adopt the prefixed CURRENT git branch when the agent renamed
        ``<N>-ticket`` → ``<N>-fix-foo`` (and persist it on the row), and
    * fall back to the recorded branch on an unrelated / non-prefixed ref.
    """
    from teatree.core.models import Ticket, Worktree  # noqa: PLC0415
    from teatree.core.runners.ship import resolve_and_reconcile_branch  # noqa: PLC0415

    issue_url = "https://github.com/souliane/teatree/issues/999999042"
    Ticket.objects.filter(issue_url=issue_url).delete()
    ticket = Ticket.objects.create(overlay="regression-corpus", issue_url=issue_url)
    prefix = f"{ticket.ticket_number}-"
    try:
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw)
            repo = seed_repo_on_branch(work, f"{prefix}ticket")
            worktree = Worktree.objects.create(
                ticket=ticket,
                overlay="regression-corpus",
                repo_path=str(repo),
                branch=f"{prefix}ticket",
                extra={"worktree_path": str(repo)},
            )
            _git(repo, "branch", "-m", f"{prefix}ticket", f"{prefix}fix-foo")
            with without_git_overrides():
                adopted = resolve_and_reconcile_branch(ticket, worktree, str(repo))
            worktree.refresh_from_db()
            reconciled_on_row = worktree.branch

            _git(repo, "checkout", "-b", "unrelated-branch")
            worktree.branch = f"{prefix}fix-foo"
            worktree.save(update_fields=["branch"])
            with without_git_overrides():
                fell_back = resolve_and_reconcile_branch(ticket, worktree, str(repo))
    finally:
        ticket.delete()

    return adopted == f"{prefix}fix-foo" and reconciled_on_row == f"{prefix}fix-foo" and fell_back == f"{prefix}fix-foo"


def _check_mr_description_first_line_validated() -> bool:
    """#1367: the MR description FIRST LINE is validated client-side.

    Pre-fix only the title was checked, so a description opening with
    ``## Summary`` passed the client gate then red the GitLab
    ``validate_mr_title_and_description`` pipeline. The fixed
    :func:`validate_mr_metadata` must:
    * reject a description whose first line is not conventional-commit, and
    * accept a conventional-commit first line with a What/Why body.
    """
    from teatree.core.review.mr_metadata import DEFAULT_MR_TITLE_REGEX, validate_mr_metadata  # noqa: PLC0415

    title = "feat(ship): add the gate (#1367)"
    bad_first_line = "## Summary\nAdds the gate.\n\n## Why\nThe convention is missed often."
    good = "feat(ship): add the gate (#1367)\n\n## What\nthe change\n\n## Why\nthe reason"
    rejected = validate_mr_metadata(title, bad_first_line, DEFAULT_MR_TITLE_REGEX)
    accepted = validate_mr_metadata(title, good, DEFAULT_MR_TITLE_REGEX)
    return any("first line" in err.lower() for err in rejected) and accepted == []


__all__ = [
    "_check_account_switch_detect_and_recover",
    "_check_banned_terms_scanner_fails_closed_on_crash",
    "_check_branch_currency_conflict_only",
    "_check_forge_resolves_by_host_not_token",
    "_check_loop_owner_lease_pid_anchored",
    "_check_merge_precondition_maker_is_not_checker",
    "_check_merge_precondition_substrate_full_autonomy_holds",
    "_check_merge_precondition_substrate_human_authorize",
    "_check_mr_description_first_line_validated",
    "_check_private_repo_allowlist_path_segment_match",
    "_check_ship_branch_reconcile_renamed",
]
