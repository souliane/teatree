"""Tests for :meth:`GhPrApiClient.main_check_failed` — the uv-audit-fallback probe.

The ``gh`` subprocess is doubled by the paging ``gh_check_runs`` fake, which sits
BELOW the probe's own argv builder: the request shape therefore decides what the
probe can see, exactly as it does against the real endpoint, and the pagination
argv plus the check-runs classification run for real. No test exercised this
``gh``-backed adapter before #4090's sibling fix — every consumer test doubles
the ``PrApiClient`` port instead.
"""

from collections.abc import Callable

import teatree.loop.scanners.pr_sweep_adapters as adapters_mod
from teatree.loop.main_check_runs import PAGE_SIZE
from teatree.loop.scanners.pr_sweep_adapters import GhPrApiClient
from tests.teatree_loop.conftest import FakeGhCheckRuns, check_run

SLUG = "souliane/teatree"
CHECK = "uv-audit"

StubGh = Callable[..., FakeGhCheckRuns]


class TestMainCheckFailed:
    def test_requests_every_page(self, gh_check_runs: StubGh) -> None:
        fake = gh_check_runs(adapters_mod, runs=[check_run(CHECK)])

        GhPrApiClient(token="").main_check_failed(slug=SLUG, check_name=CHECK)

        argv = fake.argv_log[0]
        assert "--paginate" in argv
        assert "--slurp" in argv
        assert "--jq" not in argv, "gh refuses --slurp together with --jq"
        endpoint = next(a for a in argv if "check-runs" in a)
        assert endpoint.startswith(f"repos/{SLUG}/commits/main/check-runs")

    def test_true_when_the_named_check_failed(self, gh_check_runs: StubGh) -> None:
        gh_check_runs(adapters_mod, runs=[check_run(CHECK, conclusion="failure")])

        assert GhPrApiClient(token="").main_check_failed(slug=SLUG, check_name=CHECK) is True

    def test_false_when_the_named_check_succeeded(self, gh_check_runs: StubGh) -> None:
        gh_check_runs(adapters_mod, runs=[check_run(CHECK)])

        assert GhPrApiClient(token="").main_check_failed(slug=SLUG, check_name=CHECK) is False

    def test_false_when_the_named_check_is_pending(self, gh_check_runs: StubGh) -> None:
        gh_check_runs(adapters_mod, runs=[check_run(CHECK, status="in_progress", conclusion="")])

        assert GhPrApiClient(token="").main_check_failed(slug=SLUG, check_name=CHECK) is False

    def test_false_when_the_named_check_is_absent(self, gh_check_runs: StubGh) -> None:
        gh_check_runs(adapters_mod, runs=[check_run("lint")])

        assert GhPrApiClient(token="").main_check_failed(slug=SLUG, check_name=CHECK) is False

    def test_false_on_non_zero_rc(self, gh_check_runs: StubGh) -> None:
        gh_check_runs(adapters_mod, returncode=1)

        assert GhPrApiClient(token="").main_check_failed(slug=SLUG, check_name=CHECK) is False

    def test_false_on_unparsable_output(self, gh_check_runs: StubGh) -> None:
        gh_check_runs(adapters_mod, raw_stdout="not json")

        assert GhPrApiClient(token="").main_check_failed(slug=SLUG, check_name=CHECK) is False

    def test_a_failing_check_past_the_first_page_is_still_found(self, gh_check_runs: StubGh) -> None:
        """The concrete failure mode, end to end: the failing check is the last of many.

        ``main`` carries one more check-run than the request's own page holds, so
        the named one is reachable only by following the next page — past GitHub's
        30-run default page AND past the larger page this probe asks for. The fake
        serves page 1 alone to an argv that does not ask for the rest, so
        ``runs_served`` is the truncation an unpaginated read suffers.
        """
        runs = [check_run(f"job-{i}") for i in range(PAGE_SIZE)] + [check_run(CHECK, conclusion="failure")]
        fake = gh_check_runs(adapters_mod, runs=runs)

        failed = GhPrApiClient(token="").main_check_failed(slug=SLUG, check_name=CHECK)

        assert fake.pages_served == 2, "the probe stopped at page 1 — the failing check was never read"
        assert fake.runs_served == len(runs)
        assert failed is True
