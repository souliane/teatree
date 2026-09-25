"""The hourly housekeeping loop reconciles the complete CI eval OAuth pool."""

import os
from contextlib import ExitStack
from unittest.mock import Mock, patch

import teatree.loop.global_scanner_factories as scanner_factories
from teatree.ci_oauth_switch import CiAccountSwitchError
from teatree.loop.dispatch_tables import STATUSLINE_DROP_KINDS, STATUSLINE_ZONE_BY_KIND
from teatree.loop.global_scanner_factories import build_default_jobs
from teatree.loop.scanners.ci_oauth_pool_reconcile import CiOauthPoolReconcileScanner
from teatree.loops.housekeeping.loop import _build_jobs


def test_scanner_reconciles_cached_configured_rows() -> None:
    rows = [object(), object()]
    switcher = Mock()
    switcher.reconcile_pool.return_value = ("primary", "spare")
    scanner = CiOauthPoolReconcileScanner(
        repo="souliane/teatree",
        deployment_repo="souliane/teatree",
        rows=lambda: rows,
        switcher=lambda _: switcher,
    )

    signals = scanner.scan()

    switcher.reconcile_pool.assert_called_once_with(rows)
    assert signals[0].kind == "ci_oauth_pool.reconciled"
    assert "2" in signals[0].summary


def test_scanner_surfaces_reconciliation_failure() -> None:
    switcher = Mock()
    switcher.reconcile_pool.side_effect = CiAccountSwitchError("permission denied")
    scanner = CiOauthPoolReconcileScanner(
        repo="souliane/teatree",
        deployment_repo="souliane/teatree",
        rows=lambda: [object()],
        switcher=lambda _: switcher,
    )

    signals = scanner.scan()

    assert signals[0].kind == "ci_oauth_pool.failed"
    assert "permission denied" in signals[0].summary


def test_housekeeping_registers_one_global_reconciler() -> None:
    jobs = _build_jobs(backends=[])

    assert sum(isinstance(job.scanner, CiOauthPoolReconcileScanner) for job in jobs) == 1


def test_default_tick_does_not_register_reconciler() -> None:
    with ExitStack() as stack:
        for name in vars(scanner_factories):
            if name.endswith("_scanner") and name.startswith("_"):
                stack.enter_context(patch.object(scanner_factories, name, return_value=None))
        jobs = build_default_jobs(backends=[])

    assert not any(isinstance(job.scanner, CiOauthPoolReconcileScanner) for job in jobs)


def test_foreign_deployment_cannot_reconcile_an_unowned_repo() -> None:
    switcher = Mock()
    scanner = CiOauthPoolReconcileScanner(
        repo="souliane/teatree",
        deployment_repo="other-org/foreign-factory",
        rows=lambda: [object()],
        switcher=lambda _: switcher,
    )

    signals = scanner.scan()

    switcher.assert_not_called()
    assert [signal.kind for signal in signals] == ["ci_oauth_pool.unowned"]
    assert STATUSLINE_ZONE_BY_KIND[signals[0].kind] == "action_needed"


def test_foreign_environment_does_not_write_core_secret() -> None:
    switcher_factory = Mock()
    scanner = CiOauthPoolReconcileScanner(rows=lambda: [object()], switcher=switcher_factory)

    with patch.dict(
        os.environ,
        {"TEATREE_REPO_URL": "", "CI_OAUTH_POOL_REPO": "souliane/teatree"},
    ):
        signals = scanner.scan()

    switcher_factory.assert_not_called()
    assert [signal.kind for signal in signals] == ["ci_oauth_pool.unowned"]


def test_missing_repository_configuration_is_disabled_not_failed() -> None:
    switcher = Mock()
    scanner = CiOauthPoolReconcileScanner(
        deployment_repo="other-org/foreign-factory",
        rows=lambda: [object()],
        switcher=lambda _: switcher,
    )

    with patch.dict(os.environ, {"CI_OAUTH_POOL_REPO": ""}):
        signals = scanner.scan()

    switcher.assert_not_called()
    assert [signal.kind for signal in signals] == ["ci_oauth_pool.disabled"]
    assert signals[0].kind in STATUSLINE_DROP_KINDS


def test_default_repository_is_owned_only_by_declared_core_deployment() -> None:
    switcher = Mock()
    switcher.reconcile_pool.return_value = ("primary",)
    switcher_factory = Mock(return_value=switcher)
    scanner = CiOauthPoolReconcileScanner(rows=lambda: [object()], switcher=switcher_factory)

    with patch.dict(
        os.environ,
        {"TEATREE_REPO_URL": "https://github.com/souliane/teatree.git", "CI_OAUTH_POOL_REPO": ""},
    ):
        signals = scanner.scan()

    switcher_factory.assert_called_once_with("souliane/teatree")
    assert [signal.kind for signal in signals] == ["ci_oauth_pool.reconciled"]
