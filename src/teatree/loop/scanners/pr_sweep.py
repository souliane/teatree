"""Auto-merge-green-PRs scanner (#1248).

For each open PR on a configured repo, the scanner checks the BLUEPRINT
§17.4.3 pre-conditions deterministically and — when they all pass —
invokes the sanctioned ``t3 <overlay> ticket merge <clear_id>``
transition. The orchestrator no longer has to wake up every time a PR
turns green; the loop closes itself.

Decision ladder per open PR:

1. ``draft: true`` → skip
2. open ``CHANGES_REQUESTED`` review → skip
3. no actionable ``MergeClear`` row for ``(slug, pr_id, head_sha)``
    → skip (collaborative-overlay default) OR fall through to
    ``gh pr merge --squash`` (solo-overlay carve-out, #1309 — see
    ``solo_overlay`` on :class:`PrSweepScanner`)
4. CI ``test(3.13)`` not green AND red checks include something
    other than ``uv-audit`` → skip
5. only red check is ``uv-audit`` AND ``main`` is also red on
    ``uv-audit`` → ``--fallback-uv-audit``
6. all required checks green → merge through the keystone

Step 5's ``--fallback-uv-audit`` switch documents the scanner's standing
authorisation to escalate to ``gh pr merge --squash`` when the keystone
transition refuses on the same fallback path (a pre-existing-on-``main``
failing audit job is a deterministic gate, not an ad-hoc judgement —
exactly the case §17.4.3 step 7 reserves for the scanner).

The scanner posts a Slack DM only on actual merges (acceptance gate);
skips log to the periodic-task log but never DM, to keep the DM channel
quiet.
"""

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Protocol, TypedDict, cast, runtime_checkable

from teatree.core.models.merge_clear import MergeClear
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass, ScanSignal
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)


GREEN_TERMINAL_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
REQUIRED_CHECK_NAME = "test (3.13)"
UV_AUDIT_CHECK_NAME = "uv-audit"
_GH_NOT_INSTALLED_RC = 127


class GhPrJson(TypedDict, total=False):
    """Shape of one ``gh pr list --json …`` entry the scanner consumes."""

    number: int
    headRefOid: str
    isDraft: bool
    url: str
    title: str
    reviews: list[object]
    statusCheckRollup: list[object]


class GhReviewJson(TypedDict, total=False):
    """Shape of one review entry inside ``GhPrJson.reviews``."""

    state: str


class GhCheckJson(TypedDict, total=False):
    """Shape of one check entry inside ``GhPrJson.statusCheckRollup``."""

    name: str
    context: str
    conclusion: str
    status: str
    state: str


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One required-status check on a PR head."""

    name: str
    conclusion: str
    status: str

    @property
    def verdict(self) -> str:
        upper_status = self.status.upper()
        if upper_status and upper_status != "COMPLETED":
            return "pending"
        upper_conclusion = self.conclusion.upper()
        if upper_conclusion in GREEN_TERMINAL_CONCLUSIONS:
            return "green"
        return "failed"


@dataclass(frozen=True, slots=True)
class PrSummary:
    """Decoded subset of a PR's ``gh`` payload the sweep needs."""

    slug: str
    number: int
    head_sha: str
    is_draft: bool
    has_changes_requested: bool
    checks: tuple[CheckResult, ...]
    url: str = ""
    title: str = ""


@dataclass(frozen=True, slots=True)
class MergeAttempt:
    """The scanner's per-PR decision plus any merge outcome."""

    slug: str
    pr_id: int
    decision: str
    merged: bool = False
    merged_sha: str = ""
    reason: str = ""


@runtime_checkable
class PrApiClient(Protocol):
    """Adapter over ``gh`` used by the scanner — mockable in tests.

    Two methods only: list open PRs on a repo, and fetch the per-PR
    detail block (head SHA, draft, reviews, checks). The implementation
    shells out to ``gh`` with an optional ``GH_TOKEN`` override so each
    overlay can hit its private repos under its own PAT.
    """

    def list_open_prs(self, *, slug: str) -> list[PrSummary]: ...  # pragma: no branch

    def main_check_failed(self, *, slug: str, check_name: str) -> bool: ...  # pragma: no branch

    def merge_pr_squash(self, *, slug: str, pr_id: int) -> tuple[bool, str]: ...  # pragma: no branch


@runtime_checkable
class MergeKeystone(Protocol):
    """Adapter over ``call_command('ticket', 'merge', ...)`` — mockable."""

    def merge_clear(self, *, clear_id: int) -> tuple[bool, str, str]:
        """Return ``(merged, merged_sha, error)`` — ``error`` is the rejection reason."""
        ...  # pragma: no branch


@runtime_checkable
class MergeNotifier(Protocol):
    """Post a Slack DM only when a merge actually lands (acceptance gate)."""

    def announce(self, *, slug: str, pr_id: int, merged_sha: str, fallback: bool) -> None: ...  # pragma: no branch


@dataclass(slots=True)
class PrSweepScanner:
    """Sweep open PRs on configured repos; merge the green-and-cleared ones (#1248).

    *repos* is the ordered list of GitHub ``owner/repo`` slugs the scanner
    sweeps every tick. *api* fetches PR state through ``gh``; *keystone*
    executes the sanctioned merge transition; *notifier* posts the
    post-merge DM (no DM on skips — that's the noise the spec rules out).
    *overlay* tags emitted signals so a multi-overlay loop can attribute
    merges to the right overlay (private-overlay PRs run under a
    different code-host token).

    *solo_overlay* opts the scanner into the dogfood-overlay bypass (#1309).
    A solo overlay is a single-author repo whose user has explicitly opted
    in via ``mode = "auto"`` + ``require_human_approval_to_merge = false``.
    On such an overlay the maker / reviewer is the same human identity, and
    :meth:`MergeClear.issue` mechanically refuses a self-attested CLEAR
    (``is_non_reviewer_role`` guard) — no orchestrator can ever issue a
    CLEAR for that PR. Without this bypass the sweep silently no-ops every
    green+mergeable+clean PR on the dogfood overlay with reason
    ``no_clear_for_head``, which is exactly the failure mode #1309
    reports. When ``solo_overlay=True`` AND no actionable CLEAR exists for
    the head, the scanner runs the same precondition checks (draft,
    changes-requested, CI verdict) and — only if every gate is green —
    falls back to a direct ``gh pr merge --squash`` via
    :meth:`PrApiClient.merge_pr_squash`. The CLEAR contract is left
    untouched for every overlay that did NOT explicitly opt in; this is
    the conservative side of the two options on the table because it
    keeps the cold-reviewer attestation as the default and only relaxes
    it for the overlay configuration the user has already declared
    "trust the agent end-to-end".
    """

    repos: tuple[str, ...]
    api: PrApiClient
    keystone: MergeKeystone
    notifier: MergeNotifier
    overlay: str = ""
    solo_overlay: bool = False
    name: str = "pr_sweep"

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        for slug in self.repos:
            for pr in self._safe_list(slug):
                attempt = self._evaluate(pr)
                signals.append(_signal_from_attempt(attempt, overlay=self.overlay))
                logger.info(
                    "pr_sweep %s#%d decision=%s reason=%s merged=%s",
                    attempt.slug,
                    attempt.pr_id,
                    attempt.decision,
                    attempt.reason,
                    attempt.merged,
                )
        return signals

    def _safe_list(self, slug: str) -> list[PrSummary]:
        try:
            return self.api.list_open_prs(slug=slug)
        except ScannerError:
            # Auth / rate-limit / missing-scope: propagate to the dispatcher
            # so this scanner is recorded in ``report.errors`` and skipped for
            # one tick (#1287). Silently swallowing would mask the failure.
            raise
        except Exception:
            logger.exception("pr_sweep failed to list PRs for %s", slug)
            return []

    def _evaluate(self, pr: PrSummary) -> MergeAttempt:
        skip_reason = _precondition_skip_reason(pr)
        if skip_reason is not None:
            return _skip(pr, reason=skip_reason)
        clear = _find_actionable_clear(slug=pr.slug, pr_id=pr.number, head_sha=pr.head_sha)
        if clear is None:
            if self.solo_overlay:
                return self._evaluate_solo_overlay(pr)
            return _skip(pr, reason="no_clear_for_head")
        check_verdict = _classify_checks(pr.checks)
        if check_verdict in {"failed", "pending"}:
            return _skip(pr, reason="ci_red" if check_verdict == "failed" else "ci_pending")
        fallback = check_verdict == "green_with_uv_audit_red"
        if fallback and not self._main_uv_audit_red(slug=pr.slug):
            return _skip(pr, reason="uv_audit_red_but_clean_on_main")
        return self._merge(pr=pr, clear=clear, fallback=fallback)

    def _evaluate_solo_overlay(self, pr: PrSummary) -> MergeAttempt:
        """Merge a green+clean PR on a solo overlay without a CLEAR (#1309).

        Runs the same CI verdict gate as the CLEAR path so a red or pending
        check still blocks. A green-only-but-uv-audit-red PR escalates the
        same way (``main`` must also be red on uv-audit). Once the CI gate
        passes, calls :meth:`PrApiClient.merge_pr_squash` directly — the
        keystone path can't be used here because it requires a CLEAR row.
        """
        check_verdict = _classify_checks(pr.checks)
        if check_verdict in {"failed", "pending"}:
            return _skip(pr, reason="ci_red" if check_verdict == "failed" else "ci_pending")
        fallback = check_verdict == "green_with_uv_audit_red"
        if fallback and not self._main_uv_audit_red(slug=pr.slug):
            return _skip(pr, reason="uv_audit_red_but_clean_on_main")
        ok, merged_sha = self.api.merge_pr_squash(slug=pr.slug, pr_id=pr.number)
        if not ok:
            return MergeAttempt(
                slug=pr.slug,
                pr_id=pr.number,
                decision="blocked",
                reason="solo_overlay_gh_fallback_failed",
            )
        self._announce_merge(slug=pr.slug, pr_id=pr.number, merged_sha=merged_sha, fallback=fallback)
        reason = "solo_overlay_no_clear_uv_audit" if fallback else "solo_overlay_no_clear"
        return MergeAttempt(
            slug=pr.slug,
            pr_id=pr.number,
            decision="merged",
            merged=True,
            merged_sha=merged_sha,
            reason=reason,
        )

    def _main_uv_audit_red(self, *, slug: str) -> bool:
        try:
            return self.api.main_check_failed(slug=slug, check_name=UV_AUDIT_CHECK_NAME)
        except Exception:
            logger.exception("pr_sweep failed to fetch main uv-audit status for %s", slug)
            return False

    def _merge(self, *, pr: PrSummary, clear: MergeClear, fallback: bool) -> MergeAttempt:
        merged, merged_sha, error = self.keystone.merge_clear(clear_id=int(clear.pk))
        if merged:
            self._announce_merge(slug=pr.slug, pr_id=pr.number, merged_sha=merged_sha, fallback=fallback)
            return MergeAttempt(
                slug=pr.slug,
                pr_id=pr.number,
                decision="merged",
                merged=True,
                merged_sha=merged_sha,
                reason="fallback_uv_audit" if fallback else "all_green",
            )
        if fallback:
            ok, fallback_sha = self.api.merge_pr_squash(slug=pr.slug, pr_id=pr.number)
            if ok:
                self._announce_merge(slug=pr.slug, pr_id=pr.number, merged_sha=fallback_sha, fallback=True)
                return MergeAttempt(
                    slug=pr.slug,
                    pr_id=pr.number,
                    decision="merged",
                    merged=True,
                    merged_sha=fallback_sha,
                    reason="fallback_uv_audit_gh",
                )
        return MergeAttempt(
            slug=pr.slug,
            pr_id=pr.number,
            decision="blocked",
            reason=error or "keystone_refused",
        )

    def _announce_merge(self, *, slug: str, pr_id: int, merged_sha: str, fallback: bool) -> None:
        try:
            self.notifier.announce(slug=slug, pr_id=pr_id, merged_sha=merged_sha, fallback=fallback)
        except Exception:
            logger.exception("pr_sweep failed to post merge notification for %s#%d", slug, pr_id)


def _skip(pr: PrSummary, *, reason: str) -> MergeAttempt:
    return MergeAttempt(slug=pr.slug, pr_id=pr.number, decision="skip", reason=reason)


def _precondition_skip_reason(pr: PrSummary) -> str | None:
    if pr.is_draft:
        return "draft"
    if pr.has_changes_requested:
        return "changes_requested"
    return None


def _classify_checks(checks: tuple[CheckResult, ...]) -> str:
    """Return ``green`` / ``green_with_uv_audit_red`` / ``pending`` / ``failed``.

    The required check is ``test(3.13)``: if it's not green the PR is not
    mergeable. If it IS green and the ONLY red check is ``uv-audit``, the
    PR falls into the documented fallback path that the scanner is
    authorised to escalate (step 5).
    """
    required = next((c for c in checks if c.name == REQUIRED_CHECK_NAME), None)
    if required is None or required.verdict != "green":
        if any(c.verdict == "pending" for c in checks if c.name == REQUIRED_CHECK_NAME):
            return "pending"
        return "failed" if checks else "pending"
    red = [c for c in checks if c.verdict == "failed"]
    if not red:
        if any(c.verdict == "pending" for c in checks):
            return "pending"
        return "green"
    if all(c.name == UV_AUDIT_CHECK_NAME for c in red):
        return "green_with_uv_audit_red"
    return "failed"


def _find_actionable_clear(*, slug: str, pr_id: int, head_sha: str) -> MergeClear | None:
    """Locate the actionable, SHA-matched CLEAR for *(slug, pr_id, head_sha)*.

    A row whose ``reviewed_sha`` does not match the live PR head is treated
    as absent (the CLEAR was issued against stale code — §17.4.2 binds the
    authorisation to the exact reviewed tree). The keystone transition
    re-validates SHA-match at merge time as well, so even a stale match
    here would be refused — this lookup just keeps the scanner quiet.
    """
    candidates = MergeClear.objects.filter(
        slug=slug,
        pr_id=pr_id,
        consumed_at__isnull=True,
    ).order_by("-issued_at")
    for clear in candidates:
        if clear.reviewed_sha == head_sha and clear.is_actionable():
            return clear
    return None


def _decode_pr(*, slug: str, raw: GhPrJson) -> PrSummary:
    number_raw = raw.get("number")
    number = number_raw if isinstance(number_raw, int) else 0
    head_sha = _as_str(raw.get("headRefOid"))
    is_draft = bool(raw.get("isDraft"))
    url = _as_str(raw.get("url"))
    title = _as_str(raw.get("title"))
    reviews_raw = raw.get("reviews")
    reviews: list[object] = list(reviews_raw) if isinstance(reviews_raw, list) else []
    rollup_raw = raw.get("statusCheckRollup")
    rollup: list[object] = list(rollup_raw) if isinstance(rollup_raw, list) else []
    return PrSummary(
        slug=slug,
        number=number,
        head_sha=head_sha,
        is_draft=is_draft,
        has_changes_requested=_has_changes_requested(reviews),
        checks=tuple(_decode_check(cast("GhCheckJson", item)) for item in rollup if isinstance(item, dict)),
        url=url,
        title=title,
    )


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _classify_gh_stderr(stderr: str) -> ScannerErrorClass:
    """Classify a non-zero ``gh`` stderr into a :class:`ScannerErrorClass` (#1287).

    The classifier reads gh's well-known error wording: auth-required
    prompts (``gh auth login``, ``GH_TOKEN``, ``Bad credentials``, ``401``),
    GitHub rate-limit messages (``API rate limit exceeded``, ``rate
    limit``, ``secondary rate limit``), and network failures (``dial
    tcp``, ``no such host``, ``Could not resolve``). Anything else falls
    through to :attr:`ScannerErrorClass.UNKNOWN` so the dispatcher still
    surfaces the failure rather than masking it.
    """
    lower = stderr.lower()
    rate_limit_markers = ("rate limit", "rate-limit", "secondary rate")
    auth_markers = ("gh auth login", "gh_token", "bad credentials", "401")
    network_markers = ("no such host", "could not resolve", "dial tcp", "network is unreachable")
    if any(marker in lower for marker in rate_limit_markers):
        return ScannerErrorClass.RATE_LIMIT
    if any(marker in lower for marker in auth_markers):
        return ScannerErrorClass.AUTH
    if any(marker in lower for marker in network_markers):
        return ScannerErrorClass.NETWORK
    return ScannerErrorClass.UNKNOWN


def _signal_from_attempt(attempt: MergeAttempt, *, overlay: str) -> ScanSignal:
    return ScanSignal(
        kind="pr_sweep.merged" if attempt.merged else f"pr_sweep.{attempt.decision}",
        summary=f"{attempt.slug}#{attempt.pr_id} {attempt.decision} ({attempt.reason})",
        payload={
            "slug": attempt.slug,
            "pr_id": attempt.pr_id,
            "decision": attempt.decision,
            "reason": attempt.reason,
            "merged": attempt.merged,
            "merged_sha": attempt.merged_sha,
            "overlay": overlay,
        },
    )


@dataclass(slots=True)
class GhPrApiClient:
    """``gh``-backed :class:`PrApiClient`.

    *token* — when non-empty — is exported as ``GH_TOKEN`` for every
    subprocess call so the scanner can hit a private repo on behalf of a
    given overlay using that overlay's PAT.
    """

    token: str = ""

    def list_open_prs(self, *, slug: str) -> list[PrSummary]:
        argv = [
            "pr",
            "list",
            "--repo",
            slug,
            "--state",
            "open",
            "--json",
            "number,headRefOid,isDraft,url,title,reviews,statusCheckRollup",
        ]
        rc, out, err = self._run_gh(argv)
        if rc == _GH_NOT_INSTALLED_RC:
            # gh-not-installed is an environmental error, not an upstream
            # auth/rate-limit issue — preserve the pre-existing "fall back
            # to empty" behaviour so a machine without ``gh`` does not spam
            # ScannerError per tick.
            return []
        if rc != 0:
            error_class = _classify_gh_stderr(err)
            detail = f"gh pr list {slug!r} rc={rc}: {err.strip()[:200]}"
            raise ScannerError(
                scanner="pr_sweep",
                error_class=error_class,
                detail=detail,
            )
        if not out.strip():
            return []
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [_decode_pr(slug=slug, raw=cast("GhPrJson", item)) for item in data if isinstance(item, dict)]

    def main_check_failed(self, *, slug: str, check_name: str) -> bool:
        argv = [
            "api",
            f"repos/{slug}/commits/main/check-runs",
            "--jq",
            f'.check_runs | map(select(.name == "{check_name}")) | .[0].conclusion // ""',
        ]
        rc, out, _ = self._run_gh(argv)
        if rc != 0:
            return False
        return out.strip().lower() not in {"success", "neutral", "skipped", ""}

    def merge_pr_squash(self, *, slug: str, pr_id: int) -> tuple[bool, str]:
        argv = ["pr", "merge", str(pr_id), "--repo", slug, "--squash"]
        rc, _out, _err = self._run_gh(argv)
        if rc != 0:
            return False, ""
        rc, out, _ = self._run_gh(
            ["pr", "view", str(pr_id), "--repo", slug, "--json", "mergeCommit", "--jq", ".mergeCommit.oid"],
        )
        return True, out.strip() if rc == 0 else ""

    def _run_gh(self, argv: list[str]) -> tuple[int, str, str]:
        gh = shutil.which("gh") or "gh"
        env = {**os.environ, "GH_TOKEN": self.token} if self.token else None
        try:
            result = run_allowed_to_fail([gh, *argv], expected_codes=None, env=env)
        except FileNotFoundError:
            return 127, "", "gh not installed"
        return result.returncode, result.stdout, result.stderr


def _has_changes_requested(reviews: list[object]) -> bool:
    """True iff any review on the PR is in ``CHANGES_REQUESTED`` state."""
    for review in reviews:
        if not isinstance(review, dict):
            continue
        review_dict = cast("GhReviewJson", review)
        state = _as_str(review_dict.get("state")).upper()
        if state == "CHANGES_REQUESTED":
            return True
    return False


def _decode_check(raw: GhCheckJson) -> CheckResult:
    name = _as_str(raw.get("name")) or _as_str(raw.get("context"))
    conclusion = _as_str(raw.get("conclusion"))
    status = _as_str(raw.get("status"))
    # Legacy StatusContext entries (no ``status`` field) carry ``state``;
    # treat ``state == SUCCESS`` as a green completed check so non-Check-Run
    # contexts (e.g. external CI) still classify correctly.
    if not status and not conclusion:
        state = _as_str(raw.get("state")).upper()
        if state == "SUCCESS":
            conclusion = "SUCCESS"
            status = "COMPLETED"
        elif state == "PENDING":
            status = "IN_PROGRESS"
        elif state:
            conclusion = "FAILURE"
            status = "COMPLETED"
    return CheckResult(name=name, conclusion=conclusion, status=status)


@dataclass(slots=True)
class CallCommandMergeKeystone:
    """Production :class:`MergeKeystone` — invokes ``call_command('ticket', 'merge', …)``."""

    loop_identity: str = "merge-loop"

    def merge_clear(self, *, clear_id: int) -> tuple[bool, str, str]:
        from django.core.management import call_command  # noqa: PLC0415

        result = call_command("ticket", "merge", str(clear_id), loop_identity=self.loop_identity)
        if not isinstance(result, dict):
            return False, "", "ticket merge returned non-dict"
        merged = bool(result.get("merged"))
        merged_sha = str(result.get("merged_sha") or "")
        error = str(result.get("error") or "")
        return merged, merged_sha, error


@dataclass(slots=True)
class SlackMergeNotifier:
    """Post a one-line DM on every actual merge (no DM on skips — acceptance gate)."""

    backend: object
    user_id: str = ""

    def announce(self, *, slug: str, pr_id: int, merged_sha: str, fallback: bool) -> None:
        if not self.user_id:
            return
        post = getattr(self.backend, "post_dm", None) or getattr(self.backend, "post_message", None)
        if not callable(post):
            return
        prefix = "merged (uv-audit fallback)" if fallback else "merged"
        sha_short = merged_sha[:8] if merged_sha else "?"
        post(channel=self.user_id, text=f"{prefix} {slug}#{pr_id} @ {sha_short}")


@dataclass(slots=True)
class NullMergeNotifier:
    """No-op notifier — used when Slack is not configured for the overlay."""

    calls: list[tuple[str, int, str, bool]] = field(default_factory=list)

    def announce(self, *, slug: str, pr_id: int, merged_sha: str, fallback: bool) -> None:
        self.calls.append((slug, pr_id, merged_sha, fallback))
