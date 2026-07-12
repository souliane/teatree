"""Per-tick config resolvers extracted from :mod:`teatree.loop.tick` (#1015).

Three concerns the tick module composes but doesn't own. First, a
URL-prefix gate for the PR scanners — each scanner is registered per
``(overlay x code_host)``, so without a gate it would emit every MR/PR
the host returns and bleed cross-overlay rows into the wrong statusline.
Second, the web origin for a code-host backend — needed to build the
URL-prefix gate; resolved from the runtime class so a self-hosted GitLab
is honoured. Third, ``identity_alias_groups`` for the overlay — the
ticket-disposition scanner suppresses a reassign only when both ends fall
inside the same group, so multi-human overlays keep cross-human handoffs
visible.

Keeping these in their own module keeps ``tick.py`` focused on
orchestration (scan in parallel, dispatch, render) without lugging
config-resolution code along.
"""

import logging

from teatree.config import discover_overlays
from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend

logger = logging.getLogger(__name__)


def _allowed_url_prefixes_for_host(
    backend: OverlayBackends,
    code_host: CodeHostBackend,
) -> tuple[str, ...]:
    """Compute URL prefixes that bound a scanner to the overlay's repos (#1015, #1324).

    Each scanner is registered per ``(overlay x code_host)``. Without a gate,
    a scanner emits every MR/PR the host returns — including ones from a
    sibling overlay that shares the same PAT. The gate is a list of URL
    prefixes built from the overlay's ``workspace_repos`` and the host's
    web origin.

    Two slug shapes are supported (#1324):

    * ``owner/repo`` — emits an exact prefix ``https://host/owner/repo/``.
        This is the shape ``[overlays.<name>] workspace_repos`` opts in to in
        the DB overlays registry.
    * Bare ``repo`` — emits a wildcard pattern ``https://host/*/repo/`` that
        :meth:`MyPrsScanner._url_allowed` matches as "any owner segment, then
        this repo segment". Overlays whose ``get_repos()`` returns bare names
        (e.g. ``product``) still gate correctly across self-hosted namespaces
        (e.g. ``gitlab.com/some-namespace/product/``) without forcing every
        overlay to repeat its namespace.

    Returns an empty tuple when no overlay or repo list is configured so
    the scanner keeps its legacy "emit all" behaviour for ad-hoc and
    test invocations.
    """
    overlay = backend.overlay
    if overlay is None:
        return ()
    try:
        repos = overlay.get_workspace_repos()
    except Exception:  # noqa: BLE001 — never break a tick on a config read.
        logger.warning("Overlay %r get_workspace_repos() failed; URL gate disabled", backend.name)
        return ()
    if not repos:
        return ()
    web_origin = _web_origin_for_host(code_host)
    if not web_origin:
        return ()
    out: list[str] = []
    for slug in repos:
        if not isinstance(slug, str) or not slug:
            continue
        if "/" in slug:
            out.append(f"{web_origin}/{slug}/")
        else:
            # Bare slug → wildcard pattern matching any owner segment.
            out.append(f"{web_origin}/*/{slug}/")
    return tuple(out)


def _web_origin_for_host(code_host: CodeHostBackend) -> str:
    """Return the host's web origin (no trailing slash) for URL-prefix building.

    Resolved by inspecting the runtime class: GitHub is the canonical
    ``https://github.com``; GitLab strips ``/api/v4`` off the configured
    API base to recover the user-facing root (so a self-hosted GitLab is
    honoured). Returns ``""`` when the host shape is unrecognised so the
    URL gate degrades to ``empty prefixes → emit all``.
    """
    from teatree.backends.github import GitHubCodeHost  # noqa: PLC0415 — deferred: loaded at tick time, not import
    from teatree.backends.gitlab import GitLabCodeHost  # noqa: PLC0415 — deferred: loaded at tick time, not import

    if isinstance(code_host, GitHubCodeHost):
        return "https://github.com"
    if isinstance(code_host, GitLabCodeHost):
        api_base = getattr(code_host.client, "base_url", "")
        if not api_base:
            return ""
        return api_base.replace("/api/v4", "").rstrip("/")
    return ""


def _identity_alias_groups_for_overlay(
    overlay_name: str,
    backend: OverlayBackends | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Resolve ``identity_alias_groups`` for the active overlay (#1015).

    Each inner tuple is one human's set of aliases across forges. The
    ticket-disposition scanner suppresses a reassign only when both ends
    of the transition fall inside the SAME group — multi-human overlays
    keep cross-human handoffs visible.

    The live ``OverlayConfig.identity_aliases`` is the canonical source — any
    overlay class that reads from its own ``config`` sees the same value.
    TOML-defined overlays that never instantiate a Python overlay class fall
    back to ``[overlays.<name>] identity_aliases`` via ``discover_overlays``.
    Defaults to ``()`` so legacy single-group setups behave unchanged.
    """
    if backend is not None and backend.overlay is not None:
        groups = _normalize_alias_groups(getattr(backend.overlay.config, "identity_aliases", None))
        if groups:
            return groups
    try:
        for entry in discover_overlays():
            if entry.name != overlay_name:
                continue
            return _normalize_alias_groups(entry.overrides.get("identity_aliases"))
    except Exception:  # noqa: BLE001 — never break a tick on a config read.
        logger.warning("Failed to resolve identity_aliases for %r; defaulting to empty", overlay_name)
    return ()


def _normalize_alias_groups(raw: object) -> tuple[tuple[str, ...], ...]:
    """Coerce a TOML/list-of-lists value into the tuple-of-tuples scanner shape.

    Drops empty inner groups and non-string handles so a malformed config
    can't crash a tick — the scanner sees a clean, well-typed value or
    falls through to the empty default.
    """
    if not isinstance(raw, list):
        return ()
    groups: list[tuple[str, ...]] = []
    for group in raw:
        if not isinstance(group, list):
            continue
        handles = tuple(str(s) for s in group if isinstance(s, str) and s)
        if handles:
            groups.append(handles)
    return tuple(groups)
