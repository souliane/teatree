"""Pane-display presentation layer for Track-B maker panes (WI-5, #1838).

The PRESENTATION half of a maker pane. A pane's SDK session always runs
in-process (``teatree.teams.pane_spawn.build_pane_options`` resolves the session
id — the SINGLE source of truth); this module optionally renders that SAME
session in a visible terminal pane. It NEVER replaces the SDK run: any failure
(no tmux, no TTY, a non-zero ``tmux`` exit) degrades to the in-process path, so a
headless/CI run with no config is byte-identical to today.

The mechanism is tmux control mode (``tmux -CC``). teatree only ever issues plain
``tmux split-window`` / ``tmux select-pane`` / ``tmux kill-pane`` — the NATIVE
iTerm2 split rendering is purely a property of how the USER attached (``tmux
-CC``), so the same commands degrade to plain tmux panes anywhere else. Mirrors
Claude Code's own ``teammateMode`` (``tmux`` / ``in-process``, with ``auto``
probing) where natural.

A pure leaf: it imports only the standard library — never ``teatree.core`` /
``teatree.agents`` / the rest of ``teatree.teams`` — so it adds no live-path edge
(the #2320 inertness scan stays green) and is reached lazily from the one
sanctioned consumer (the pane-reaper scanner) for teardown.
"""

import logging
import shutil
import sys
from dataclasses import dataclass
from typing import Literal, Protocol

from teatree.utils.run import CompletedProcess, run_allowed_to_fail

logger = logging.getLogger(__name__)

#: The interactive child the visible pane hosts. ``--resume`` re-attaches the
#: SAME SDK session the in-process run owns; ``bypassPermissions`` mirrors the
#: headless/pane permission mode. It is plain interactive ``claude`` (NEVER
#: ``claude -p``) so the pane routes through the INTERACTIVE billing lane — a
#: visible interactive pane is the intended path, not a metered-headless backdoor.
_DEFAULT_PERMISSION_MODE = "bypassPermissions"


class _DisplayablePane(Protocol):
    """The pane surface the display layer reads — only the canonical claim slot.

    A :class:`~teatree.teams.panes.TeammatePane` satisfies this via its
    ``claim_slot`` property (the canonical ``team:<role>`` key), which doubles as
    the pane title so the visible title equals the claim key — one normalization
    seam, no second derivation.
    """

    @property
    def claim_slot(self) -> str: ...  # pragma: no cover - structural


@dataclass(frozen=True, slots=True)
class PaneHandle:
    """A spawned tmux pane hosting one maker session.

    ``pane_id`` is tmux's own ``#{pane_id}`` (e.g. ``%7``) captured from
    ``split-window -P -F`` — the verify-by-re-read receipt that the pane really
    exists. ``role`` is the canonical ``team:<role>`` slot (also the pane title);
    ``session_id`` is the resolved SDK session the visible child resumed.
    """

    pane_id: str
    role: str
    session_id: str


def detect_multiplexer() -> Literal["tmux", "none"]:
    """Probe the environment for a usable terminal multiplexer (#1838 WI-5).

    Returns ``"tmux"`` only when a pane can plausibly be split into the user's
    live session, else ``"none"`` (the in-process path stands). The probe, in
    order:

    *   ``$TMUX`` is set → we are already inside a tmux server; a split lands in
        the user's current session (and renders as a native iTerm2 split under
        ``tmux -CC``). This wins outright.
    *   No ``tmux`` binary on ``PATH`` → ``"none"`` (nothing to split with).
    *   stdout is not a TTY (headless / CI / piped) → ``"none"``: there is no
        interactive terminal to render a pane into. This is the load-bearing
        headless guard — a CI run is byte-identical to today.
    """
    import os  # noqa: PLC0415 — read at call time so a test can patch os.environ.

    if os.environ.get("TMUX"):
        return "tmux"
    if shutil.which("tmux") is None:
        return "none"
    if not sys.stdout.isatty():
        return "none"
    return "tmux"


def spawn_pane(pane: _DisplayablePane, options: object) -> PaneHandle | None:
    """Split a tmux pane hosting *pane*'s SDK session, or ``None`` on any failure.

    Issues ``tmux split-window -c <cwd> -P -F '#{pane_id}' -- claude --resume
    <session_id> --model <model> --permission-mode bypassPermissions`` then
    ``tmux select-pane -t <pane_id> -T 'team:<role>'``. The ``--resume`` id is the
    SDK session resolved by ``build_pane_options`` (``options.resume``) — the
    visible child re-attaches the SAME session, never a new one. Returns the
    :class:`PaneHandle` (with tmux's captured ``pane_id``) on success.

    Degrades to ``None`` — the caller keeps the in-process SDK run — on EVERY
    failure: no resume id (nothing to display), the ``tmux`` binary missing
    (``FileNotFoundError``), a non-zero ``split-window`` exit, or no captured
    pane id. The display layer never raises into the SDK path.
    """
    session_id = _option_str(options, "resume")
    if not session_id:
        return None
    role = pane.claim_slot
    result = _tmux(_build_split_argv(options, session_id=session_id))
    if result is None or result.returncode != 0:
        logger.debug("pane_display: tmux split-window unavailable/failed — staying in-process")
        return None
    pane_id = result.stdout.strip()
    if not pane_id:
        return None
    _title_pane(pane_id, title=role)
    return PaneHandle(pane_id=pane_id, role=role, session_id=session_id)


def teardown_pane(handle: PaneHandle) -> None:
    """Kill the tmux pane for *handle*, idempotently (#1838 WI-5).

    Issues ``tmux kill-pane -t <pane_id>``. A missing pane is NOT an error: tmux
    exits non-zero ("can't find pane") for an already-gone pane and we swallow
    it, so a double teardown (the reaper demote plus a startup reconcile) is a
    safe no-op. A missing ``tmux`` binary is likewise swallowed — the DB lease,
    not the pane, is authoritative; the kill is a best-effort downstream effect.
    """
    if _tmux(["tmux", "kill-pane", "-t", handle.pane_id]) is None:
        logger.debug("pane_display: tmux kill-pane unavailable — nothing to tear down")


def reconcile_orphan_panes(*, live_claim_slots: set[str]) -> list[str]:
    """Kill ``team:*``-titled tmux panes with no live claim. Returns killed ids.

    The startup reconcile: a crashed/compacted lead can leave a ``team:<role>``
    pane open with no matching live DB claim (the DB lease is authoritative, the
    pane is a downstream effect that can outlive it). This lists every pane with
    its title (``tmux list-panes -F '#{pane_id} #{pane_title}'``), keeps only
    those whose title is a ``team:*`` slot, and kills any whose slot is NOT in
    *live_claim_slots*. A plain shell / editor pane is never a candidate — only a
    ``team:``-titled pane is ours to reap.

    Fail-safe: no ``tmux`` binary or an empty pane list is a no-op (returns
    ``[]``). A non-team pane is never touched.
    """
    panes = _list_team_panes()
    killed: list[str] = []
    for pane_id, slot in panes:
        if slot in live_claim_slots:
            continue
        teardown_pane(PaneHandle(pane_id=pane_id, role=slot, session_id=""))
        killed.append(pane_id)
    return killed


def _build_split_argv(options: object, *, session_id: str) -> list[str]:
    """Build the ``tmux split-window`` argv hosting the resumed ``claude`` child."""
    argv = ["tmux", "split-window"]
    cwd = _option_str(options, "cwd")
    if cwd:
        argv += ["-c", cwd]
    # -P -F '#{pane_id}' prints the new pane's id so we can verify-by-re-read.
    argv += ["-P", "-F", "#{pane_id}", "--", "claude", "--resume", session_id]
    model = _option_str(options, "model")
    if model:
        argv += ["--model", model]
    permission_mode = _option_str(options, "permission_mode") or _DEFAULT_PERMISSION_MODE
    argv += ["--permission-mode", permission_mode]
    return argv


def _title_pane(pane_id: str, *, title: str) -> None:
    """Set the pane title to the ``team:<role>`` slot — best-effort, never raises."""
    if _tmux(["tmux", "select-pane", "-t", pane_id, "-T", title]) is None:
        logger.debug("pane_display: tmux select-pane unavailable — pane left untitled")


def _list_team_panes() -> list[tuple[str, str]]:
    """Return ``(pane_id, slot)`` for every ``team:*``-titled pane, or ``[]``."""
    result = _tmux(["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_title}"])
    if result is None or result.returncode != 0:
        return []
    panes: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        pane_id, _, title = line.strip().partition(" ")
        if pane_id and title.startswith("team:"):
            panes.append((pane_id, title))
    return panes


def _tmux(argv: list[str]) -> CompletedProcess[str] | None:
    """Run a ``tmux`` command via the trusted subprocess wrapper, ``None`` if absent.

    Routes through :func:`teatree.utils.run.run_allowed_to_fail` (the single
    sanctioned subprocess-egress chokepoint) with ``expected_codes=None`` so ANY
    tmux exit code is returned for the caller to inspect — a "can't find pane"
    non-zero is expected (idempotent teardown), never an error. A missing ``tmux``
    binary (``FileNotFoundError``) or any OS error degrades to ``None`` so the
    display layer is a no-op rather than a raise — the in-process SDK path stands.
    """
    try:
        return run_allowed_to_fail(argv, expected_codes=None)
    except (FileNotFoundError, OSError):
        return None


def _option_str(options: object, attr: str) -> str:
    """Read a string field off the ``ClaudeAgentOptions`` SDK object, ``""`` if absent."""
    value = getattr(options, attr, None)
    return value if isinstance(value, str) else ""


__all__ = [
    "PaneHandle",
    "detect_multiplexer",
    "reconcile_orphan_panes",
    "spawn_pane",
    "teardown_pane",
]
