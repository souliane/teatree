"""SessionStart: every standing directive reaches an engaged session as it starts, resumes, clears or compacts.

A session start is a turn that is already happening, so the delivery costs no turn and arms nothing
(#4166). This is its own Django-free process beside the router, silent on any error; who gets what is
:mod:`hooks.scripts.standing_directives_delivery`'s decision. The directives are recorded as delivered only
once they were written.
"""

import json
import logging
import sys
from pathlib import Path

# Run as a script, the plugin root and its ``src/`` are not on ``sys.path``; both imports below need them.
if str(Path(__file__).resolve().parents[2] / "src") not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
if str(Path(__file__).resolve().parents[2]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.scripts.additional_context import emit_additional_context
from hooks.scripts.standing_directives_delivery import due_rules

logger = logging.getLogger(__name__)


def main() -> None:
    try:
        rules = due_rules(str(json.loads(sys.stdin.read()).get("session_id") or ""), every_slot=True)
        if rules.text:
            emit_additional_context("SessionStart", rules.text)
        rules.mark_delivered()
    except Exception:
        logger.debug("no standing directives at this session start", exc_info=True)


if __name__ == "__main__":
    main()
