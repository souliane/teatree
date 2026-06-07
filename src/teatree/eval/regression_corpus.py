"""Deterministic regression evals — real code-path assertions per failure class.

A behavioral scenario (``scenarios/*.yaml``) grades what an agent *says* it
would do. This corpus grades what the gate/checker code *does*: each check
calls the REAL function (the merge-precondition assertion, the branch-currency
conflict predictor, the loop-lease pid-anchored
claim, the migration-graph leaf checker) on a constructed must-block input and
on a must-allow input, and reports a violation when either direction is wrong.

This is a Layer-1 eval per ``README.md`` — deterministic, free, no ``claude``
run — sibling of :mod:`teatree.eval.trigger_qa`. It exists so the recurring
safety-gate failure classes of the last development cycle each have one check
that would go RED on the pre-fix behavior and stays GREEN on the fix, surfaced
through ``t3 eval pinned-regressions`` and the ``eval-pinned-regressions`` prek
pre-push hook.

Each :class:`RegressionCheck` names its failure class, the originating fix, and
a callable that returns ``True`` when the real code path still honors the
invariant. A check that needs a git repo builds a throwaway one under a
``tempfile.TemporaryDirectory`` and tears it down; a check that needs the ORM
is skipped unless Django is configured (the CLI bootstraps it). No network, no
secrets, no shared state.
"""

import dataclasses
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from teatree.utils.git import git_env_without_overrides
from teatree.utils.run import run_checked


@contextmanager
def _staged_overlay_autonomy(overlay_name: str, autonomy: str) -> Iterator[None]:
    """Run the block with a hermetic ``~/.teatree.toml`` pinning *overlay_name* to *autonomy*.

    An eval-isolation helper for assertions whose outcome depends on the
    overlay's effective autonomy (the substrate-merge carve-out). Swaps
    ``teatree.config.CONFIG_PATH`` at the single seam ``get_effective_settings``
    reads, so the resolved autonomy is deterministic regardless of the
    developer's live config, and restores it on exit.
    """
    from teatree import config as config_module  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as raw:
        cfg = Path(raw) / ".teatree.toml"
        cfg.write_text(f'[teatree]\n[overlays.{overlay_name}]\nautonomy = "{autonomy}"\n', encoding="utf-8")
        original = config_module.CONFIG_PATH
        config_module.CONFIG_PATH = cfg
        try:
            yield
        finally:
            config_module.CONFIG_PATH = original


@dataclasses.dataclass(frozen=True)
class RegressionCheck:
    """One real-code-path regression check for a named failure class."""

    failure_class: str
    origin: str
    invariant: str
    predicate: Callable[[], bool]
    needs_db: bool = False


@dataclasses.dataclass(frozen=True)
class CheckResult:
    check: RegressionCheck
    ok: bool
    skipped: bool
    detail: str


@dataclasses.dataclass(frozen=True)
class RegressionReport:
    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if not r.ok and not r.skipped)


def _git(repo: Path, *args: str) -> str:
    env = {
        **git_env_without_overrides(),
        "GIT_AUTHOR_NAME": "eval",
        "GIT_AUTHOR_EMAIL": "eval@example.com",
        "GIT_COMMITTER_NAME": "eval",
        "GIT_COMMITTER_EMAIL": "eval@example.com",
    }
    return run_checked(["git", *args], cwd=repo, env=env).stdout


def _seed_repo_with_diverging_target(work: Path) -> tuple[Path, str]:
    """Build a repo whose feature SHA conflicts with ``origin/main``.

    Returns ``(repo_path, feature_sha)``. ``origin`` is a sibling clone the
    branch-currency fetch resolves, so the real ``sha_conflicts_with_target``
    runs its true ``git fetch`` + ``git merge-tree`` path against a genuine
    divergence — not a mock.
    """
    origin = work / "origin"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")

    repo = work / "clone"
    _git(work, "clone", str(origin), "clone")
    conflicted = repo / "conflict.txt"
    conflicted.write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "origin", "HEAD:main")

    _git(repo, "checkout", "-b", "feature")
    conflicted.write_text("feature side\n", encoding="utf-8")
    _git(repo, "commit", "-am", "feature edit")
    feature_sha = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "checkout", "main")
    conflicted.write_text("main side\n", encoding="utf-8")
    _git(repo, "commit", "-am", "main edit")
    _git(repo, "push", "origin", "main")
    _git(repo, "checkout", "feature")
    return repo, feature_sha


def _seed_repo_behind_but_clean(work: Path) -> tuple[Path, str]:
    """Build a repo whose feature SHA is behind ``origin/main`` but conflict-free."""
    origin = work / "origin2"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")

    repo = work / "clone2"
    _git(work, "clone", str(origin), "clone2")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "origin", "HEAD:main")

    _git(repo, "checkout", "-b", "feature")
    feature_sha = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "checkout", "main")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")  # disjoint file — no conflict
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main advances disjointly")
    _git(repo, "push", "origin", "main")
    _git(repo, "checkout", "feature")
    return repo, feature_sha


def _check_branch_currency_conflict_only() -> bool:
    """§940: the CLEAR-side gate blocks ONLY on a real conflict, never behind-alone.

    Pre-fix the gate refused any behind branch (the behind-only ritual #940
    removed). The fixed ``sha_conflicts_with_target`` must:
    * return a finding when the reviewed SHA truly conflicts with the target, and
    * return ``None`` when the SHA is merely behind but conflict-free.
    """
    from teatree.core.branch_currency import sha_conflicts_with_target  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as raw:
        work = Path(raw)
        conflict_repo, conflict_sha = _seed_repo_with_diverging_target(work)
        conflict = sha_conflicts_with_target(str(conflict_repo), conflict_sha, "origin/main")
        clean_repo, clean_sha = _seed_repo_behind_but_clean(work)
        clean = sha_conflicts_with_target(str(clean_repo), clean_sha, "origin/main")
    return conflict is not None and bool(conflict.conflicting_paths) and clean is None


_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _check_merge_precondition_substrate_human_authorize() -> bool:
    """Substrate floor: a below-full substrate CLEAR never merges without the recorded human authorizer.

    Exercises the real ``_assert_clear_authorized`` guard (the network-free
    §17.4.3 identity/substrate block) against an actionable, green,
    independently-reviewed substrate ``MergeClear``:
    * presenting no ``--human-authorized`` must RAISE (the floor holds), and
    * presenting the recorded authorizer must NOT raise on that guard.

    The CLEAR's overlay (resolved from its ``slug``) is pinned to ``babysit``
    via a hermetic ``~/.teatree.toml`` so the check is deterministic regardless
    of the developer's live config. The ``autonomy = full`` carve-out is a
    distinct must-allow path verified by
    :func:`_check_merge_precondition_substrate_full_autonomy`; this check is the
    must-block direction for a below-full overlay.
    """
    return _exercise_substrate_authorize(autonomy="babysit", expect_cleared_without_human=False)


def _check_merge_precondition_substrate_full_autonomy() -> bool:
    """Carve-out: a substrate CLEAR under an ``autonomy = full`` overlay clears without a per-PR sign-off.

    The mirror of :func:`_check_merge_precondition_substrate_human_authorize`:
    with the CLEAR's overlay pinned to ``full``, the standing grant satisfies
    the substrate sign-off, so presenting no ``--human-authorized`` must NOT
    raise. This is the must-allow direction the ticket-less / aliased
    overlay-resolution fix restores; without it, a full-autonomy substrate
    merge is wrongly blocked (the rest of the floor is unaffected).
    """
    return _exercise_substrate_authorize(autonomy="full", expect_cleared_without_human=True)


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
                human_authorized="",
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
            human_authorized="",
        )
    except MergePreconditionError:
        return True
    return False


def _check_loop_owner_lease_pid_anchored() -> bool:
    """#1604/#1722: an alive foreign owner past its TTL is never hijacked.

    The pre-fix lease released on TTL lapse alone, so a fresh session stole a
    busy owner's loop. The pid-anchored ``claim_ownership`` must:
    * refuse a foreign claim while the owner's pid is alive (even past TTL), and
    * grant the claim once the owner's pid is dead and the TTL has lapsed.
    """
    from datetime import timedelta  # noqa: PLC0415

    from django.utils import timezone  # noqa: PLC0415

    from teatree.core.models import LoopLease  # noqa: PLC0415

    name = "regression-lease"
    alive_pid = os.getpid()
    LoopLease.objects.filter(name=name).delete()
    LoopLease.objects.claim_ownership(name, session_id="owner-session", owner_pid=alive_pid, ttl_seconds=1800)
    # Force the TTL to have lapsed; only the alive pid now protects the lease.
    LoopLease.objects.filter(name=name).update(lease_expires_at=timezone.now() - timedelta(seconds=10))
    won_against_alive, _ = LoopLease.objects.claim_ownership(
        name, session_id="thief-session", owner_pid=os.getpid(), ttl_seconds=1800
    )

    dead_pid = _unused_pid()
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


def _unused_pid() -> int:
    """A pid that is (almost certainly) not alive — picks a high free slot."""
    from teatree.utils.singleton import pid_alive  # noqa: PLC0415

    for candidate in range(2_000_000, 2_000_500):
        if not pid_alive(candidate):
            return candidate
    return 2_147_483_000


def _count_core_leaves(graph: object) -> int:
    """Number of leaf nodes the ``core`` app owns in a migration graph.

    A linear graph has exactly one; a fork (two migrations off one parent)
    leaves two. The predicate the regression check turns on, factored out so a
    test can feed it a synthetic forked graph and assert it returns ``> 1``.
    """
    return sum(1 for leaf in graph.leaf_nodes() if leaf[0] == "core")  # type: ignore[attr-defined]


def _check_migration_graph_single_leaf() -> bool:
    """#1721: the migration graph stays linear — a forked graph (>1 leaf) is caught.

    The real failure: two PRs each branch a migration off the same parent, the
    merged graph has multiple leaf nodes, and ``migrate`` refuses. This asserts
    the live ``teatree.core`` graph has exactly one leaf node via
    :func:`_count_core_leaves` — the same predicate a synthetic forked graph
    drives ``> 1`` in the corpus's anti-vacuous test.
    """
    from django.db.migrations.loader import MigrationLoader  # noqa: PLC0415

    loader = MigrationLoader(None, ignore_no_migrations=True)
    return _count_core_leaves(loader.graph) == 1


class _StubBackend:
    def __init__(self, *, ok: bool) -> None:
        self._ok = ok
        self.name = "slack"

    def auth_test(self) -> dict:
        return {"ok": self._ok} if self._ok else {"ok": False, "error": "invalid_auth"}


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

    reachable = AccountSwitchRecovery(reset_caches=_fake_reset, backends=lambda: [_StubBackend(ok=True)])
    unreachable_recovery = AccountSwitchRecovery(reset_caches=_fake_reset, backends=lambda: [_StubBackend(ok=False)])

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


_CHECKS: tuple[RegressionCheck, ...] = (
    RegressionCheck(
        failure_class="branch-currency §940 (conflict-only, never behind-only)",
        origin="https://github.com/souliane/teatree/pull/1719",
        invariant="sha_conflicts_with_target blocks a real conflict, allows a behind-but-clean SHA",
        predicate=_check_branch_currency_conflict_only,
    ),
    RegressionCheck(
        failure_class="substrate-merge human-authorize floor",
        origin="https://github.com/souliane/teatree/pull/1498",
        invariant="a below-full substrate MergeClear never merges without the recorded human authorizer",
        predicate=_check_merge_precondition_substrate_human_authorize,
        needs_db=True,
    ),
    RegressionCheck(
        failure_class="substrate-merge full-autonomy carve-out",
        origin="https://github.com/souliane/teatree/issues/1748",
        invariant="an autonomy=full overlay clears a ticket-less substrate CLEAR without a per-PR human sign-off",
        predicate=_check_merge_precondition_substrate_full_autonomy,
        needs_db=True,
    ),
    RegressionCheck(
        failure_class="maker≠checker at merge time",
        origin="https://github.com/souliane/teatree/pull/1601",
        invariant="a self-issued CLEAR (reviewer == executing loop) is refused at merge time",
        predicate=_check_merge_precondition_maker_is_not_checker,
        needs_db=True,
    ),
    RegressionCheck(
        failure_class="loop-owner hijack / pid-anchored lease",
        origin="https://github.com/souliane/teatree/pull/1724",
        invariant="an alive foreign owner past TTL is never hijacked; a dead owner is reclaimable",
        predicate=_check_loop_owner_lease_pid_anchored,
        needs_db=True,
    ),
    RegressionCheck(
        failure_class="migration-fork / multiple-leaf-nodes",
        origin="https://github.com/souliane/teatree/pull/1721",
        invariant="the live core migration graph has exactly one leaf; a forked graph is detectable",
        predicate=_check_migration_graph_single_leaf,
        needs_db=True,
    ),
    RegressionCheck(
        failure_class="account-switch detect-invalidate-reprobe (#1916)",
        origin="https://github.com/souliane/teatree/issues/1916",
        invariant="a /login switch invalidates the backend cache and re-probes; same account is a no-op",
        predicate=_check_account_switch_detect_and_recover,
    ),
    RegressionCheck(
        failure_class="private-repo allowlist path-segment match (security, #1953)",
        origin="https://github.com/souliane/teatree/pull/2084",
        invariant="private_repos matches path segments, not substring; an alias-glued public slug never downgrades",
        predicate=_check_private_repo_allowlist_path_segment_match,
    ),
    RegressionCheck(
        failure_class="banned-terms scanner fail-closed on crash (security, #1954)",
        origin="https://github.com/souliane/teatree/pull/2079",
        invariant="a crashing scanner returns SCANNER_UNAVAILABLE_MARKER (gate blocks), never None; a no-op is None",
        predicate=_check_banned_terms_scanner_fails_closed_on_crash,
    ),
)


def _django_ready() -> bool:
    try:
        from django.apps import apps  # noqa: PLC0415
    except ImportError:
        return False
    return apps.ready


def run_regression_corpus(checks: tuple[RegressionCheck, ...] = _CHECKS) -> RegressionReport:
    db_ready = _django_ready()
    results: list[CheckResult] = []
    for check in checks:
        if check.needs_db and not db_ready:
            results.append(CheckResult(check=check, ok=True, skipped=True, detail="Django not configured"))
            continue
        try:
            ok = check.predicate()
            results.append(CheckResult(check=check, ok=ok, skipped=False, detail="" if ok else "invariant violated"))
        except Exception as exc:  # noqa: BLE001 — a raising predicate IS a regression failure, not a crash.
            results.append(CheckResult(check=check, ok=False, skipped=False, detail=f"{type(exc).__name__}: {exc}"))
    return RegressionReport(results=tuple(results))


def render_text(report: RegressionReport) -> str:
    lines: list[str] = []
    for r in report.results:
        status = "SKIP" if r.skipped else ("PASS" if r.ok else "FAIL")
        line = f"{status} {r.check.failure_class}"
        if r.detail:
            line += f" — {r.detail}"
        lines.append(line)
    passed = sum(1 for r in report.results if r.ok and not r.skipped)
    skipped = sum(1 for r in report.results if r.skipped)
    lines.append(f"\nsummary: {passed} passed, {len(report.failures)} failed, {skipped} skipped")
    return "\n".join(lines)


def render_json(report: RegressionReport) -> str:
    return json.dumps(
        {
            "ok": report.ok,
            "checks": [
                {
                    "failure_class": r.check.failure_class,
                    "origin": r.check.origin,
                    "ok": r.ok,
                    "skipped": r.skipped,
                    "detail": r.detail,
                }
                for r in report.results
            ],
        },
        indent=2,
    )
