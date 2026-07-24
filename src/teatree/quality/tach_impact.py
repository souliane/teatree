"""The tach pytest plugin's impact verdict, read report-only (#3672).

The impact analysis ships as a pytest PLUGIN, not a separate command — it deselects via
``pytest_collection_modifyitems`` when ``--tach`` / ``--tach-base`` is passed, and
otherwise only reports what it WOULD skip. This module takes the report-only half and
drives the plugin's handler directly, so:

* nothing is ever deselected — the verdict is computed, never applied;
* no second pytest session is spawned to obtain it;
* the plugin's ``pytest_sessionfinish`` (which rewrites ``NO_TESTS_COLLECTED`` to ``OK``
    when it removed items) is never reached, so that rewrite cannot fire here at all.

Every failure degrades to ``None`` — UNKNOWN, never an empty would-skip set. An empty
set would read as "the plugin agrees with us about everything", which is exactly the
confidently-wrong answer a failed probe must not manufacture.
"""

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tach.extension import ProjectConfig, TachPytestPluginHandler

logger = logging.getLogger(__name__)

#: The plugin auto-detects a bare local default branch; our selector diffs against the
#: ``origin/main`` merge-base. Pinning the plugin to the same ref is what makes the two
#: verdicts comparable at all — a divergent base would make every diff meaningless.
TACH_DEFAULT_BASE = "origin/main"


def _load_project_config(root: Path) -> "ProjectConfig | None":
    from tach.parsing import parse_project_config  # noqa: PLC0415 — deferred: optional dev-time dep

    return parse_project_config(root=root)


def _impact_handler(root: Path, base: str) -> "TachPytestPluginHandler | None":
    from tach.extension import TachPytestPluginHandler  # noqa: PLC0415 — deferred: optional dev-time dep
    from tach.filesystem.git_ops import get_changed_files  # noqa: PLC0415 — deferred: same

    config = _load_project_config(root)
    if config is None:
        return None
    changed = get_changed_files(project_root=root, head=None, base=base)
    return TachPytestPluginHandler(
        project_root=root,
        project_config=config,
        changed_files=changed,
        all_affected_modules={path.resolve() for path in changed},
    )


def would_skip_tests(root: Path, *, candidates: Iterable[str], base: str = TACH_DEFAULT_BASE) -> tuple[str, ...] | None:
    """Which of *candidates* the plugin would deselect, or ``None`` when it cannot answer.

    *candidates* are repo-relative posix test paths; the returned subset preserves that
    form so it diffs directly against our own selection.
    """
    paths = tuple(candidates)
    if not paths:
        return None
    try:
        handler = _impact_handler(root, base)
        if handler is None:
            return None
        return tuple(path for path in paths if handler.should_remove_items(file_path=(root / path).resolve()))
    except Exception:
        logger.debug("tach impact probe unavailable", exc_info=True)
        return None


__all__ = ["TACH_DEFAULT_BASE", "would_skip_tests"]
