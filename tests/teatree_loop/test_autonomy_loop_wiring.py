"""The loop consumers see the per-overlay ``autonomy`` collapse (souliane/teatree#1668).

The as-built #1668 PR collapsed the three approval gates in
``get_effective_settings()`` but ``_effective_settings_for_overlay`` — the
resolver the loop's scanner-builders use — did ``replace(base, **overrides)``
and never routed through ``_apply_autonomy``. So the auto-merge / codex
consumers (``pr_sweep`` ``solo_overlay`` gate, ``_codex_review_scanner_for``)
were BLIND to the collapse: a ``full``/``notify`` overlay's merge autonomy was
a silent no-op in the loop. These tests pin the collapse through the loop
resolver AND the resulting ``solo_overlay`` decision.

Colleague-gate (#1668 ``notify`` tier): the single-author ``solo_overlay``
bypass — a direct ``gh pr merge`` that skips the per-diff CLEAR — must be
reachable ONLY under ``autonomy = "full"``. The collaborative ``notify`` tier
collapses the same merge gates (``mode = auto`` +
``require_human_approval_to_merge = false``) but must keep the CLEAR path so
the user's MR merges only after a colleague approval and the agent never
self-approves its own MR.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from teatree.config import Autonomy, Mode
from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.overlay import OverlayBase, OverlayConfig, OverlayMetadata
from teatree.loop.scanner_factories import _effective_settings_for_overlay, _pr_sweep_scanner_for


def _stage_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(toml, encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])


def _backend(
    *,
    name: str,
    repos: tuple[str, ...] = ("acme/repo",),
    identities: tuple[str, ...] = (),
) -> OverlayBackends:
    config = MagicMock(spec=OverlayConfig)
    config.get_github_token = lambda: ""
    metadata = MagicMock(spec=OverlayMetadata)
    metadata.get_followup_repos = lambda: list(repos)
    overlay = MagicMock(spec=OverlayBase)
    overlay.config = config
    overlay.metadata = metadata
    return OverlayBackends(
        name=name,
        hosts=(MagicMock(spec=CodeHostBackend),),
        messaging=None,
        ready_labels=(),
        overlay=overlay,
        identities=identities,
    )


class TestEffectiveSettingsForOverlaySeesCollapse:
    """A.3 — the loop resolver routes through ``_apply_autonomy``."""

    def test_full_overlay_resolver_sees_collapsed_values(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stage_config(
            tmp_path,
            monkeypatch,
            '[teatree]\n[overlays.t3-teatree]\nautonomy = "full"\n',
        )
        settings = _effective_settings_for_overlay("t3-teatree")
        assert settings.autonomy is Autonomy.FULL
        assert settings.mode is Mode.AUTO
        assert settings.require_human_approval_to_merge is False

    def test_notify_overlay_resolver_sees_collapsed_values(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stage_config(
            tmp_path,
            monkeypatch,
            '[teatree]\n[overlays.t3-client]\nautonomy = "notify"\n',
        )
        settings = _effective_settings_for_overlay("t3-client")
        assert settings.autonomy is Autonomy.NOTIFY
        assert settings.mode is Mode.AUTO
        assert settings.require_human_approval_to_merge is False
        assert settings.notify_on_behalf is True

    def test_global_interactive_mode_does_not_defeat_collapse_in_loop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The over-pin fix is visible through the loop resolver too."""
        monkeypatch.delenv("T3_MODE", raising=False)
        _stage_config(
            tmp_path,
            monkeypatch,
            '[teatree]\nmode = "interactive"\n[overlays.t3-teatree]\nautonomy = "full"\n',
        )
        settings = _effective_settings_for_overlay("t3-teatree")
        assert settings.mode is Mode.AUTO

    def test_babysit_overlay_resolver_keeps_conservative_values(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stage_config(
            tmp_path,
            monkeypatch,
            '[teatree]\n[overlays.careful]\nautonomy = "babysit"\n',
        )
        settings = _effective_settings_for_overlay("careful")
        assert settings.autonomy is Autonomy.BABYSIT
        assert settings.require_human_approval_to_merge is True


class TestPrSweepSoloOverlayGate:
    """B.4 — the single-author CLEAR-skipping bypass is exclusive to ``full``."""

    def test_full_overlay_enables_solo_bypass(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stage_config(
            tmp_path,
            monkeypatch,
            '[teatree]\n[overlays.t3-teatree]\nautonomy = "full"\n',
        )
        scanner = _pr_sweep_scanner_for(_backend(name="t3-teatree"), slack_user_id="")
        assert scanner is not None
        assert scanner.solo_overlay is True

    def test_notify_overlay_keeps_clear_path_no_solo_bypass(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``notify`` collapses the merge gates but must NOT skip the CLEAR path.

        The collaborative ``notify`` tier merges the user's MR only after a
        colleague approval (the CLEAR is issued by an independent reviewer);
        ``solo_overlay = True`` would let the sweep merge an un-CLEARed MR via
        a direct ``gh pr merge``, bypassing colleague approval.
        """
        _stage_config(
            tmp_path,
            monkeypatch,
            '[teatree]\n[overlays.t3-client]\nautonomy = "notify"\n',
        )
        scanner = _pr_sweep_scanner_for(_backend(name="t3-client"), slack_user_id="")
        assert scanner is not None
        assert scanner.solo_overlay is False

    def test_babysit_overlay_keeps_clear_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stage_config(
            tmp_path,
            monkeypatch,
            '[teatree]\n[overlays.careful]\nautonomy = "babysit"\n',
        )
        scanner = _pr_sweep_scanner_for(_backend(name="careful"), slack_user_id="")
        assert scanner is not None
        assert scanner.solo_overlay is False

    def test_full_overlay_arms_auto_review_dispatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``full`` autonomy collapses ``require_human_approval_to_merge`` → arms #68."""
        _stage_config(
            tmp_path,
            monkeypatch,
            '[teatree]\n[overlays.t3-teatree]\nautonomy = "full"\n',
        )
        scanner = _pr_sweep_scanner_for(_backend(name="t3-teatree"), slack_user_id="")
        assert scanner is not None
        assert scanner.solo_overlay is True
        assert scanner.auto_review_dispatch is True
        assert scanner.review_dispatcher is not None

    def test_full_overlay_with_pinned_human_approval_disarms_auto_review_dispatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit ``require_human_approval_to_merge = true`` survives the collapse.

        ``full`` autonomy would collapse the merge gate to ``false``, but a
        hard-pinned per-overlay value wins (``_global_pinned_fields`` /
        ``_apply_autonomy``). The single-author bypass still arms
        (``solo_overlay``) yet the cold-review auto-dispatch must NOT — the
        human stays in the merge loop, so the agent must not auto-dispatch its
        own review. This is the case that catches a future collapse-precedence
        regression silently arming a human-approval overlay.
        """
        _stage_config(
            tmp_path,
            monkeypatch,
            '[teatree]\n[overlays.t3-teatree]\nautonomy = "full"\nrequire_human_approval_to_merge = true\n',
        )
        scanner = _pr_sweep_scanner_for(_backend(name="t3-teatree"), slack_user_id="")
        assert scanner is not None
        assert scanner.solo_overlay is True
        assert scanner.auto_review_dispatch is False
        assert scanner.review_dispatcher is None

    def test_self_identities_threaded_from_backend(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#2210: the operator's identity set is wired so the review-arm is own-PR scoped.

        ``backend.identities`` (the multi-alias self set) must reach the
        scanner — without it ``pr_authored_by_self`` has no identity set to
        match against and (fail-closed) would arm nothing, defeating #68.
        """
        _stage_config(
            tmp_path,
            monkeypatch,
            '[teatree]\n[overlays.t3-teatree]\nautonomy = "full"\n',
        )
        backend = _backend(name="t3-teatree", identities=("souliane", "souliane-alt"))
        scanner = _pr_sweep_scanner_for(backend, slack_user_id="")
        assert scanner is not None
        assert scanner.self_identities == ("souliane", "souliane-alt")
