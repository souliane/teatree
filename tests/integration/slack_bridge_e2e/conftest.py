"""Shared infra for the Slack messaging-bridge integration fortress (#1057).

This package exercises the inbound bridge (Slack DM →
``PendingChatInjection`` → ``UserPromptSubmit`` drain → agent
``additionalContext``) AND the outbound bridge (``notify_user`` →
backend ``post_message`` → Slack ``chat.postMessage``) end-to-end with a
fake Slack transport bolted onto the ``httpx`` boundary so the only
thing mocked is the network. Every other layer — the real
``SlackBotBackend``, the real ``SlackDmInboundScanner``, real
``PendingChatInjection`` rows in the Django DB, the real hook router,
the real ``notify_user``, the real ``loops_tick`` management command —
runs unmodified. The split into ``test_inbound`` / ``test_outbound`` /
``test_routing`` / ``test_backend_error_paths`` (#1066) mirrors the
``tests/teatree_core/management_commands/`` package convention.

The marker ``@pytest.mark.integration`` puts each test class on the
integration-tests selector so CI runs the fortress on every PR.

Anti-vacuous evidence: each test names the production line whose
removal would turn it RED. The PR body records that line-to-test
mapping plus the actual RED runs that confirm the guard.

Justified scaffolding (#1066 nit 2): the per-overlay routing tests
deliberately patch ``backend_factory._messaging_from_toml`` /
``teatree.config.load_config`` / ``backend_factory.get_overlay``
rather than only ``httpx``. Exercising per-overlay token isolation
needs multiple synthetic overlay configs that are not expressible via
a single real config store, so the config-resolution seam is
patched to inject them. ``httpx`` (the network) and
``teatree.utils.secrets.read_pass`` (the password store) remain the
only true externals and stay real. This deviation from the epic's
literal "mock only httpx" guidance is intentional — do not "fix" it
back to a single real TOML; that would risk coverage drift on the
per-overlay routing branches.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from teatree.backends.slack import http as slack_http
from teatree.config.enums import Autonomy as _Autonomy
from teatree.config.enums import OnBehalfPostMode as _OnBehalfPostMode
from teatree.types import DEFAULT_MR_TITLE_REGEX as _DEFAULT_MR_TITLE_REGEX
from teatree.types import SlackVoiceClassifierMode as _VoiceClassifierMode
from teatree.types import SpeakConfig as _SpeakConfig

# ── Fake Slack transport ──────────────────────────────────────────────


@dataclass
class _Call:
    method: str
    url: str
    token: str
    payload: dict[str, Any]


@dataclass
class FakeSlackTransport:
    """Recording fake Slack transport at the ``httpx`` boundary.

    Routes every ``slack.com/api/<method>`` request to a scripted
    response keyed by the URL's last segment. POST bodies arrive as
    ``json=``; GET bodies arrive as ``params=``. Each call is appended
    to :attr:`calls` so tests can assert who-called-what-with.
    Per-method handlers can be overridden by setting ``handlers[name]``.
    """

    calls: list[_Call] = field(default_factory=list)
    handlers: dict[str, Callable[[dict[str, Any], dict[str, str]], dict[str, Any]]] = field(default_factory=dict)
    default_responses: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            "auth.test": {"ok": True, "user_id": "B_BOT"},
            "conversations.open": {"ok": True, "channel": {"id": "D-USER"}},
            "conversations.history": {"ok": True, "messages": []},
            "conversations.replies": {"ok": True, "messages": []},
            "chat.postMessage": {"ok": True, "ts": "1700000000.000100", "channel": "D-USER"},
            "chat.getPermalink": {
                "ok": True,
                "permalink": "https://example.slack.com/archives/D-USER/p1700000000000100",
            },
            "reactions.add": {"ok": True},
            "reactions.get": {"ok": True, "message": {"reactions": []}},
            "conversations.info": {"ok": True, "channel": {"is_ext_shared": False}},
            "users.lookupByEmail": {"ok": False, "error": "users_not_found"},
            "users.list": {"ok": True, "members": []},
        }
    )

    def _method_name(self, url: str) -> str:
        return url.rsplit("/", maxsplit=1)[-1]

    def _respond(
        self,
        *,
        http_method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> httpx.Response:
        method = self._method_name(url)
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        self.calls.append(_Call(method=method, url=url, token=token, payload=dict(payload)))
        handler = self.handlers.get(method)
        if handler:
            body: dict[str, Any] = handler(payload, headers)
        else:
            body = self.default_responses.get(method, {"ok": True})
        request = httpx.Request(http_method, url)
        return httpx.Response(200, json=body, request=request)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._respond(
            http_method="POST",
            url=url,
            headers=dict(kwargs.get("headers", {})),
            payload=dict(kwargs.get("json", {})),
        )

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._respond(
            http_method="GET",
            url=url,
            headers=dict(kwargs.get("headers", {})),
            payload=dict(kwargs.get("params", {})),
        )

    def calls_to(self, method: str) -> list[_Call]:
        return [c for c in self.calls if c.method == method]


@pytest.fixture(autouse=True)
def _isolate_bundled_overlay_module() -> Iterator[None]:
    """Drop the ``teatree.contrib.t3_teatree.overlay`` module + cache after each test.

    The fortress tests reach :func:`teatree.core.notify.notify_user`
    (directly or via the management-command tick), whose ``maybe_linkify``
    path calls ``overlay_loader.get_overlay`` → ``_discover_overlays``.
    The first call imports ``teatree.contrib.t3_teatree.overlay``, whose
    ``TeatreeOverlay`` class evaluates
    ``OverlayConfig(overlay_name="t3-teatree")`` at class-definition time
    against the live ``teatree.config.load_config()``. The resulting
    ``TeatreeOverlay.config`` is a class-level singleton bound to that
    first-import config, so downstream tests that
    ``patch("teatree.config.load_config")`` and expect a fresh class build
    (notably ``test_reads_t3_teatree_table_not_bare_teatree`` for
    souliane/teatree#1108) silently see the stale class attribute and
    fail. ``overlay_loader.reset_overlay_cache()`` alone is insufficient —
    it clears the ``lru_cache`` but leaves the imported module (and its
    class attribute) in ``sys.modules``.

    Removing the module here lets the next first-import (typically inside
    the victim's ``with patch(...)`` context) re-evaluate the class body
    under the test's patched ``load_config``. Pairing with
    ``reset_overlay_cache`` keeps the ``_discover_overlays`` cache aligned
    so the rebuild actually fires.
    """
    import sys  # noqa: PLC0415

    from teatree.core import overlay_loader  # noqa: PLC0415

    try:
        yield
    finally:
        overlay_loader.reset_overlay_cache()
        sys.modules.pop("teatree.contrib.t3_teatree.overlay", None)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> FakeSlackTransport:
    """Install :class:`FakeSlackTransport` as the ``httpx`` boundary for slack_bot."""
    fake = FakeSlackTransport()
    monkeypatch.setattr(slack_http.httpx, "post", fake.post)
    monkeypatch.setattr(slack_http.httpx, "get", fake.get)
    return fake


# ── Test-side fake config ─────────────────────────────────────────────


@dataclass
class _FakeUserSettings:
    """Mirror of ``UserSettings`` for ``_resolved_identities()``."""

    user_identity_aliases: list[str] = field(default_factory=list)
    # #1395 The backend factory now resolves the voice/token classifier
    # mode from ``load_config().user.slack_voice_classifier_mode``;
    # mirror the new attribute so the test fixture stays a structural
    # subset of the real ``UserSettings``.
    slack_voice_classifier_mode: _VoiceClassifierMode = _VoiceClassifierMode.WARN
    # #1775 ``_resolved_identities()`` now routes through
    # ``get_effective_settings()``, whose autonomy collapse + per-overlay
    # speak merge read these fields. Mirror them with the real
    # ``UserSettings`` defaults so the fixture stays a structural subset.
    autonomy: _Autonomy = _Autonomy.BABYSIT
    on_behalf_post_mode: _OnBehalfPostMode = _OnBehalfPostMode.DRAFT_OR_ASK
    speak: _SpeakConfig = field(default_factory=_SpeakConfig)
    # #36 / #3115 ``get_effective_settings`` rebuilds settings via
    # ``dataclasses.replace(base, **layered)`` where ``layered`` carries the
    # overlay CODE-DEFAULT tier — every key in
    # ``PROMOTED_OVERLAY_CODE_DEFAULT_KEYS``. ``replace`` re-invokes
    # ``base.__class__(**changes)``, so each promoted key MUST be a field here or
    # the rebuild raises ``TypeError`` (surfaces only when the active overlay
    # resolves and populates the tier — a cwd-basename-dependent path, hence
    # invisible to CI's ``/app`` checkout; see ``test_fake_config_fidelity``).
    # Mirror the real ``UserSettings`` defaults so the fixture stays a structural
    # subset.
    review_skill: str = ""
    architectural_review_skill: str = "ac-reviewing-codebase"
    scanning_news_skill: str = "scanning-news"
    eval_local_skill: str = "eval"
    backlog_sweep_skill: str = "sweeping-tickets"
    dogfood_smoke_skill: str = "dogfood-smoke"
    mr_title_regex: str = _DEFAULT_MR_TITLE_REGEX


@dataclass
class _FakeConfig:
    """Mirror of ``TeaTreeConfig`` for the backend-factory TOML fallback."""

    raw: dict[str, Any] = field(default_factory=dict)
    user: _FakeUserSettings = field(default_factory=_FakeUserSettings)


# ── Helpers ───────────────────────────────────────────────────────────


def _own_loop(session_id: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Register ``session_id`` as the loop owner via the hook router's registry."""
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    import hooks.scripts.hook_router as router  # noqa: PLC0415

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router, "STATE_DIR", state)
    registry_dir = tmp_path / "loop_registry"
    registry_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(registry_dir))
    router._write_loop_registry(
        {
            router._OWNER_LOOP: {
                "session_id": session_id,
                "agent_id": "test",
                "pid": os.getpid(),
                "heartbeat_ts": int(time.time()),
            }
        }
    )
