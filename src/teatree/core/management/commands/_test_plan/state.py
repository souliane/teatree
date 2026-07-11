"""The persisted test-plan state model — the source of truth for the merge.

The typed :class:`TestPlanState` is serialised into the hidden
``<!-- t3-e2e-data {…} -->`` blob on the one-note-per-ticket test-plan note
(teatree #272). This module owns the state schema, its defensive JSON
coercion, and the note-marker parse/emit — every function a pure transform
over strings and the persisted mapping, no ORM / code-host / CLI.
"""

import json
import re
from collections.abc import Mapping
from typing import NotRequired, TypedDict

from teatree.core.management.commands._test_plan.scenario import ScenarioSection, coerce_scenario_section

# The two columns of every workflow table. Dev on the LEFT, Local on the RIGHT.
_ENVS = ("dev", "local")

# The known body templates; the default is the side-by-side capture matrix.
DEFAULT_TEMPLATE = "capture-matrix"
KNOWN_TEMPLATES = (DEFAULT_TEMPLATE, "browser-click-first", "link-api", "scenario-plan")

# The hidden idempotency marker — keyed on the TICKET (its number, e.g. 8521),
# so a ticket carries exactly ONE test-plan note across all environments.
#
# The emitted string stays ``t3-e2e-evidence`` (NOT renamed to ``t3-test-plan``)
# on purpose: it is PERSISTED in live GitLab/GitHub ticket notes. Changing the
# emitted marker would break idempotent update of every note posted before this
# rename — a fresh note would be created beside the stale one. The regex below
# is therefore the durable wire format; the concept rename is user-facing only.
_TICKET_MARKER_RE = re.compile(r"<!--\s*t3-e2e-evidence\s+ticket=(?P<ticket>\S+)\s*-->")
# The hidden machine-readable state blob — the source of truth for the merge.
_DATA_BLOB_RE = re.compile(r"<!--\s*t3-e2e-data\s+(?P<json>\{.*?\})\s*-->", re.DOTALL)


class TestPlanValidationError(ValueError):
    """Raised when a manifest fails pre-post validation; the note is NOT posted."""


def _as_dict(value: object) -> Mapping[str, object]:
    """``value`` as a read-only mapping when it is a dict, else ``{}``."""
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    """Return *value* as a ``list[object]`` when it is a list, else ``[]``."""
    return list(value) if isinstance(value, list) else []


class WorkflowEmbed(TypedDict):
    """One workflow's rendered embed markdown for one side: the video + screenshots."""

    video_md: str
    image_md: list[str]
    link_md: NotRequired[str]
    code_md: NotRequired[str]


class SideState(TypedDict):
    """One side (dev/local) of the persisted state: commits, workflows, optional gap."""

    commits: dict[str, str]
    workflows: dict[str, WorkflowEmbed]
    missing_on_dev: NotRequired[list[str]]


class TestPlanState(ScenarioSection):
    """The full persisted note state — serialised into the hidden ``t3-e2e-data`` blob.

    ``template``: ``"capture-matrix"`` (default), ``"browser-click-first"``,
    ``"link-api"``, or ``"scenario-plan"``. ``steps``: workflow → ordered step
    list (shared across sides, persisted across re-runs). ``blocked_workflows``:
    workflow → reason string. The ``scenario-plan`` keys
    (``scenarios``/``scenario_intro``/``environment``) come from
    :class:`ScenarioSection`.
    """

    ticket: str
    title: str
    mrs: list[str]
    dev: SideState
    local: SideState
    steps: dict[str, list[str]]
    template: NotRequired[str]
    blocked_workflows: NotRequired[dict[str, str]]


def empty_state(*, ticket: str, title: str) -> TestPlanState:
    """A fresh state with both sides empty."""
    return {
        "ticket": ticket,
        "title": title,
        "mrs": [],
        "dev": {"commits": {}, "missing_on_dev": [], "workflows": {}},
        "local": {"commits": {}, "workflows": {}},
        "steps": {},
    }


def _coerce_workflow(raw: object) -> WorkflowEmbed:
    raw_dict = _as_dict(raw)
    embed: WorkflowEmbed = {
        "video_md": str(raw_dict.get("video_md") or ""),
        "image_md": [str(i) for i in _as_list(raw_dict.get("image_md"))],
    }
    if raw_dict.get("link_md"):
        embed["link_md"] = str(raw_dict["link_md"])
    if raw_dict.get("code_md"):
        embed["code_md"] = str(raw_dict["code_md"])
    return embed


def _coerce_side(raw: object, *, env: str) -> SideState:
    raw_dict = _as_dict(raw)
    commits = {str(k): str(v) for k, v in _as_dict(raw_dict.get("commits")).items()}
    workflows = {str(k): _coerce_workflow(v) for k, v in _as_dict(raw_dict.get("workflows")).items()}
    side: SideState = {"commits": commits, "workflows": workflows}
    if env == "dev":
        side["missing_on_dev"] = [str(m) for m in _as_list(raw_dict.get("missing_on_dev"))]
    return side


def coerce_state(raw: object) -> TestPlanState:
    """Build a well-typed :class:`TestPlanState` from a JSON blob; drops malformed fields."""
    raw_dict = _as_dict(raw)
    state: TestPlanState = {
        "ticket": str(raw_dict.get("ticket") or ""),
        "title": str(raw_dict.get("title") or ""),
        "mrs": [str(m) for m in _as_list(raw_dict.get("mrs"))],
        "dev": _coerce_side(raw_dict.get("dev"), env="dev"),
        "local": _coerce_side(raw_dict.get("local"), env="local"),
        "steps": _coerce_steps(raw_dict.get("steps")),
    }
    template = str(raw_dict.get("template") or "").strip()
    if template in KNOWN_TEMPLATES:
        state["template"] = template
    blocked = _coerce_blocked_workflows(raw_dict.get("blocked_workflows"))
    if blocked:
        state["blocked_workflows"] = blocked
    state.update(coerce_scenario_section(raw_dict))
    return state


def _coerce_blocked_workflows(raw: object) -> dict[str, str]:
    """Rebuild the workflow → blocked-reason mapping, dropping malformed entries."""
    return {str(name): str(reason) for name, reason in _as_dict(raw).items() if str(name) and str(reason)}


def _coerce_steps(raw: object) -> dict[str, list[str]]:
    """Rebuild the workflow → test-plan-steps mapping, dropping malformed entries."""
    return {str(name): [str(s) for s in _as_list(steps)] for name, steps in _as_dict(raw).items() if _as_list(steps)}


def test_plan_marker(*, ticket_id: str) -> str:
    """Hidden HTML-comment idempotency marker; matched by :data:`_TICKET_MARKER_RE`."""
    return f"<!-- t3-e2e-evidence ticket={ticket_id} -->"


def parse_state_blob(body: str) -> TestPlanState:
    """Recover the persisted state from a note body, or an empty state when absent/corrupt."""
    match = _DATA_BLOB_RE.search(body)
    if match is None:
        return empty_state(ticket="", title="")
    try:
        data = json.loads(match.group("json"))
    except json.JSONDecodeError:
        return empty_state(ticket="", title="")
    return coerce_state(data)


def find_ticket_marker(body: str, *, ticket_id: str) -> bool:
    """True iff *body* carries this ticket's test-plan marker."""
    match = _TICKET_MARKER_RE.search(body)
    return match is not None and match.group("ticket") == ticket_id
