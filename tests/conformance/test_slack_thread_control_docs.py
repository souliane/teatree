"""README and BLUEPRINT state the shipped Slack thread-session contract, with no host data."""

import re

from tests.conformance._src_tree import REPO_ROOT

_CONTRACT = {
    "the thread binding": r"ThreadBinding",
    "operator-only forwarding": r"(?:Another|other) authors? in a bound thread",
    "pause as an operator_paused park": r"operator_paused:",
    "resume as its release": r"release_operator_pause",
    "queued input": r"queued into",
    "the context window setting": r"agent_model_context_windows",
    "Claude native compaction": r"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
    "pydantic_ai late compaction": r"LateHistoryCompactor",
}
_HOST_DATA = re.compile(r"/var/lib/[\w.-]+|/home/[\w.-]+|control-db|worker pid \d+")


def _docs() -> str:
    return "\n".join((REPO_ROOT / name).read_text(encoding="utf-8") for name in ("README.md", "BLUEPRINT.md"))


def test_readme_and_blueprint_name_the_thread_session_contract() -> None:
    docs = _docs()

    missing = [name for name, pattern in _CONTRACT.items() if not re.search(pattern, docs)]

    assert missing == []


def test_the_paragraphs_stating_the_contract_carry_no_host_data() -> None:
    paragraphs = [p for p in re.split(r"\n\s*\n", _docs()) if any(re.search(c, p) for c in _CONTRACT.values())]

    assert paragraphs
    assert [m for p in paragraphs for m in _HOST_DATA.findall(p)] == []
