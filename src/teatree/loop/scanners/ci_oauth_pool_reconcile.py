"""Hourly reconciliation of the configured OAuth accounts into CI's eval pool."""

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from teatree.backends.github.ci_eval_client import DEFAULT_CI_EVAL_REPO
from teatree.ci_oauth_switch import CiAccountSwitcher, CiAccountSwitchError
from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.token_rows import TokenAccountRow


def _cached_rows() -> list["TokenAccountRow"]:
    from teatree.token_report import TokenReport  # noqa: PLC0415 — requires ready Django apps

    return TokenReport(from_cache=True).rows()


def _declared_deployment_repo() -> str:
    source = urlparse(os.environ.get("TEATREE_REPO_URL", ""))
    if source.hostname != "github.com":
        return ""
    slug = source.path.strip("/").removesuffix(".git")
    owner, separator, name = slug.partition("/")
    return slug if owner and separator and name and "/" not in name else ""


@dataclass(slots=True)
class CiOauthPoolReconcileScanner:
    """Refresh a write-only GitHub secret from every configured pass entry.

    The housekeeping loop supplies the hourly cadence. Account health comes from
    the cache: reconciliation is about membership, not a fresh Anthropic probe.
    The switcher resolves every secret before writing the replacement pool.
    CI_OAUTH_POOL_REPO opts into a target; TEATREE_REPO_URL declares deployment
    ownership. Only the core repo may use the legacy default target.
    """

    repo: str | None = None
    deployment_repo: str | None = None
    rows: Callable[[], Sequence["TokenAccountRow"]] = _cached_rows
    switcher: Callable[[str], CiAccountSwitcher] = lambda repo: CiAccountSwitcher(repo=repo)
    name: str = "ci_oauth_pool_reconcile"

    def scan(self) -> list[ScanSignal]:
        deployment_repo = self.deployment_repo if self.deployment_repo is not None else _declared_deployment_repo()
        repo = self.repo if self.repo is not None else os.environ.get("CI_OAUTH_POOL_REPO", "").strip()
        if not repo and deployment_repo == DEFAULT_CI_EVAL_REPO:
            repo = DEFAULT_CI_EVAL_REPO
        if not repo:
            return [ScanSignal("ci_oauth_pool.disabled", "CI OAuth pool disabled: no owned repository configured")]
        if repo != deployment_repo:
            return [
                ScanSignal(
                    "ci_oauth_pool.unowned",
                    f"CI OAuth pool reconciliation skipped: deployment does not own {repo}",
                    {"repo": repo, "deployment_repo": deployment_repo},
                )
            ]
        try:
            accounts = self.switcher(repo).reconcile_pool(self.rows())
        except CiAccountSwitchError as exc:
            return [ScanSignal("ci_oauth_pool.failed", f"CI OAuth pool reconciliation failed: {exc}")]
        except Exception as exc:  # noqa: BLE001 — keep the loop alive; do not expose unknown secret-bearing errors
            return [ScanSignal("ci_oauth_pool.failed", f"CI OAuth pool reconciliation failed ({type(exc).__name__})")]
        return [
            ScanSignal(
                "ci_oauth_pool.reconciled",
                f"CI OAuth pool reconciled: {len(accounts)} configured account(s)",
                {"repo": repo, "accounts": list(accounts)},
            )
        ]
