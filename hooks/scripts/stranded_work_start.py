"""SessionStart: the work the previous session left stranded in this checkout, delivered once.

Its own Django-free process beside the router, silent on any error. The report is taken out of the
store before it is written and put back if the write fails, unless a later end has left a newer one,
so it reaches exactly one session. An Agent-SDK session start takes nothing: the report is for the
interactive session the owner reads.
"""

import json
import logging
import sys
from pathlib import Path

# Run as a script, the plugin root is not on ``sys.path``; the imports below need it.
if str(Path(__file__).resolve().parents[2]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.scripts.additional_context import emit_additional_context
from hooks.scripts.session_lane import LANE_SDK, session_lane
from hooks.scripts.stranded_work_report import claim

logger = logging.getLogger(__name__)


def main() -> None:
    if session_lane() == LANE_SDK:
        return
    try:
        report = claim(str(json.loads(sys.stdin.read()).get("cwd") or ""))
    except Exception:
        logger.debug("no stranded-work report at this session start", exc_info=True)
        return
    if not report.text:
        return
    try:
        emit_additional_context("SessionStart", report.text)
    except Exception:
        report.put_back()
        logger.debug("the stranded-work report was not delivered; kept for the next start", exc_info=True)
        return
    report.consume()


if __name__ == "__main__":
    main()
