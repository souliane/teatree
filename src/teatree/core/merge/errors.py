"""Merge-keystone exception hierarchy (BLUEPRINT §17.4)."""


class MergePreconditionError(RuntimeError):
    """A §17.4.3 pre-condition check failed — the loop must not merge.

    The caller re-escalates into the durable backlog (it never self-issues a
    replacement CLEAR) and leaves the FSM unchanged.
    """


class MergeHeadMovedError(MergePreconditionError):
    """GitHub rejected the merge because the head moved off ``expected_head_oid``.

    Treated as a failed check, NOT a retry-with-new-head (§17.4.3): the loop
    never re-resolves the head and proceeds.
    """


class MergeReplayError(MergePreconditionError):
    """The CLEAR was already consumed when re-checked UNDER the row lock.

    ``assert_merge_preconditions`` reads ``is_actionable()`` without the row
    lock; two executors that both pass that unlocked check would otherwise
    both reach the post hook and double-consume the single-use CLEAR (a
    double ``MergeAudit`` / double ``mark_merged()``). The post hook re-reads
    the row ``select_for_update``-locked and re-asserts ``consumed_at is
    None`` so exactly one executor wins; the loser raises this.
    """


class MergeTransientError(MergePreconditionError):
    """The forge merge call failed with a transient/empty-JSON/network/5xx response.

    Distinct from a policy refusal (not-mergeable / required-checks /
    review-required) and from a head-moved (:class:`MergeHeadMovedError`):
    a truncated or empty API body (``unexpected end of JSON input``), a
    network error, a timeout, or a 5xx is the forge momentarily failing to
    answer, NOT a verdict on the merge. ``execute_bound_merge`` auto-retries
    a bounded number of times before raising this; only after the retries are
    exhausted does it surface so the caller re-escalates into the durable
    backlog. Because it is raised BEFORE the post hook, the single-use CLEAR
    is never consumed — a manual / loop retry of the SAME CLEAR can merge
    (the #1804 stranding window).
    """
