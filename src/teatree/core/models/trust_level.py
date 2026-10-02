"""The per-action-class trust level an operator sets in the approval dial (#119)."""

from enum import StrEnum


class TrustLevel(StrEnum):
    """One action class's configured trust: ASK a human, or AUTO-approve by policy.

    The stored value of each class in the ``approval_dial`` ``ConfigSetting`` table.
    ``ask`` is the ship default (every class ASK — the dial inert). ``auto`` is the
    operator's deliberate graduation, still floored by the taint check and re-tightened
    by a metric breach.
    """

    ASK = "ask"
    AUTO = "auto"
