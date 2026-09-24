from teatree.agents.skill_injection import _COMPANION_HEADER


def companion_names(context: str) -> list[str]:
    if _COMPANION_HEADER not in context:
        return []
    block = context.split(_COMPANION_HEADER, 1)[1].split("\n\n", 1)[0]
    return [line.removeprefix("- ") for line in block.splitlines() if line.startswith("- ")]
