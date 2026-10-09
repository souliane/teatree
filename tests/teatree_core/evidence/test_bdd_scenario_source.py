"""The FINAL BDD source lives in one hidden comment; nothing about it is reader-visible."""

import json
import re

import pytest

from teatree.core.evidence import bdd_scenario_source as bdd

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_SOURCE: bdd.BddScenarioSource = {
    "prd_page": "https://notion.so/example-prd",
    "bdd_revision": "2026-09-22",
    "status": "final",
    "scenario_ids": ["BDD-4101", "BDD-9300-002"],
}
_VISIBLE_BLOCK = """\
### Scenario source

- **PRD page:** https://notion.so/example-prd
- **BDD revision:** 2026-09-22
- **BDD status:** final
- **Final scenario IDs:** `BDD-4101`, `BDD-9300-002`
"""


def _plan(*parts: str) -> str:
    return "\n\n".join((*parts, "## Test Plan\n"))


def _declaration(body: str) -> str:
    return f"<!-- t3-bdd-source {body} -->"


def _refusal(body: str) -> bdd.BddSourceError:
    with pytest.raises(bdd.BddSourceError) as caught:
        bdd.validate_bdd_source(body)
    return caught.value


def test_rendered_declaration_is_invisible_and_round_trips() -> None:
    rendered = bdd.render_bdd_source(_SOURCE)

    assert _COMMENT.sub("", rendered).strip() == ""
    assert bdd.validate_bdd_source(_plan(rendered)) == _SOURCE


def test_visible_field_block_refuses_even_beside_a_hidden_declaration() -> None:
    error = _refusal(_plan(bdd.render_bdd_source(_SOURCE), _VISIBLE_BLOCK))

    assert error.kind == "bdd-source"
    assert "visible" in str(error)


def test_customer_prose_scenario_source_is_not_a_leak() -> None:
    prose = (
        "### Scenario source\n\nThese scenarios follow the customer's own requirement page, cases REQ-001 to REQ-014.\n"
    )

    bdd.refuse_visible_bdd_source(prose)


def test_field_lines_inside_a_comment_are_not_visible() -> None:
    hidden_block = f"<!--\n{_VISIBLE_BLOCK}-->"

    assert bdd.validate_bdd_source(_plan(bdd.render_bdd_source(_SOURCE), hidden_block)) == _SOURCE


def test_missing() -> None:
    error = _refusal(_plan())

    assert error.kind == "bdd-source"
    assert "missing" in str(error)
    assert "t3-bdd-source" in str(error)


def test_two_declarations() -> None:
    rendered = bdd.render_bdd_source(_SOURCE)

    error = _refusal(_plan(rendered, rendered))

    assert error.kind == "bdd-source"
    assert "exactly one" in str(error)


def test_preflight_names_status() -> None:
    error = _refusal(_plan(_declaration(json.dumps(_SOURCE | {"status": "preflight"}))))

    assert error.kind == "bdd-source"
    assert "preflight" in str(error)


@pytest.mark.parametrize(("body", "named"), [("{not json}", "not valid JSON"), ('["a"]', "JSON object")])
def test_malformed_declaration_json_fails_closed(body: str, named: str) -> None:
    error = _refusal(_plan(_declaration(body)))

    assert error.kind == "bdd-source"
    assert named in str(error)
    assert "missing" not in str(error)


@pytest.mark.parametrize("trace", ["BDD-4101", '{"ids": ["BDD-4101"]}', "[1]"])
def test_malformed_trace_fails_closed(trace: str) -> None:
    error = _refusal(_plan(bdd.render_bdd_source(_SOURCE), f"<!-- t3-bdd-trace {trace} -->"))

    assert error.kind == "bdd-trace"


def test_legacy_covers_single_segment_id_is_traced() -> None:
    error = _refusal(_plan(bdd.render_bdd_source(_SOURCE), "<!-- covers BDD-999 -->"))

    assert error.kind == "bdd-trace"
    assert "BDD-999" in str(error)


def test_coerce_keeps_invalid_stored_shape() -> None:
    preflight = _SOURCE | {"status": "preflight"}

    assert bdd.coerce_bdd_source(preflight) == preflight
    assert bdd.coerce_bdd_source({"status": "final"}) == {
        "prd_page": "",
        "bdd_revision": "",
        "status": "final",
        "scenario_ids": [],
    }
    assert bdd.coerce_bdd_source(["not", "a", "mapping"]) is None
