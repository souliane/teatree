"""Tests for :mod:`teatree.loop.main_check_runs` — the shared paginated check-runs primitive.

Pure logic (argv shape, page-flattening) — no subprocess, no Django.
"""

import json
from collections.abc import Mapping, Sequence

from teatree.loop.main_check_runs import PAGE_SIZE, check_runs_argv, parse_check_run_pages


class TestCheckRunsArgv:
    def test_requests_every_page(self) -> None:
        argv = check_runs_argv(slug="souliane/teatree", ref="main")

        assert "--paginate" in argv
        assert "--slurp" in argv
        assert "--jq" not in argv, "gh refuses --slurp together with --jq"

    def test_endpoint_names_the_slug_and_ref(self) -> None:
        argv = check_runs_argv(slug="souliane/teatree", ref="deadbeef")

        endpoint = next(a for a in argv if "check-runs" in a)
        assert endpoint.startswith("repos/souliane/teatree/commits/deadbeef/check-runs")

    def test_page_size_defaults_above_githubs_own_default(self) -> None:
        argv = check_runs_argv(slug="o/r", ref="main")

        endpoint = next(a for a in argv if "check-runs" in a)
        assert f"per_page={PAGE_SIZE}" in endpoint
        assert PAGE_SIZE > 30, "GitHub's own unpaginated default page is 30"

    def test_page_size_is_overridable(self) -> None:
        argv = check_runs_argv(slug="o/r", ref="main", page_size=7)

        endpoint = next(a for a in argv if "check-runs" in a)
        assert "per_page=7" in endpoint


def _pages(*pages: Sequence[Mapping[str, object]]) -> str:
    """The ``--slurp`` shape: one list of page bodies, each with its own ``check_runs``."""
    return json.dumps([{"total_count": len(page), "check_runs": page} for page in pages])


class TestParseCheckRunPages:
    def test_flattens_runs_across_multiple_pages(self) -> None:
        out = _pages(
            [{"name": "a", "status": "completed", "conclusion": "success"}],
            [{"name": "b", "status": "completed", "conclusion": "failure"}],
        )

        runs = parse_check_run_pages(out)

        assert runs is not None
        assert {r["name"] for r in runs} == {"a", "b"}

    def test_a_check_beyond_page_one_is_still_found(self) -> None:
        # The regression this module exists to prevent: a check name that would have
        # been truncated off an unpaginated page-1-only read is still present once
        # every ``--slurp``ed page is flattened.
        page_one = [{"name": f"job-{i}", "status": "completed", "conclusion": "success"} for i in range(30)]
        page_two = [{"name": "uv-audit", "status": "completed", "conclusion": "failure"}]
        out = _pages(page_one, page_two)

        runs = parse_check_run_pages(out)

        assert runs is not None
        names = {str(r.get("name")) for r in runs}
        assert "uv-audit" in names

    def test_none_on_unparsable_json(self) -> None:
        assert parse_check_run_pages("not json") is None

    def test_none_when_payload_is_not_a_list(self) -> None:
        assert parse_check_run_pages(json.dumps({"check_runs": []})) is None

    def test_none_on_empty_input(self) -> None:
        assert parse_check_run_pages("") is None

    def test_none_when_every_page_is_empty(self) -> None:
        # Zero evidence must never read as "the check is absent, therefore not
        # failing" — the caller needs to be able to tell "no data" from "checked".
        assert parse_check_run_pages(_pages([], [])) is None

    def test_non_dict_entries_are_skipped(self) -> None:
        good_run = {"name": "a", "status": "completed", "conclusion": "success"}
        out = json.dumps([{"check_runs": ["not-a-dict", good_run]}])

        runs = parse_check_run_pages(out)

        assert runs is not None
        assert [r["name"] for r in runs] == ["a"]
