"""TeaTree config enums — operating mode, throughput dial, autonomy, on-behalf gate."""

from enum import StrEnum


class Mode(StrEnum):
    """Operating mode for agent sessions.

    ``interactive`` (default, conservative on security) gates publishing actions
    on explicit user approval — push, PR creation/merge, external writes all
    stop and ask. ``auto`` grants full autonomy: the agent ships end-to-end
    without confirmation, falling back to interactive only for the non-
    negotiable always-gated list (force-push to default branches, destructive
    shared-state ops). Opt in via ``[teatree] mode = "auto"`` in
    ``~/.teatree.toml`` or the ``T3_MODE`` environment variable.
    """

    INTERACTIVE = "interactive"
    AUTO = "auto"

    @classmethod
    def parse(cls, value: str) -> "Mode":
        """Parse a mode string. Invalid values raise ``ValueError``.

        The conservative default (``INTERACTIVE``) is applied by the caller
        when the setting is absent — this function only validates explicit
        values, so typos never silently downgrade to a less-safe mode.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid t3 mode {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


# Friendly aliases accepted by ``Speed.parse`` and normalised to a canonical
# tier. Module-level (not a class attribute) so ``StrEnum`` does not try to
# treat the mapping as an enum member.
_SPEED_ALIASES: dict[str, str] = {
    "low": "slow",
    "normal": "medium",
    "high": "full",
}


class Speed(StrEnum):
    """How much parallel work the orchestrator drives at once.

    A single dial spanning sequential to burst throughput. Orthogonal to
    :class:`Mode` and :class:`Autonomy` (which govern *whether* a publishing
    action may proceed); ``speed`` governs *how many* threads of work run
    concurrently — it never relaxes a safety gate.

    Tiers (``SLOW`` < ``MEDIUM`` < ``FULL`` < ``BOOST``, default ``MEDIUM``):

    *   :attr:`SLOW` — at most one implementation worker in flight at a time
        (the cold-review reviewer still runs separately). The cautious dial
        for a fragile tree or a constrained host.
    *   :attr:`MEDIUM` — the conservative baseline: NO orchestrator fan-out.
        Throughput comes only from the intrinsic loop, the PR sweep, and the
        per-overlay ``max_concurrent_auto_starts`` auto-start cap.
    *   :attr:`FULL` — arm ``/loop /t3:speed boost`` so each wave re-classifies
        the backlog and fans out a burst, sustained across waves.
    *   :attr:`BOOST` — one parallel-backlog-blast wave (the former
        ``/t3:full-speed`` behaviour), clamped to ``max_concurrent_auto_starts``.

    A no-arg ``/t3:speed`` invocation means "go full" regardless of the
    persisted baseline; the persisted value is the resting dial the loop
    reads. Opt in via ``[teatree] speed = "full"`` in ``~/.teatree.toml``,
    the ``T3_SPEED`` environment variable, or ``t3 teatree speed set <level>``.
    """

    SLOW = "slow"
    MEDIUM = "medium"
    FULL = "full"
    BOOST = "boost"

    @classmethod
    def parse(cls, value: str) -> "Speed":
        """Parse a speed string, accepting friendly aliases; typos raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the conservative default (:attr:`MEDIUM`)
        is applied by the caller when the setting is absent, so this validates
        only explicit values and a typo never silently changes throughput.
        ``low``/``normal``/``high`` map onto ``slow``/``medium``/``full``.
        """
        normalised = value.strip().lower()
        normalised = _SPEED_ALIASES.get(normalised, normalised)
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            aliases = ", ".join(sorted(_SPEED_ALIASES))
            msg = f"Invalid speed {value!r}; valid values: {valid} (aliases: {aliases})"
            raise ValueError(msg) from exc


class Autonomy(StrEnum):
    """The single per-overlay trust switch collapsing the three user-approval gates.

    Tiers (``FULL`` > ``NOTIFY`` > ``BABYSIT``, default ``BABYSIT``):

    *   :attr:`BABYSIT` — every approval gate keeps its own value; the user
        stays in the loop on merges, answers, and colleague-visible posts.
    *   :attr:`NOTIFY` — autonomous, but every on-behalf action DMs the user
        (derived ``notify_on_behalf``) and the user's MR merges only after a
        colleague approval (per-diff CLEAR, never self-approve).
    *   :attr:`FULL` — autonomous with no after-the-fact DM; the single-author
        ``solo_overlay`` merge bypass is reachable here only, and the substrate
        per-PR sign-off is satisfied by this standing grant (the §17.4.3 step 5
        carve-out — see :func:`teatree.core.merge.execution.assert_merge_preconditions`)
        so a substrate CLEAR needs no per-CLEAR ``human_authorizer``.

    Both autonomous tiers collapse the three gates and pin ``mode = auto`` (see
    :func:`_apply_autonomy`). An explicit per-gate value always wins. The
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

        Mirrors :meth:`Mode.parse`: the conservative default
        (:attr:`BABYSIT`) is applied by the caller when the setting is
        absent, so a typo never silently grants full autonomy.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid autonomy {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


class TeamsDisplay(StrEnum):
    """How a Track-B maker pane is DISPLAYED — presentation-only (WI-5, #1838).

    A maker pane's SDK session always runs in-process (the source of truth); this
    setting governs only whether that same session is ALSO rendered in a visible
    terminal pane. The mechanism is tmux control mode (``tmux -CC``): under it
    iTerm2 renders ``tmux split-window`` panes as native split panes in one tab;
    elsewhere it degrades to plain tmux panes. The naming mirrors Claude Code's
    own ``teammateMode`` (``tmux`` / ``in-process``, with ``auto`` probing).

    Tiers (default :attr:`NONE`, ships dark):

    *   :attr:`NONE` (default) — no display layer. The in-process SDK path is
        unchanged; behaviour is byte-identical to today. The conservative default.
    *   :attr:`AUTO` — display via tmux WHEN a multiplexer is detected (``$TMUX``
        set, a ``tmux`` binary, a TTY), else fall back to the in-process path. The
        always-degrades-gracefully tier.
    *   :attr:`TMUX` — prefer the tmux display; still falls back to in-process when
        no tmux / no TTY / a spawn failure (the display never replaces the SDK run).

    Read from ``[teams] display`` (the feature namespace, alongside ``enabled``);
    per-overlay overridable via ``[overlays.<name>].teams_display``;
    ``T3_TEAMS_DISPLAY`` env wins. A typo / bad value fails SAFE to :attr:`NONE`
    — the presentation layer can never escalate itself on by a mistyped value.
    """

    NONE = "none"
    AUTO = "auto"
    TMUX = "tmux"

    @classmethod
    def parse(cls, value: str) -> "TeamsDisplay":
        """Parse a teams-display string; invalid values raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the conservative default (:attr:`NONE`) is
        applied by the caller when the setting is absent, so this validates only
        explicit values and a typo in a TOML/DB tier is rejected LOUD. The env
        tier instead fails SAFE to :attr:`NONE` (see ``_parse_env_teams_display``)
        so a mistyped env var never crashes the resolver.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid teams display {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


class OnBehalfPostMode(StrEnum):
    """Tri-state pre-gate over on-behalf colleague-VISIBLE posts (#960).

    Three points on the autonomy ramp for colleague-visible posts the
    agent makes *as the user* to a colleague/customer surface (PR/MR
    comment, issue comment, Slack channel/thread post, Notion post, PR/MR
    approve, reaction on someone else's message).

    Colleague-INVISIBLE *draft* notes (``t3 review post-draft-note``) are
    exempt from this gate under EVERY mode — a draft is never visible to
    colleagues (only the user can submit it), so it never needs approval.
    That exemption is the whole purpose of the setting: keep the user in
    control of their colleague-visible voice while letting the agent draft
    freely. Drafts always publish autonomously; under :attr:`ASK` /
    :attr:`DRAFT_OR_ASK` the agent additionally DMs the user with the
    publish/delete commands so they can review and submit.

    *   :attr:`DRAFT_OR_ASK` (default) and :attr:`ASK` behave identically:
        both auto-publish a draft (+ DM the user) and both BLOCK every
        colleague-visible post until the user records an approval. They
        are kept as distinct names for clarity and backward compatibility;
        the per-action draft exemption is what makes a draft ungated, not
        the mode.
    *   :attr:`ASK` — every colleague-VISIBLE action requires an explicit
        recorded approval (``t3 review approve-on-behalf <target> <action>
        --approver <id>``) before it publishes. Drafts are exempt.
    *   :attr:`IMMEDIATE` — the gate is off; gated actions publish
        directly (subject to the always-gated list in :class:`Mode`).

    The user satisfies the gate for a colleague-visible post without a TTY
    by recording an
    :class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval`;
    DMs *to the user themselves*, draft notes, and internal-only
    orchestration writes are out of scope and remain ungated under every
    mode.
    """

    DRAFT_OR_ASK = "draft_or_ask"
    ASK = "ask"
    IMMEDIATE = "immediate"

    @classmethod
    def parse(cls, value: str) -> "OnBehalfPostMode":
        """Parse an on-behalf-post-mode string; invalid values raise ``ValueError``.

        Mirrors :meth:`Mode.parse`: the conservative default
        (:attr:`DRAFT_OR_ASK`) is applied by the caller when the setting
        is absent, so typos never silently downgrade to a less-safe
        mode.
        """
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid on_behalf_post_mode {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc
