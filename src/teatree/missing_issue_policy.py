"""Missing-issue-reference policy — verdict resolver.

Single source of truth for the policy that decides what teatree does when a
commit or MR/PR needs an issue reference and the agent has none in hand. The
recurring failure this resolver replaces is the agent *improvising* — inventing
a dummy ref, or auto-filing an issue on a colleague's tracker — when it should
instead recover the ORIGINAL existing issue and, failing that, defer the
fallback to a repo-class-aware policy.

The policy has two ordered steps:

1.  **Find existing first (always).** Before anything else the agent looks for
    the ORIGINAL existing issue that introduced the bug or left the scope
    unimplemented — searching the repo's open/closed issues and the
    introducing commit's linked issue — and uses THAT. This step is
    unconditional: every tier of the setting tries it first. When an existing
    issue is found the verdict is :attr:`MissingIssueVerdict.USE_EXISTING`,
    independent of the policy tier and the repo class.

2.  **Fallback when none is found**, decided by the
    :class:`~teatree.config.MissingIssuePolicy` setting and the repo class
    (colleague-facing/external vs the user's own):

    *   :attr:`~teatree.config.MissingIssuePolicy.FIND_EXISTING_THEN_ASK`
        (default) — on a colleague-facing repo the agent must NEVER auto-create
        and never use a dummy ref; it :attr:`~MissingIssueVerdict.ASK_USER`. On
        the user's own repo, creating is allowed
        (:attr:`~MissingIssueVerdict.CREATE`).
    *   :attr:`~teatree.config.MissingIssuePolicy.CREATE` (opt-in) — auto-create
        is authorised even on a colleague-facing repo
        (:attr:`~MissingIssueVerdict.CREATE`).
    *   :attr:`~teatree.config.MissingIssuePolicy.DUMMY` (opt-in) — a
        placeholder/dummy ref is authorised even on a colleague-facing repo
        (:attr:`~MissingIssueVerdict.DUMMY`).

The colleague-vs-own distinction is the SCOPE/collaboration axis the rest of
teatree already tracks (``owned_repos`` / ``author_is_self``); this resolver
takes it as the ``colleague_facing`` argument the caller supplies rather than
re-deriving it, keeping this module a thin layer depending only on
:mod:`teatree.config` so it can be imported from anywhere (``teatree.cli``,
``teatree.core``) without a circular dependency — exactly the shape of
:mod:`teatree.on_behalf_gate`.

The setting is ``[teatree] missing_issue_ref_policy`` (default
:attr:`~teatree.config.MissingIssuePolicy.FIND_EXISTING_THEN_ASK`, per-overlay
overridable, env override via ``T3_MISSING_ISSUE_POLICY``). Resolution follows
the standard env → active-overlay → global → default chain via
:func:`teatree.config.get_effective_settings`. The agent-facing prose lives in
``skills/ship/SKILL.md`` § "Missing Issue Reference Policy".
"""

from enum import StrEnum

from teatree.config import MissingIssuePolicy, get_effective_settings


class MissingIssueVerdict(StrEnum):
    """The outcomes :func:`resolve_missing_issue_verdict` returns."""

    #: The ORIGINAL existing issue was found — use it (every tier, every repo).
    USE_EXISTING = "use_existing"
    #: No existing issue; stop and ASK the user (the conservative colleague-repo
    #: outcome under the default policy). Never create, never dummy.
    ASK_USER = "ask_user"
    #: No existing issue; auto-create an issue (own repo under the default
    #: policy, or any repo when the operator opted into ``create``).
    CREATE = "create"
    #: No existing issue; use a placeholder/dummy ref (only when the operator
    #: opted into ``dummy``).
    DUMMY = "dummy"


def resolve_missing_issue_verdict(*, colleague_facing: bool, existing_found: bool) -> MissingIssueVerdict:
    """Return the verdict for a missing issue ref under the effective policy.

    ``existing_found`` is whether the always-first search for the ORIGINAL
    existing issue succeeded; when it did, the verdict is
    :attr:`MissingIssueVerdict.USE_EXISTING` regardless of the policy tier or
    the repo class. ``colleague_facing`` is whether the target repo is a
    colleague-facing / external repo (a shared product repo the user does not
    own) vs the user's own repo (teatree, the user's solo overlay repos).

    When no existing issue is found, the fallback is decided by the resolved
    :class:`~teatree.config.MissingIssuePolicy`:

    *   :attr:`~teatree.config.MissingIssuePolicy.FIND_EXISTING_THEN_ASK`
        (default) → :attr:`MissingIssueVerdict.ASK_USER` on a colleague-facing
        repo (never auto-create, never dummy), :attr:`MissingIssueVerdict.CREATE`
        on the user's own repo.
    *   :attr:`~teatree.config.MissingIssuePolicy.CREATE` (opt-in) →
        :attr:`MissingIssueVerdict.CREATE` on any repo.
    *   :attr:`~teatree.config.MissingIssuePolicy.DUMMY` (opt-in) →
        :attr:`MissingIssueVerdict.DUMMY` on any repo.

    Resolution follows the standard env (``T3_MISSING_ISSUE_POLICY``) →
    active-overlay → global → default chain via
    :func:`teatree.config.get_effective_settings`.
    """
    if existing_found:
        return MissingIssueVerdict.USE_EXISTING

    policy = get_effective_settings().missing_issue_ref_policy
    if policy is MissingIssuePolicy.DUMMY:
        return MissingIssueVerdict.DUMMY
    if policy is MissingIssuePolicy.CREATE:
        return MissingIssueVerdict.CREATE
    # FIND_EXISTING_THEN_ASK (default): never auto-create or dummy on a
    # colleague-facing repo — ASK the user. Creating is allowed on the user's
    # own repo (the user owns the tracker, so it is self-bookkeeping).
    return MissingIssueVerdict.ASK_USER if colleague_facing else MissingIssueVerdict.CREATE
