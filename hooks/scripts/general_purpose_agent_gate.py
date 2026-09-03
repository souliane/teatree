"""PreToolUse: refuse a blank ``general-purpose`` sub-agent for managed-repo work.

A ``general-purpose`` sub-agent starts with ZERO context — no skills, no repo
guidelines, no runbook, no memory — so for work in a repo this harness manages it
rediscovers the stack from scratch and operates outside the workflow. The rule
existed as an advisory ``additionalContext`` injection, which read as a
suggestion: it fired about a dozen times in one session and was overridden every
time. A rule that has failed again needs a gate, not another reminder.

Which repos count is OVERLAY knowledge, so it is read from the overlay registry
(``managed_repo.overlays_registry`` — the DB-home ``overlays`` row) rather than
named here: this module stays overlay-agnostic, and an overlay that declares no
repos disables the gate for itself by construction.

Scoped to the ``Agent``/``Task`` dispatch tools by exact name. The ``TaskCreate``
/ ``TaskUpdate`` todo writes carry a description and no ``subagent_type``, so a
name-prefix match would deny them and block task tracking.

Never-lockout: a per-call ``[general-purpose-ok: <reason>]`` token and the
``general_purpose_agent_gate_enabled`` kill-switch
(``t3 <overlay> gate general-purpose disable``) both ALLOW, an unreadable
registry yields no tokens and therefore no deny, and the deny routes through
``_fail_open_or_deny`` so the self-rescue allowlist + master fail-open switch +
circuit breaker apply. A crash inside the gate is caught by the router's
crash-proof dispatcher, which allows the call and prints the fault to stderr.

Cold-import safe: stdlib-only module top plus the already-extracted
``managed_repo`` sibling — no Django, no ``teatree`` at import.
"""

import re
import sys

from hooks.scripts.managed_repo import overlays_registry

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# registers and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("general_purpose_agent_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.general_purpose_agent_gate", sys.modules[__name__])

#: The two harness tool names that spawn a sub-agent.
DISPATCH_TOOLS = frozenset({"Agent", "Task"})

#: An absent or empty ``subagent_type`` defaults to this one.
BLANK_SUBAGENT_TYPE = "general-purpose"

_REPO_LIST_KEYS = ("workspace_repos", "frontend_repos", "public_repos")
_BRIEF_FIELDS = ("prompt", "description")
_MIN_TOKEN_CHARS = 3

# Per-call escape, mirroring the sibling gates' ``[…-ok: <reason>]`` tokens. The
# leading ``\S`` is what rejects an empty reason.
_GENERAL_PURPOSE_OK_RE = re.compile(r"\[general-purpose-ok:\s*(\S[^\]]*?)\s*\]")
#: Scanned prefix of each brief field — a token buried deep in a long brief must
#: not silently authorise the dispatch (the ``[quote-ok:]`` precedent).
_TOKEN_SCAN_CHARS = 512

_ALTERNATIVES = (
    "t3:coder (implement), t3:debugger (diagnose/fix), t3:tester (tests + CI), "
    "t3:e2e (browser runs), t3:reviewer (review), t3:bughunter (reproduce a defect), "
    "t3:planner (plan), t3:shipper (commit/PR)"
)


def managed_repo_tokens() -> frozenset[str]:
    """Lowercase name tokens of the repos the overlay registry declares managed.

    Each declared slug contributes itself and its path segments, so a brief
    naming either the full ``<namespace>/<repo>`` or the bare repo is caught.
    """
    tokens: set[str] = set()
    for overlay_cfg in overlays_registry().values():
        if not isinstance(overlay_cfg, dict):
            continue
        for key in _REPO_LIST_KEYS:
            declared = overlay_cfg.get(key)
            if not isinstance(declared, list):
                continue
            for slug in declared:
                text = str(slug).strip().lower()
                tokens.update(part for part in (text, *text.split("/")) if len(part) >= _MIN_TOKEN_CHARS)
    return frozenset(tokens)


def named_managed_repo(brief: str) -> str | None:
    """The managed-repo token *brief* names, or ``None``.

    Word-boundary anchored, so ``acme-product`` still matches inside a path like
    ``wt-acme-product-42`` (``-`` and ``/`` are boundaries) while a short token
    never matches inside a longer word. Longest token first, so the match names
    the fully-qualified slug when the brief carries one.
    """
    tokens = managed_repo_tokens()
    if not tokens:
        return None
    alternation = "|".join(re.escape(token) for token in sorted(tokens, key=lambda t: (-len(t), t)))
    match = re.search(rf"\b(?:{alternation})\b", brief, re.IGNORECASE)
    return match.group(0).lower() if match else None


def refusal(repo_token: str) -> str:
    return (
        f"REFUSED: a `general-purpose` sub-agent for `{repo_token}` starts BLANK — no skills, "
        "no repo guidelines, no runbook, no memory — so it rediscovers the stack from scratch "
        "and works outside the workflow. Re-dispatch with an agent that carries the context: "
        f"{_ALTERNATIVES} — or Explore for read-only fan-out search. If this really is generic "
        "work outside that repo, add `[general-purpose-ok: <reason>]` to the prompt."
    )


def _brief_fields(tool_input: dict) -> list[str]:
    values = (tool_input.get(key) for key in _BRIEF_FIELDS)
    return [value for value in values if isinstance(value, str) and value]


def _escape_reason(fields: list[str]) -> str | None:
    for value in fields:
        if match := _GENERAL_PURPOSE_OK_RE.search(value[:_TOKEN_SCAN_CHARS]):
            return match.group(1).strip()
    return None


def _gate_enabled() -> bool:
    """Whether the gate is enabled (default True), fail-open on a broken config."""
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting("general_purpose_agent_gate_enabled", default=True)


def _blank_dispatch_fields(data: dict) -> list[str]:
    """The brief fields of a BLANK ``general-purpose`` dispatch, else empty.

    Matches the ``Agent``/``Task`` tool names EXACTLY: ``TaskCreate`` /
    ``TaskUpdate`` are todo writes that carry a description and no
    ``subagent_type``, so a prefix match would deny them.
    """
    if data.get("tool_name") not in DISPATCH_TOOLS:
        return []
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    if (tool_input.get("subagent_type") or BLANK_SUBAGENT_TYPE) != BLANK_SUBAGENT_TYPE:
        return []
    return _brief_fields(tool_input)


def handle_block_general_purpose_agent(data: dict) -> bool:
    """Deny a blank ``general-purpose`` dispatch whose brief names a managed repo.

    The free checks run before either DB read, so an ordinary typed dispatch
    costs nothing.
    """
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    fields = _blank_dispatch_fields(data)
    if not fields or not _gate_enabled():
        return False
    repo_token = named_managed_repo(" ".join(fields))
    if repo_token is None:
        return False
    if reason := _escape_reason(fields):
        sys.stderr.write(f"NOTE: general-purpose agent gate skipped via [general-purpose-ok: {reason}].\n")
        return False
    return _fail_open_or_deny(data, refusal(repo_token), gate_id="general_purpose_agent_gate")
