"""Whole-message operator control verbs for bound ticket threads."""


def control_verb(message: str) -> str | None:
    words = message.casefold().strip().strip(".!").replace(",", " ").split()
    if not words or words[0] not in {"pause", "resume"}:
        return None
    selectors = [word for word in words[1:] if word.startswith("#")]
    if len(selectors) > 1:
        return None
    if any(
        word not in {"it", "please", "task"} and not (word.startswith("#") and word[1:].isdigit()) for word in words[1:]
    ):
        return None
    return words[0]
