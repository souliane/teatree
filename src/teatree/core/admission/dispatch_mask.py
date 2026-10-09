"""The admission composition every headless (django-tasks) dispatch site reads.

It extends ``claim_admission_block_reason`` with the ``dispatch``-loop mask. That
arm cannot live in ``managers_task_claim`` itself: ``teatree.loops.enable_verdict``
reaches back to it through ``core.models``, and tach forbids the cycle.
"""

from teatree.core.managers_task_claim import claim_admission_block_reason
from teatree.loops.enable_verdict import loop_admits


def headless_admission_block_reason() -> str:
    if reason := claim_admission_block_reason():
        return reason
    if not loop_admits("dispatch"):
        return "the dispatch loop is masked off by the active mode/hold"
    return ""


__all__ = ["headless_admission_block_reason"]
