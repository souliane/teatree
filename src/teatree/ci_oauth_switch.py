r"""Point CI's OAuth secret at the healthiest Anthropic account.

The eval benchmark in CI authenticates with ONE subscription account, held in the
``CLAUDE_CODE_OAUTH_TOKEN`` repo secret. When that account's window exhausts mid-run
the remaining shards spend nothing, every scenario force-FAILs, and the published
numbers are garbage. This module picks the account with the most headroom out of the
SAME rows ``t3 tokens`` renders (:class:`~teatree.token_report.TokenReport`) and points
the secret at it.

Selection is two separable, separately-testable steps:

*   **Eligibility.** Only ``pass``-stored OAuth accounts are candidates (a metered API
    key cannot fill an OAuth secret, so it is filtered rather than rejected). A candidate
    whose health reads :attr:`~teatree.token_report.TokenStatus.EXHAUSTED` on EITHER
    window — or that is missing / unreachable — is ineligible.
    :func:`select_account` never silently returns second-best: with no eligible
    candidate it carries every rejection so the caller fails loud.
*   **Ranking.** Eligible candidates score on headroom *as of the moment the run starts*,
    which is what makes the 5h-vs-7d tradeoff explicit rather than incidental. A 5h
    window that resets at or before ``run_start`` counts as FULLY free, so an account at
    87 % 5h / 13 % weekly ranks poorly for a run starting now and best for one starting
    after its reset. The rule is lexicographic, in two named parts:

    1.  :attr:`AccountHeadroom.binding_headroom` — the MINIMUM of the two headrooms. A
        run is throttled by whichever window runs out first, so a near-spent 5h window
        disqualifies an otherwise-rich weekly balance (that account stalls at the start
        of the run, which is the very failure this module exists to prevent) and vice
        versa.
    2.  :attr:`AccountHeadroom.weighted_headroom` — a :data:`WEIGHT_5H` / :data:`WEIGHT_7D`
        blend of TOTAL headroom, breaking ties between accounts equally constrained on
        their binding window. The weekly window carries more weight there because a long
        benchmark outlasts several 5h windows but never the weekly one.

Exhaustion is judged on the CURRENT reading even when a reset falls before ``run_start``:
an account the routing selector refuses today is never promoted by a projection.

The token is read from ``pass`` and piped to ``gh secret set`` on stdin — it never
reaches an argv, a log line, a return value, or an exception message. The account
IDENTITY travels separately as the plain, readable repo variable
:data:`CI_ACCOUNT_VARIABLE`, so a benchmark run can record which account produced which
shards (cost figures are not comparable across accounts on different plans) and so
:meth:`CiAccountSwitcher.switch` can no-op when the secret already points at the best
account.
"""

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from teatree.credential_config import TokenKind
from teatree.token_report import TokenAccountRow, TokenSource, TokenStatus
from teatree.utils.run import run_allowed_to_fail
from teatree.utils.secrets import read_pass

#: The repo Actions secret the eval workflows authenticate with.
CI_OAUTH_SECRET = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105 — a secret's NAME, not its value

#: The plain (non-secret) repo variable naming which account :data:`CI_OAUTH_SECRET`
#: currently holds. A secret is write-only, so this readable companion is both the
#: idempotency key and the cost-basis attribution a benchmark run records.
CI_ACCOUNT_VARIABLE = "CLAUDE_CODE_OAUTH_ACCOUNT"

#: The TIE-BREAK blend, as one visible knob — applied only between accounts whose binding
#: window is equally free. The weekly window weighs more because a multi-hour benchmark
#: outlasts several 5h windows but never the weekly one.
WEIGHT_5H = 0.4
WEIGHT_7D = 0.6

#: Why each non-healthy status disqualifies a candidate, in operator language.
_REJECTION_REASON: dict[TokenStatus, str] = {
    TokenStatus.MISSING: "no token stored at this pass entry",
    TokenStatus.UNREACHABLE: "health probe did not reach Anthropic",
    TokenStatus.OUT_OF_CREDITS: "prepaid credits are depleted",
}

_NO_CANDIDATES = "no OAuth accounts are configured — set anthropic_oauth_pass_paths before switching the CI secret"


class GhClient(Protocol):
    """A ``gh`` invocation: argv after the ``gh`` word, optional stdin, ``(exit, output)``."""

    def __call__(self, args: list[str], *, stdin_text: str | None = None) -> tuple[int, str]: ...


#: Resolve an account's token from its ``pass`` entry.
type SecretReader = Callable[[str], str]


class CiAccountSwitchError(RuntimeError):
    """The CI secret could not be switched. Never carries a token value."""


class NoEligibleAccountError(CiAccountSwitchError):
    """No configured account can serve the run — every candidate was rejected.

    Raised INSTEAD of leaving the stale secret in place while reporting success, which
    is exactly the silent path that publishes garbage shards. :attr:`rejected` carries
    the per-account reasons the message enumerates.
    """

    def __init__(self, message: str, *, rejected: tuple["Rejection", ...] = ()) -> None:
        super().__init__(message)
        self.rejected = rejected


@dataclass(frozen=True)
class AccountHeadroom:
    """One eligible account's headroom as of the run's start, and its resulting score."""

    account: str
    utilization_5h: float
    utilization_7d: float
    headroom_5h: float
    headroom_7d: float
    resets_before_run: bool

    @property
    def binding_headroom(self) -> float:
        """The scarcer window's free fraction — what actually throttles the run."""
        return min(self.headroom_5h, self.headroom_7d)

    @property
    def weighted_headroom(self) -> float:
        """Total headroom, blended — the tie-break between equally-constrained accounts."""
        return WEIGHT_5H * self.headroom_5h + WEIGHT_7D * self.headroom_7d


@dataclass(frozen=True)
class Rejection:
    """One candidate that cannot serve the run, and why."""

    account: str
    reason: str

    def __str__(self) -> str:
        return f"{self.account}: {self.reason}"


@dataclass(frozen=True)
class Selection:
    """The ranked eligible accounts plus every rejection, for a loud failure."""

    ranked: tuple[AccountHeadroom, ...]
    rejected: tuple[Rejection, ...]

    @property
    def best(self) -> AccountHeadroom | None:
        return self.ranked[0] if self.ranked else None


@dataclass(frozen=True)
class SwitchOutcome:
    """What the switch did: which account, which it replaced, and whether it wrote.

    ``changed`` is whether the best account differs from the recorded active one;
    ``applied`` is whether the secret was actually written (false on a no-op AND on a
    dry run), so a caller can tell "nothing to do" from "would have done it".
    """

    account: str
    previous: str
    changed: bool
    applied: bool
    binding_headroom: float
    headroom_5h: float
    headroom_7d: float
    rejected: tuple[Rejection, ...]


def _headroom(utilization: float, reset: dt.datetime | None, run_start: dt.datetime) -> tuple[float, bool]:
    """The window's free fraction at *run_start*, and whether it resets by then.

    A window resetting at or before the run's start is fully free when the run begins,
    however spent it reads now — that projection is the whole point of taking the reset
    timestamps into account.
    """
    if reset is not None and reset <= run_start:
        return 1.0, True
    return max(0.0, 1.0 - utilization), False


def _rejection_for(row: TokenAccountRow) -> Rejection | None:
    """Why *row* cannot serve the run, or ``None`` when it is eligible."""
    if row.status is TokenStatus.EXHAUSTED:
        reason = f"exhausted — 5h {row.utilization_5h * 100:.0f}% used, weekly {row.utilization_7d * 100:.0f}% used"
        return Rejection(row.account, reason)
    fixed_reason = _REJECTION_REASON.get(row.status)
    if fixed_reason is not None:
        return Rejection(row.account, fixed_reason)
    return None


def _headroom_for(row: TokenAccountRow, run_start: dt.datetime) -> AccountHeadroom:
    headroom_5h, resets_before_run = _headroom(row.utilization_5h, row.next_window_reset, run_start)
    headroom_7d, _ = _headroom(row.utilization_7d, row.weekly_reset, run_start)
    return AccountHeadroom(
        account=row.account,
        utilization_5h=row.utilization_5h,
        utilization_7d=row.utilization_7d,
        headroom_5h=headroom_5h,
        headroom_7d=headroom_7d,
        resets_before_run=resets_before_run,
    )


def _is_candidate(row: TokenAccountRow) -> bool:
    """Whether *row* is a ``pass``-stored OAuth account — the only thing the secret accepts."""
    return row.kind is TokenKind.OAUTH and row.source is TokenSource.STORE


def select_account(rows: Sequence[TokenAccountRow], *, run_start: dt.datetime) -> Selection:
    """Rank the OAuth accounts in *rows* by headroom at *run_start*.

    Ties break on weekly headroom, then account name, so the choice is deterministic
    across runs with identical health.
    """
    candidates = [row for row in rows if _is_candidate(row)]
    rejected = tuple(rejection for rejection in map(_rejection_for, candidates) if rejection is not None)
    rejected_accounts = {rejection.account for rejection in rejected}
    eligible = [_headroom_for(row, run_start) for row in candidates if row.account not in rejected_accounts]
    ranked = sorted(eligible, key=lambda entry: (-entry.binding_headroom, -entry.weighted_headroom, entry.account))
    return Selection(ranked=tuple(ranked), rejected=rejected)


def _default_gh(args: list[str], *, stdin_text: str | None = None) -> tuple[int, str]:
    """Run ``gh <args>``; a non-zero exit is a verdict to classify, not an error to raise on."""
    result = run_allowed_to_fail(["gh", *args], expected_codes=None, stdin_text=stdin_text)
    return result.returncode, f"{result.stdout}\n{result.stderr}"


class CiAccountSwitcher:
    """Point *repo*'s :data:`CI_OAUTH_SECRET` at the healthiest configured account.

    The ``gh`` invoker and the ``pass`` reader are injected (defaults: the real ``gh``
    and :func:`~teatree.utils.secrets.read_pass`) so a test drives the whole rotation
    with no forge and no secret store.
    """

    def __init__(self, *, repo: str, gh: GhClient | None = None, secret_reader: SecretReader | None = None) -> None:
        self._repo = repo
        self._gh: GhClient = gh or _default_gh
        self._secret_reader = secret_reader or read_pass

    def active_account(self) -> str:
        """The account the secret currently holds, per :data:`CI_ACCOUNT_VARIABLE`.

        Empty when the variable is unset — the pre-switch state, which simply means the
        next switch always writes.
        """
        code, out = self._gh(["api", f"repos/{self._repo}/actions/variables/{CI_ACCOUNT_VARIABLE}", "--jq", ".value"])
        return out.strip() if code == 0 else ""

    def switch(
        self, rows: Sequence[TokenAccountRow], *, run_start: dt.datetime, dry_run: bool = False
    ) -> SwitchOutcome:
        """Select from *rows* and point the CI secret at the winner.

        Raises :class:`NoEligibleAccountError` — naming every candidate and why it was
        rejected — rather than leaving a spent account in place and reporting success.
        A *dry_run*, and an already-optimal secret, both leave the forge untouched.
        """
        selection = select_account(rows, run_start=run_start)
        best = selection.best
        if best is None:
            raise NoEligibleAccountError(self._no_eligible_message(selection), rejected=selection.rejected)

        previous = self.active_account()
        changed = best.account != previous
        if changed and not dry_run:
            self._write_secret(best.account)
            self._record_account(best.account)
        return SwitchOutcome(
            account=best.account,
            previous=previous,
            changed=changed,
            applied=changed and not dry_run,
            binding_headroom=best.binding_headroom,
            headroom_5h=best.headroom_5h,
            headroom_7d=best.headroom_7d,
            rejected=selection.rejected,
        )

    @staticmethod
    def _no_eligible_message(selection: Selection) -> str:
        if not selection.rejected:
            return _NO_CANDIDATES
        lines = [f"no eligible Anthropic OAuth account — the CI secret was left untouched ({CI_OAUTH_SECRET}):"]
        lines.extend(f"  {rejection}" for rejection in selection.rejected)
        return "\n".join(lines)

    def _write_secret(self, account: str) -> None:
        """Pipe *account*'s token into the repo secret. The value never enters an argv."""
        token = self._secret_reader(account)
        if not token:
            empty = f"no token stored at {account!r} — the CI secret was left untouched"
            raise CiAccountSwitchError(empty)
        code, _out = self._gh(
            ["secret", "set", CI_OAUTH_SECRET, "--repo", self._repo, "--body-file", "-"], stdin_text=token
        )
        if code != 0:
            # The command's output is deliberately dropped: it is the one place a token
            # value could be echoed back into an exception message.
            failed = f"`gh secret set {CI_OAUTH_SECRET}` failed on {self._repo} (exit {code})"
            raise CiAccountSwitchError(failed)

    def _record_account(self, account: str) -> None:
        """Record which account the secret now holds — a path, never a token."""
        code, out = self._gh(["variable", "set", CI_ACCOUNT_VARIABLE, "--repo", self._repo, "--body", account])
        if code != 0:
            failed = (
                f"secret updated to {account} but `gh variable set {CI_ACCOUNT_VARIABLE}` failed "
                f"on {self._repo} (exit {code}): {out.strip()}"
            )
            raise CiAccountSwitchError(failed)


__all__ = [
    "CI_ACCOUNT_VARIABLE",
    "CI_OAUTH_SECRET",
    "WEIGHT_5H",
    "WEIGHT_7D",
    "AccountHeadroom",
    "CiAccountSwitchError",
    "CiAccountSwitcher",
    "GhClient",
    "NoEligibleAccountError",
    "Rejection",
    "SecretReader",
    "Selection",
    "SwitchOutcome",
    "select_account",
]
