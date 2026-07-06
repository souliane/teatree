"""The non-default workflow-shaped body templates (``browser-click-first`` / ``link-api``).

Split out of :mod:`.render` so the render layer stays a single
concern under the module-health cap, mirroring the ``scenario-plan`` split in
:mod:`.scenario`. Both alternate renderers iterate the merged state's
workflows the same way the default capture-matrix table does; they reuse the
render layer's two shared helpers — workflow enumeration and the numbered
``How to test`` block — passed in as callables so the runtime dependency stays
one-directional (render imports these renderers; this module imports the state
types only under ``TYPE_CHECKING``).
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.core.management.commands._test_plan.render import TestPlanState


def render_browser_click_first(
    state: "TestPlanState",
    *,
    workflow_names: "Callable[[TestPlanState], list[str]]",
) -> list[str]:
    """``browser-click-first`` template: numbered steps then inline screenshots."""
    lines: list[str] = []
    for workflow in workflow_names(state):
        lines.extend((f"### {workflow}", ""))
        steps = state.get("steps", {}).get(workflow, [])
        if steps:
            lines.extend(("**How to test:**", ""))
            lines.extend(f"{i}. {step}" for i, step in enumerate(steps, start=1))
            lines.append("")
        for side in (state["dev"], state["local"]):
            lines.extend(side.get("workflows", {}).get(workflow, {}).get("image_md", []))
        lines.append("")
    return lines


def render_link_api(
    state: "TestPlanState",
    *,
    workflow_names: "Callable[[TestPlanState], list[str]]",
    test_plan_block: "Callable[[TestPlanState, str], list[str]]",
) -> list[str]:
    """``link-api`` template: How-to-test steps then link_md + code_md per workflow, no table, no images."""
    lines: list[str] = []
    for workflow in workflow_names(state):
        lines.extend((f"### {workflow}", ""))
        lines.extend(test_plan_block(state, workflow))
        for side in (state["dev"], state["local"]):
            embed = side.get("workflows", {}).get(workflow, {})
            link_md = embed.get("link_md", "") if embed else ""
            code_md = embed.get("code_md", "") if embed else ""
            if link_md:
                lines.append(link_md)
            if code_md:
                lines.append(code_md)
        lines.append("")
    return lines
