"""TeaTree config enums for operating mode, throughput, autonomy, and review backend."""

from enum import StrEnum


class Mode(StrEnum):
    """Operating mode for agent sessions.

    ``interactive`` (conservative on security) gates publishing actions
    on explicit user approval — push, PR creation/merge, external writes all
    stop and ask. ``auto`` (the default) grants full autonomy: the agent ships end-to-end
    without confirmation, falling back to interactive only for the non-
    negotiable always-gated list (force-push to default branches, destructive
    shared-state ops). ``mode`` is a DB-home setting: opt in via ``t3 <overlay>
    config_setting set mode auto`` (per-overlay overridable with ``--overlay
    <name>``) or the ``T3_MODE`` environment variable — a ``[teatree] mode`` TOML
    value is ignored on read.
    """

    INTERACTIVE = "interactive"
    AUTO = "auto"

    @classmethod
    def parse(cls, value: str) -> "Mode":
        """Parse a mode string. Invalid values raise ``ValueError``.

        The dataclass default (``AUTO``) is applied by the caller when the
        setting is absent — this function only validates explicit values, so a
        typo raises loud rather than silently resolving to a mode.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid t3 mode {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


class Wip(StrEnum):
    """How much new work a loop tick admits at once — the bounded-WIP dial.

    A single dial spanning sequential to burst throughput. Orthogonal to
    :class:`Mode` and :class:`Autonomy` (which govern *whether* a publishing
    action may proceed); ``wip`` governs *how many* threads of work run
    concurrently — it never relaxes a safety gate.

    Tiers (``SLOW`` < ``MEDIUM`` < ``FULL`` < ``BOOST``, default ``FULL``):

    *   :attr:`SLOW` — at most one implementation worker in flight at a time
        (the cold-review reviewer still runs separately). The cautious dial
        for a fragile tree or a constrained host.
    *   :attr:`MEDIUM` — the conservative baseline: NO orchestrator fan-out.
        Throughput comes only from the intrinsic loop, the PR sweep, and the
        per-overlay ``max_concurrent_auto_starts`` auto-start cap.
    *   :attr:`FULL` — arm ``/loop /t3:wip boost`` so each wave re-classifies
        the backlog and fans out a burst, sustained across waves.
    *   :attr:`BOOST` — a pool-refill burst that keeps ``boost_concurrency
        = N`` live workers in flight, refilling the shortfall each tick;
        clamped to ``max_concurrent_auto_starts``.

    A no-arg ``/t3:wip`` invocation means "go full" regardless of the
    persisted baseline; the persisted value is the resting dial the loop
    reads. ``wip`` is a DB-home setting: opt in via ``t3 <overlay>
    config_setting set wip full`` (the ``t3 <overlay> wip set <level>``
    wrapper does this), or the ``T3_WIP`` environment variable — a
    ``[teatree] wip`` TOML value is ignored on read.
    """

    SLOW = "slow"
    MEDIUM = "medium"
    FULL = "full"
    BOOST = "boost"

    @classmethod
    def parse(cls, value: str) -> "Wip":
        """Parse a wip string; typos raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the dataclass default (:attr:`FULL`) is
        applied by the caller when the setting is absent, so this validates
        only explicit values and a typo raises rather than changing throughput.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid wip {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


class Autonomy(StrEnum):
    """The single per-overlay trust switch collapsing the tier-governed approval gates.

    Tiers (``FULL`` > ``NOTIFY`` > ``BABYSIT``, default ``FULL``):

    *   :attr:`BABYSIT` — every approval gate keeps its own value; the user
        stays in the loop on merges and answers.
        Review-request posting follows the active posture like any other
        colleague-visible post.
    *   :attr:`NOTIFY` — autonomous with colleague approval before merge
        (per-diff CLEAR, never self-approve).
    *   :attr:`FULL` — autonomous; the single-author
        ``solo_overlay`` merge bypass is reachable here only, and the substrate
        per-PR sign-off is satisfied by this standing grant (the §17.4.3 step 5
        carve-out — see :func:`teatree.core.merge.execution.assert_merge_preconditions`)
        so a substrate CLEAR needs no per-CLEAR ``human_authorizer``.

    Both autonomous tiers collapse the tier-governed gates and pin ``mode = auto`` — see
    :func:`_apply_autonomy`. Two gates are deliberately outside that set, each its
    own named opt-in no tier touches: ``require_human_approval_to_merge`` for review
    before merge (#3630), and ``Mode.egress`` for speaking to a colleague
    under the owner's own identity (#3895). An explicit per-gate value always wins. The
    safety floor (privacy/leak gate, cold-review with reviewer != maker,
    CI-green, not-draft, never-lockout, the SHA-bound audited keystone
    transition) is out of scope and never touched — under ``full`` the substrate
    carve-out removes ONLY the per-PR human sign-off, never a floor guard.
    """

    BABYSIT = "babysit"
    NOTIFY = "notify"
    FULL = "full"

    @classmethod
    def parse(cls, value: str) -> "Autonomy":
        """Parse an autonomy string; invalid values raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the dataclass default (:attr:`FULL`) is
        applied by the caller when the setting is absent, so this validates
        only explicit values and a typo raises rather than resolving to a tier.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid autonomy {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


class PrReviewBackend(StrEnum):
    """Which reviewer executes the self-authored-PR cold review.

    *   :attr:`AUTO` (default) — resolve per tick: codex when its binary is on
        PATH and it is not cooling down from a quota exhaustion, else claude.
        This is the only value that can change its mind, and it exists so a box
        with no codex installed, or an account that just ran out, keeps getting
        reviews instead of none.
    *   :attr:`CLAUDE` / :attr:`CODEX` — an explicit pin. A pin is honoured as
        written and never silently degrades to the other backend: an operator who
        named a reviewer wants to know it is unavailable, not to discover weeks
        later that something else has been reviewing their diffs.

    Note what the tiers do NOT govern: whether a self-PR is reviewed at all.
    Every self-authored open PR is admitted to the review board regardless; this
    picks WHO reviews it.
    """

    AUTO = "auto"
    CLAUDE = "claude"
    CODEX = "codex"

    @classmethod
    def parse(cls, value: str) -> "PrReviewBackend":
        """Parse a pr-review-backend string; invalid values raise ``ValueError``."""
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid pr_review_backend {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc
