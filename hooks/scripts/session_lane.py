"""Which lane a hook is running in, read from the transport's own env contract.

Two decisions turn on this one question and resolve it in OPPOSITE directions:
``headless_authoring_gate`` refuses only a positively-identified interactive CLI
session, while ``engagement.autoload_skill_demand`` withholds the platform skill
only from a positively-identified SDK run. A second copy of the env contract would
let the two drift into disagreeing about which sessions are the factory's, so both
read this leaf.

Cold-import safe: stdlib only.
"""

import os

LANE_INTERACTIVE_CLI = "interactive_cli"
LANE_SDK = "sdk"
LANE_UNKNOWN = "unknown"


def session_lane() -> str:
    """:data:`LANE_SDK` for any Agent-SDK embedding, :data:`LANE_INTERACTIVE_CLI` for a human-driven CLI session.

    :data:`LANE_UNKNOWN` when the env carries neither signature. Callers decide
    what an unknown lane means for them; this reports only what the env states.

    The SDK signature is checked FIRST and is the broader test, so a transport that
    sets both (or an env teatree does not recognise) resolves toward "not
    interactive" — the direction that cannot take the factory down.
    """
    entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").strip().lower()
    if os.environ.get("CLAUDE_AGENT_SDK_VERSION", "").strip() or entrypoint.startswith("sdk"):
        return LANE_SDK
    if entrypoint == "cli" and os.environ.get("CLAUDECODE", "").strip():
        return LANE_INTERACTIVE_CLI
    return LANE_UNKNOWN
