"""The ``scenario-plan`` section of the test-plan render layer (teatree #272).

The acceptance-scenario shape — a ``### Scenario N — <surface>`` block with
Preconditions / numbered Steps / Expected / Actual and either captioned inline
screenshots or an API-contract marker — split out of :mod:`._test_plan_render`
so each module stays a single concern under the module-health cap. Everything
here is a pure transform over the persisted scenario data; nothing touches the
ORM, the code host, or the CLI.

The dependency is one-directional: ``_test_plan_render`` imports these types and
:func:`render_scenario_plan`; this module imports nothing from the render layer.
"""

from collections.abc import Mapping
from typing import NotRequired, TypedDict


def _as_dict(value: object) -> Mapping[str, object]:
    """``value`` as a read-only mapping when it is a dict, else ``{}``."""
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    """Return *value* as a ``list[object]`` when it is a list, else ``[]``."""
    return list(value) if isinstance(value, list) else []


class ScenarioImage(TypedDict):
    """One captioned inline screenshot for a ``scenario-plan`` scenario."""

    slot: str
    caption: str
    image_md: str


class Scenario(TypedDict):
    """One acceptance scenario in the ``scenario-plan`` template.

    ``modality`` is ``"ui"`` (captioned screenshots) or ``"api"`` (a contract
    check with no screenshot). ``actual_pass`` renders the ``✅ Pass.`` marker;
    when False, ``actual_note`` carries the blocked/omitted reason instead.
    """

    surface: str
    title: str
    preconditions: str
    steps: list[str]
    expected: str
    modality: str
    actual_pass: bool
    images: list[ScenarioImage]
    actual_note: NotRequired[str]


def _coerce_scenario_image(raw: object) -> ScenarioImage:
    raw_dict = _as_dict(raw)
    return {
        "slot": str(raw_dict.get("slot") or ""),
        "caption": str(raw_dict.get("caption") or ""),
        "image_md": str(raw_dict.get("image_md") or ""),
    }


def _coerce_scenario(raw: object) -> Scenario:
    raw_dict = _as_dict(raw)
    scenario: Scenario = {
        "surface": str(raw_dict.get("surface") or ""),
        "title": str(raw_dict.get("title") or ""),
        "preconditions": str(raw_dict.get("preconditions") or ""),
        "steps": [str(s) for s in _as_list(raw_dict.get("steps"))],
        "expected": str(raw_dict.get("expected") or ""),
        "modality": "api" if str(raw_dict.get("modality") or "").strip() == "api" else "ui",
        "actual_pass": bool(raw_dict.get("actual_pass")),
        "images": [_coerce_scenario_image(i) for i in _as_list(raw_dict.get("images"))],
    }
    note = str(raw_dict.get("actual_note") or "").strip()
    if note:
        scenario["actual_note"] = note
    return scenario


def coerce_scenarios(raw: object) -> list[Scenario]:
    """Rebuild the ordered scenario list, dropping entries with no surface."""
    return [scenario for scenario in (_coerce_scenario(s) for s in _as_list(raw)) if scenario["surface"]]


class ScenarioSection(TypedDict):
    """The recovered ``scenario-plan`` keys, each present only when non-empty."""

    scenarios: NotRequired[list[Scenario]]
    scenario_intro: NotRequired[str]
    environment: NotRequired[str]


def coerce_scenario_section(raw_dict: Mapping[str, object]) -> ScenarioSection:
    """The ``scenarios``/``scenario_intro``/``environment`` keys recovered from a state blob.

    Each key is included only when it carries content, so a non-scenario state
    round-trips with no empty scenario fields.
    """
    section: ScenarioSection = {}
    scenarios = coerce_scenarios(raw_dict.get("scenarios"))
    if scenarios:
        section["scenarios"] = scenarios
    intro = str(raw_dict.get("scenario_intro") or "").strip()
    if intro:
        section["scenario_intro"] = intro
    environment = str(raw_dict.get("environment") or "").strip()
    if environment:
        section["environment"] = environment
    return section


def _scenario_actual_lines(scenario: Scenario) -> list[str]:
    """The ``**Actual:**`` line: a ``✅ Pass.`` marker, or the blocked/omitted note."""
    if scenario.get("actual_pass"):
        return ["**Actual:** ✅ Pass.", ""]
    note = scenario.get("actual_note") or "Not verified."
    return [f"**Actual:** {note}", ""]


def _scenario_evidence_lines(scenario: Scenario) -> list[str]:
    """Captioned inline screenshots for a UI scenario, or the API-contract block."""
    if scenario.get("modality") == "api":
        return ["_API contract check — no screenshot._", ""]
    lines: list[str] = []
    for image in scenario.get("images", []):
        caption = image.get("caption", "")
        if caption:
            lines.append(f"*{caption}*")
        if image.get("image_md"):
            lines.append(image["image_md"])
        lines.append("")
    return lines


def _scenario_block(scenario: Scenario, *, index: int) -> list[str]:
    """One ``### Scenario N — <surface>`` block: preconditions, steps, expected, actual, evidence."""
    lines = [f"### Scenario {index} — {scenario.get('surface', '')}", ""]
    title = scenario.get("title", "")
    if title:
        lines.extend((f"**{title}**", ""))
    preconditions = scenario.get("preconditions", "")
    if preconditions:
        lines.extend((f"**Preconditions:** {preconditions}", ""))
    steps = scenario.get("steps", [])
    if steps:
        lines.extend(("**Steps:**", ""))
        lines.extend(f"{i}. {step}" for i, step in enumerate(steps, start=1))
        lines.append("")
    expected = scenario.get("expected", "")
    if expected:
        lines.extend((f"**Expected:** {expected}", ""))
    lines.extend(_scenario_actual_lines(scenario))
    lines.extend(_scenario_evidence_lines(scenario))
    return lines


def render_scenario_plan(state: Mapping[str, object]) -> list[str]:
    """``scenario-plan`` body lines: optional intro, per-scenario blocks, ``---`` separators, env footer.

    Reads its own keys off the persisted state mapping (``scenarios``,
    ``scenario_intro``, ``environment``) so the render layer dispatches with a
    single argument and this module needs no back-reference to ``TestPlanState``.
    """
    lines: list[str] = []
    intro = str(state.get("scenario_intro") or "")
    if intro:
        lines.extend((intro, ""))
    for index, scenario in enumerate(coerce_scenarios(state.get("scenarios")), start=1):
        if index > 1:
            lines.extend(("---", ""))
        lines.extend(_scenario_block(scenario, index=index))
    environment = str(state.get("environment") or "")
    if environment:
        lines.extend(("---", "", f"**Environment:** {environment}", ""))
    return lines
