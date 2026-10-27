"""One warning per undeclared model-extra key across hot validation reads."""

import logging
from collections.abc import Iterable


def warn_stripped_extra_keys(
    model: str, dropped: Iterable[str], seen: set[tuple[str, str]], logger: logging.Logger
) -> None:
    for key in sorted(dropped):
        if (model, key) in seen:
            continue
        seen.add((model, key))
        logger.warning(
            "%s.extra: dropping undeclared key %r — declare it on %sExtra or the next transition strips it.",
            model,
            key,
            model,
        )
