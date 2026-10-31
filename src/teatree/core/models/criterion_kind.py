from enum import StrEnum


class CriterionKind(StrEnum):
    BEHAVIOUR = "behaviour"
    PROCESS = "process"


def render_criterion(text: str, kind: str) -> str:
    try:
        return f"[{CriterionKind(kind).value}] {text.strip()}"
    except ValueError:
        msg = f"unknown criterion kind {kind!r}: use one of {', '.join(member.value for member in CriterionKind)}"
        raise ValueError(msg) from None
