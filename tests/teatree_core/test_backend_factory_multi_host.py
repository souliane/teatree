"""An overlay with both code-host tokens configures both hosts (#976).

Pre-fix, ``get_code_host`` returned the first matching backend (GitHub when
both tokens were present), so any overlay with both GitHub and GitLab tokens
silently lost the second platform's PRs/issues/reviews. The factory now
exposes ``OverlayBackends.hosts`` — one entry per platform whose token resolved
— and the loop builds one scanner job per host. ``host`` remains as a back-compat
alias pointing at the first entry.
"""

from unittest.mock import patch

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.backends.github import GitHubCodeHost
from teatree.backends.gitlab import GitLabCodeHost
from teatree.core.backend_factory import iter_overlay_backends, reset_backend_caches
from teatree.core.overlay import OverlayBase, OverlayConfig


class _DualTokenConfig(OverlayConfig):
    code_host: str = ""  # auto-pick: trigger BOTH when both tokens present

    def get_github_token(self) -> str:
        return "gh-test-token"

    def get_gitlab_token(self) -> str:
        return "gl-test-token"


class _DualTokenOverlay(OverlayBase):
    config = _DualTokenConfig()

    def get_repos(self) -> list[str]:
        return []

    def get_provision_steps(self, worktree):
        _ = worktree
        return []


def setup_function() -> None:
    reset_backend_caches()


def teardown_function() -> None:
    reset_backend_caches()


def test_overlay_with_both_tokens_exposes_both_hosts() -> None:
    """An overlay with both PATs must produce two CodeHostBackend entries."""
    with patch.object(
        overlay_loader_mod,
        "_discover_overlays",
        return_value={"dual": _DualTokenOverlay()},
    ):
        backends = iter_overlay_backends()

    [dual] = [b for b in backends if b.name == "dual"]
    hosts = list(dual.hosts)
    types = sorted(type(h).__name__ for h in hosts)
    assert types == [GitHubCodeHost.__name__, GitLabCodeHost.__name__], (
        f"both code hosts should resolve when both tokens are set; got {types!r}"
    )


def test_hosts_is_back_compatible_with_single_host_field() -> None:
    """``host`` keeps returning the first ``hosts`` entry — legacy callers stay green."""
    with patch.object(
        overlay_loader_mod,
        "_discover_overlays",
        return_value={"dual": _DualTokenOverlay()},
    ):
        backends = iter_overlay_backends()

    [dual] = [b for b in backends if b.name == "dual"]
    assert dual.host is dual.hosts[0]


def test_identity_aliases_threaded_from_user_settings(tmp_path) -> None:
    """`[teatree] user_identity_aliases` lands on every overlay's `identities`."""
    from teatree.config import load_config  # noqa: PLC0415

    cfg_path = tmp_path / "teatree.toml"
    cfg_path.write_text(
        '[teatree]\nuser_identity_aliases = ["user-main", "user-alt"]\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)

    with (
        patch("teatree.config.load_config", return_value=cfg),
        patch.object(
            overlay_loader_mod,
            "_discover_overlays",
            return_value={"dual": _DualTokenOverlay()},
        ),
    ):
        backends = iter_overlay_backends()

    [dual] = [b for b in backends if b.name == "dual"]
    assert dual.identities == ("user-main", "user-alt"), f"aliases must reach the backend; got {dual.identities!r}"
