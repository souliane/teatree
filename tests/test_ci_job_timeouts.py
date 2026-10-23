"""Every CI job states its own wall-clock ceiling (#5006).

Without one a job inherits GitHub's 360-minute default, so a single hung job holds
one of the account's 20 concurrent runners for six hours while every later PR queues.
"""

from pathlib import Path

import yaml

_CI = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def test_every_ci_job_declares_timeout_minutes() -> None:
    jobs = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]
    missing = sorted(name for name, job in jobs.items() if not isinstance(job.get("timeout-minutes"), int))
    assert not missing, f"ci.yml jobs without an integer timeout-minutes (default 360): {missing}"


def test_dash_e2e_timeout_covers_cold_browser_install_and_both_suites() -> None:
    jobs = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]
    cold_browser_install_minutes = 7
    browser_suite_minutes = 2
    dashboard_suite_minutes = 2
    runner_setup_minutes = 1
    safety_margin_minutes = 2

    assert jobs["dash-e2e"]["timeout-minutes"] >= (
        cold_browser_install_minutes
        + browser_suite_minutes
        + dashboard_suite_minutes
        + runner_setup_minutes
        + safety_margin_minutes
    )
