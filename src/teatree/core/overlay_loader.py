"""Discover and cache overlay instances from entry points and the DB overlays registry.

Unifies both discovery mechanisms so that ``get_overlay()`` works regardless
of whether the overlay was registered via ``pip install`` (entry point) or the
DB-home ``overlays`` registry (injected into ``load_config().raw``).
"""

import importlib
import logging
import os
from collections.abc import Callable
from functools import lru_cache
from typing import TYPE_CHECKING, ClassVar, TypeVar
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured

from teatree.core.overlay_url import get_overlay_for_url
from teatree.core.overlays.overlay_code_defaults_provider import build_and_register as _register_overlay_code_defaults
from teatree.utils.url_slug import slug_from_issue_or_pr_url

if TYPE_CHECKING:
    from teatree.core.models import Ticket, Worktree
    from teatree.core.overlay import OverlayBase

_FieldT = TypeVar("_FieldT", list[str], dict[str, list[str]])

logger = logging.getLogger(__name__)

__all__ = [
    "OverlayConfigResolver",
    "frontend_repos_for_overlay",
    "get_all_overlays",
    "get_overlay",
    "get_overlay_for_repo",
    "get_overlay_for_ticket",
    "get_overlay_for_url",
    "get_overlay_for_worktree",
    "infer_overlay_for_url",
    "reset_overlay_cache",
    "resolve_overlay_name",
    "ticket_repo_is_overlay_own",
]


def get_overlay(name: str | None = None) -> "OverlayBase":
    overlays = _discover_overlays()
    if not overlays:
        msg = (
            "No teatree overlays found. Install a package that provides a"
            " 'teatree.overlays' entry point, or add one to the DB overlays registry"
            " with `t3 <overlay> config_setting set overlays <value>`."
        )
        raise ImproperlyConfigured(msg)

    if name is None:
        name = os.environ.get("T3_OVERLAY_NAME") or None

    if name is not None:
        try:
            return overlays[name]
        except KeyError:
            msg = f"Overlay {name!r} not found. Available: {', '.join(sorted(overlays))}"
            raise ImproperlyConfigured(msg) from None

    if len(overlays) == 1:
        return next(iter(overlays.values()))

    msg = f"Multiple overlays found ({', '.join(sorted(overlays))}). Pass an explicit name to get_overlay()."
    raise ImproperlyConfigured(msg)


def _canonical_overlay_name(name: str) -> str | None:
    """Fold a stored overlay name onto its canonical registered name, or ``None``.

    A blank name is the ambient single-overlay default (``None`` → the
    caller lets ``get_overlay(None)`` resolve it). A non-blank name is
    canonicalized through the same :func:`resolve_overlay_name` rule the
    config loader uses, so a stored legacy alias (``teatree`` → ``t3-teatree``)
    resolves instead of raising ``Overlay not found``. An unresolvable name
    is returned unchanged so ``get_overlay`` raises a precise ``not found``.
    """
    if not name:
        return None
    return resolve_overlay_name(name) or name


def get_overlay_for_ticket(ticket: "Ticket") -> "OverlayBase":
    """Resolve the overlay a ticket belongs to.

    The queued FSM workers run a ticket's runners in a process where every
    installed overlay is registered, so a bare :func:`get_overlay` raises
    ``Multiple overlays found`` (souliane/teatree#1814). The ticket records
    its own overlay, so resolution is unambiguous regardless of how many
    overlays are installed; an empty value falls through to the ambient
    single-overlay default. A stored legacy alias is folded onto its
    canonical registered name (souliane/teatree#1975).
    """
    return get_overlay(_canonical_overlay_name(ticket.overlay))


def get_overlay_for_worktree(worktree: "Worktree") -> "OverlayBase":
    """Resolve the overlay a worktree belongs to.

    Like :func:`get_overlay_for_ticket` but keyed on the worktree's own
    ``overlay`` field, falling back to the owning ticket for rows created
    before the field was populated (souliane/teatree#1814).
    """
    if worktree.overlay:
        return get_overlay(_canonical_overlay_name(worktree.overlay))
    return get_overlay_for_ticket(worktree.ticket)


def get_overlay_for_repo(repo: str = ".") -> "OverlayBase | None":
    """Return the overlay whose workspace repos own the git repo at ``repo``.

    Resolves the ``origin`` remote slug (``owner/name``) of the repo at
    ``repo`` and matches it against each registered overlay's
    ``get_workspace_repos()`` — the same repo-ownership relation
    :func:`infer_overlay_for_url` uses for a URL. This lets a caller in an
    ambiguous multi-overlay environment pick the overlay that actually owns
    the current repository instead of crashing on ambiguity.

    Returns ``None`` when the slug is empty (no ``origin``) or matches zero
    or more than one overlay, so the caller can fall back deterministically
    rather than guess wrong. An overlay whose ``get_workspace_repos()``
    raises is skipped so one broken overlay can't poison resolution.
    """
    from teatree.utils.git import remote_slug  # noqa: PLC0415

    slug = remote_slug(repo=repo)
    if not slug:
        return None

    matches: list[OverlayBase] = []
    for name, overlay in get_all_overlays().items():
        getter = getattr(overlay, "get_workspace_repos", None)
        if not callable(getter):
            continue
        try:
            repo_slugs = getter()
        except Exception:
            logger.warning("Overlay %r get_workspace_repos() failed during repo resolution", name, exc_info=True)
            continue
        for repo_slug in repo_slugs or []:
            if isinstance(repo_slug, str) and repo_slug and repo_slug in slug:
                matches.append(overlay)
                break

    if len(matches) == 1:
        return matches[0]
    return None


def get_all_overlays() -> "dict[str, OverlayBase]":
    return dict(_discover_overlays())


class OverlayConfigResolver:
    """Resolve a per-overlay config field, path-only-symmetrically.

    An instantiable overlay (entry-point package, or a TOML table carrying a
    ``class``) answers from its ``OverlayConfig`` attribute. A **path-only**
    TOML overlay — registered with a ``path`` but no Python ``class`` — cannot
    be instantiated as :class:`OverlayBase` in the teatree process (it is
    reached through the CLI subprocess bridge), so ``get_overlay`` raises
    ``Overlay not found`` for it even though it is a known, registered overlay.
    For that case the field is read straight from the ``[overlays.<name>]``
    TOML table — the same config surface :meth:`all_names` already trusts for
    path-only entries.

    Every field a gate resolves per overlay (frontend repos for the local-E2E
    gate, ``owned_repos`` for the fail-CLOSED scope gate) is registered in
    :attr:`RESOLVABLE_FIELDS` so a path-only overlay can never carry a field
    one of those gates reads through ``get_all_overlays()`` alone (where it is
    invisible). The symmetry fitness test pins that registry.
    """

    @classmethod
    def all_names(cls) -> list[str]:
        """Return all overlay names, including path-only TOML entries.

        Unlike ``get_all_overlays()``, this includes TOML entries that declare
        a ``path`` but no ``class`` — they can't be instantiated as
        OverlayBase but should appear when listing overlays known to teatree
        (for ticket filtering, etc.).
        """
        from teatree.config import load_config  # noqa: PLC0415

        names = set(_discover_overlays())
        config = load_config()
        for name, cfg in config.raw.get("overlays", {}).items():
            if cfg.get("path"):
                names.add(name)
        return sorted(names)

    @classmethod
    def _resolve(cls, name: str | None, attr: str, empty: _FieldT) -> _FieldT:
        """Return *attr* for overlay *name*, from its config or its raw TOML table.

        A blank *name* is the ambient single-overlay default and routes through
        ``get_overlay(None)``. A name that resolves to no registered overlay
        (a removed overlay, a typo, a synthetic tag) raises
        ``ImproperlyConfigured`` so a safety-gate caller keeps its fail-closed
        posture for a genuinely unknown overlay instead of silently inferring
        the empty value.
        """
        from teatree.config import load_config  # noqa: PLC0415

        resolved = _canonical_overlay_name(name) if name else None
        try:
            overlay = get_overlay(resolved)
        except ImproperlyConfigured:
            canonical = resolve_overlay_name(name) if name else None
            if canonical is None:
                raise
            table = load_config().raw.get("overlays", {}).get(canonical, {})
            return table.get(attr) or empty
        config = overlay.config
        if not hasattr(config, attr):
            msg = f"overlay {name!r} config has no {attr}"
            raise ImproperlyConfigured(msg)
        return getattr(config, attr) or empty

    @classmethod
    def frontend_repos(cls, name: str | None) -> list[str]:
        """The overlay's configured frontend repos, resolvable for path-only overlays."""
        return [str(r) for r in cls._resolve(name, "frontend_repos", [])]

    @classmethod
    def owned_repos(cls, name: str | None) -> dict[str, list[str]]:
        """The overlay's forge-host-keyed ``owned_repos`` (SCOPE axis), path-only-symmetric.

        The SCOPE-axis twin of :meth:`frontend_repos`. A path-only overlay's
        ``owned_repos`` is otherwise invisible to the fail-CLOSED owned-repo
        gate, which iterates only instantiable overlays. The dict is returned
        verbatim (the same shape ``apply_toml_overrides`` would ``setattr``),
        defaulting to ``{}`` when undeclared and raising ``ImproperlyConfigured``
        for a genuinely unknown overlay so the gate's fail-closed posture holds.
        """
        return dict(cls._resolve(name, "owned_repos", {}))

    @classmethod
    def path_only_owned_scopes(cls) -> list[dict[str, list[str]]]:
        """The opted-in ``owned_repos`` of every path-only overlay.

        A path-only overlay (``path``, no ``class``) opts into the scope gate
        the same way an instantiable one does — ``require_owned_repo_approval``
        True with a non-empty ``owned_repos`` — but lives only in the raw TOML
        table. This yields each such scope so the push and merge classifiers
        can honor a repo a path-only overlay owns. Overlays that did not opt in,
        carry an empty ``owned_repos``, or are instantiable (already covered by
        ``get_all_overlays()``) are excluded.
        """
        from teatree.config import load_config  # noqa: PLC0415

        instantiable = set(_discover_overlays())
        overlays_cfg = load_config().raw.get("overlays", {})
        scopes: list[dict[str, list[str]]] = []
        for name, cfg in overlays_cfg.items():
            if name in instantiable or not cfg.get("path"):
                continue
            owned = cfg.get("owned_repos") or {}
            if cfg.get("require_owned_repo_approval") and owned:
                scopes.append(dict(owned))
        return scopes

    @classmethod
    def path_only_workspace_repos(cls) -> "list[tuple[str, list[str]]]":
        """``(name, repo_slugs)`` for every path-only overlay (URL-ownership input).

        A path-only overlay (``path``, no ``class``) is skipped by
        ``get_all_overlays()``, so its repos are invisible to
        :func:`infer_overlay_for_url` — which means a URL owned by a path-only
        overlay (e.g. an external-forge overlay's ``acme-eng/acme-product`` MRs)
        resolves to ``""`` from the teatree-core process and the review-request
        routing falls back to the wrong overlay (#2231). This yields the repo
        slugs a path-only overlay declares in its ``[overlays.<name>]`` TOML
        table — ``workspace_repos`` plus the flat values of ``owned_repos`` —
        so inference can attribute a URL to it. Instantiable overlays (already
        covered by ``get_all_overlays()``) are excluded to avoid double-counting.
        """
        from teatree.config import load_config  # noqa: PLC0415

        instantiable = set(_discover_overlays())
        overlays_cfg = load_config().raw.get("overlays", {})
        result: list[tuple[str, list[str]]] = []
        for name, cfg in overlays_cfg.items():
            if name in instantiable or not cfg.get("path"):
                continue
            slugs = [s for s in (cfg.get("workspace_repos") or []) if isinstance(s, str)]
            for host_slugs in (cfg.get("owned_repos") or {}).values():
                slugs.extend(s for s in (host_slugs or []) if isinstance(s, str) and s != "*")
            if slugs:
                result.append((name, slugs))
        return result

    RESOLVABLE_FIELDS: ClassVar[dict[str, Callable[[str | None], object]]] = {}


OverlayConfigResolver.RESOLVABLE_FIELDS = {
    "frontend_repos": OverlayConfigResolver.frontend_repos,
    "owned_repos": OverlayConfigResolver.owned_repos,
}


def frontend_repos_for_overlay(name: str | None) -> list[str]:
    """The overlay's configured frontend repos, resolvable for path-only overlays.

    Thin module-level wrapper over :meth:`OverlayConfigResolver.frontend_repos`,
    kept for the existing local-E2E gate caller (``core.gates.dod_gate``).
    """
    return OverlayConfigResolver.frontend_repos(name)


def resolve_overlay_name(name: str) -> str | None:
    """Return the canonical registered overlay name for *name*, or ``None``.

    The single source of truth for "is this overlay name dispatchable, and
    under what canonical name". A name that is already a registered overlay
    returns unchanged; a legacy short alias folds onto its registered
    entry-point via the same ``_match_canonical_ep`` rule the config loader
    uses (``teatree`` → ``t3-teatree``). A name that matches nothing — a
    removed overlay, a synthetic scanner tag, a typo — returns ``None`` so
    callers can fail it permanently instead of crashing on every retry
    (souliane/teatree#1959 poison-pill).

    Callers asking only "is this dispatchable?" test ``resolve_overlay_name(x)
    is not None``; an empty/blank ``name`` is the ambient single-overlay default
    and is the caller's responsibility to special-case (it returns ``None``).
    """
    from teatree.config import _match_canonical_ep  # noqa: PLC0415

    if not name:
        return None
    known = set(OverlayConfigResolver.all_names())
    if name in known:
        return name
    return _match_canonical_ep(name, known)


def _url_to_slug(url: str) -> str:
    """Normalize ``url`` to an ``owner/name`` slug for ownership matching.

    ``infer_overlay_for_url`` is called with two input shapes. A full
    issue/PR web URL (the ``workspace ticket`` / ``Ticket._infer_overlay``
    path) is parsed via :func:`slug_from_issue_or_pr_url`, which strips the
    ``/issues|pull|merge_requests/<n>`` suffix and handles GitLab subgroups.
    A bare ``owner/repo`` slug (the merge-authorization path, where
    ``MergeClear.slug`` is already ``owner/repo``) is returned as-is.

    Returns the parsed slug, or ``""`` when neither shape yields a
    multi-segment ``owner/name`` path.
    """
    issue_slug = slug_from_issue_or_pr_url(urlparse(url).path)
    if issue_slug:
        return issue_slug
    # Bare-slug fallback: a path with no recognised forge issue/PR suffix.
    path = urlparse(url).path if "://" in url else url
    candidate = path.strip("/")
    return candidate if candidate.count("/") >= 1 else ""


def _full_slug_owns(repo_slug: str, url_slug: str) -> bool:
    """True when the proper ``owner/name`` ``repo_slug`` owns ``url_slug``.

    Segment/boundary-aware, not a raw substring: ``repo_slug`` must carry at
    least one ``/`` (a real ``owner/name`` slug) and its ``/``-delimited
    segments must align as a suffix of ``url_slug``. A bare relative token
    (``t3-company``, as ``_discover_workspace_repos()`` emits) is rejected
    here — it can never own a URL by its directory name, closing the #1120
    misclassification where ``"t3-company" in <full URL>`` was True.

    Examples (``repo_slug`` owns ``url_slug``?):

    - ``company-fork-org/t3-company`` owns ``company-fork-org/t3-company`` (exact).
    - ``subgroup/repo`` owns ``group/subgroup/repo`` (segment suffix).
    - ``t3-company`` does NOT own ``company-fork-org/t3-company`` (bare token).
    - ``acme/widget`` does NOT own ``acme/widget-extra`` (segment differs).
    """
    if "/" not in repo_slug:
        return False
    if repo_slug == url_slug:
        return True
    return url_slug.split("/")[-repo_slug.count("/") - 1 :] == repo_slug.split("/")


def _bare_name_owns(repo_token: str, url_slug: str) -> bool:
    """True when a bare repo-name ``repo_token`` matches ``url_slug``'s name segment.

    The weak tiebreaker tier: a relative directory token (no ``/``) is
    matched only against the trailing repo-name segment of ``url_slug``, on a
    full-segment boundary. This preserves overlays that legitimately own a
    repo but only expose its bare relative path (the bundled ``t3-teatree``
    overlay, whose ``get_workspace_repos()`` returns ``["teatree"]``), without
    the raw-substring collisions of the pre-#1120 matcher.
    """
    return "/" not in repo_token and url_slug.rsplit("/", 1)[-1] == repo_token


def infer_overlay_for_url(url: str) -> str:
    """Return the overlay whose workspace repos own ``url``, or ``""``.

    The single source of truth for URL→overlay inference, consumed by
    ``Ticket._infer_overlay``, ``resolve_overlay_name_for_url`` (workspace
    ticket), merge authorization, review-request routing, the eval corpus,
    and loop persistence. ``url`` may be a full issue/PR web URL or a bare
    ``owner/repo`` slug — both normalize to an ``owner/name`` via
    :func:`_url_to_slug`.

    Routes through ``overlay.get_workspace_repos()`` rather than the raw
    ``config.workspace_repos`` attribute: overlays that compute their repo
    list dynamically leave the attribute empty. A registered entry that is
    not a full overlay, or whose hook raises, is skipped so one broken
    overlay can't poison inference for the others.

    Matching is two-tier and ambiguity-safe (souliane/teatree#1120):

    1. Full ``owner/name`` slug ownership (:func:`_full_slug_owns`) is
        authoritative. A proper slug match always wins over a bare directory
        token, so a sibling overlay clone's relative path (``t3-company``)
        never out-votes the overlay that actually declares
        ``company-fork-org/t3-company``.
    2. Bare repo-name fallback (:func:`_bare_name_owns`) fires only when
        NO overlay claims the URL by a full slug — preserving overlays that
        expose only a bare relative path for a repo they own.

    Within each tier, more than one matching overlay returns ``""`` rather
    than an arbitrary first dict hit, so callers fall back to the explicit
    ``T3_OVERLAY_NAME`` path (or a default) instead of a wrong-but-nonempty
    attribution.
    """
    if not url:
        return ""
    url_slug = _url_to_slug(url)
    if not url_slug:
        return ""

    full_matches: list[str] = []
    bare_matches: list[str] = []
    for name, slugs in _overlay_repo_slugs_for_inference():
        if any(_full_slug_owns(s, url_slug) for s in slugs):
            full_matches.append(name)
        elif any(_bare_name_owns(s, url_slug) for s in slugs):
            bare_matches.append(name)

    if full_matches:
        return full_matches[0] if len(full_matches) == 1 else ""
    return bare_matches[0] if len(bare_matches) == 1 else ""


def ticket_repo_is_overlay_own(ticket: "Ticket") -> bool:
    """True iff *ticket*'s issue lives in its overlay's OWN primary repo(s).

    Distinguishes an overlay's canonical repos (``get_repos()``) from its
    broader ``get_workspace_repos()`` routing set. An overlay may declare a
    sibling project's repo(s) in its own ``workspace_repos`` purely so
    ``infer_overlay_for_url`` / the lifecycle machinery has *some* overlay to
    dispatch tickets through, when that sibling project's own repo-ownership
    config does not enumerate its own meta/tooling repo. A ticket reached
    only through that routing convenience is not "this overlay's own
    codebase" for a gate that means to scope to it (e.g. the review_skill
    evidence gate, #1539 / #2895).

    Uses the same two-tier slug matching :func:`infer_overlay_for_url`
    already uses (:func:`_full_slug_owns` / :func:`_bare_name_owns`), just
    scoped to ``get_repos()`` instead of ``get_workspace_repos()``.

    Fails OPEN (returns ``True``, i.e. "treat as the overlay's own repo") on
    every undeterminable case — no ``issue_url``, an unparsable URL, an
    unresolvable overlay, or a ``get_repos()`` that raises or returns nothing
    — so a caller that narrows behaviour based on this function never widens
    the narrowing beyond the specific routed-through case it targets.
    """
    if not ticket.issue_url:
        return True
    url_slug = _url_to_slug(ticket.issue_url)
    if not url_slug:
        return True

    try:
        overlay = get_overlay_for_ticket(ticket)
    except ImproperlyConfigured:
        return True

    try:
        repos = [r for r in overlay.get_repos() or [] if isinstance(r, str)]
    except Exception:
        logger.warning(
            "Overlay %r get_repos() failed during ticket repo-ownership check",
            ticket.overlay,
            exc_info=True,
        )
        return True

    return (
        not repos
        or any(_full_slug_owns(repo, url_slug) for repo in repos)
        or any(_bare_name_owns(repo, url_slug) for repo in repos)
    )


def _overlay_repo_slugs_for_inference() -> "list[tuple[str, list[str]]]":
    """``(name, repo_slugs)`` for every overlay that can own a URL.

    Instantiable overlays answer from ``get_workspace_repos()``; path-only TOML
    overlays — invisible to ``get_all_overlays()`` — contribute the slugs they
    declare in their ``[overlays.<name>]`` table (#2231). A broken
    ``get_workspace_repos()`` is skipped (warned) so one overlay can't poison
    inference for the rest.
    """
    result: list[tuple[str, list[str]]] = []
    for name, overlay in get_all_overlays().items():
        getter = getattr(overlay, "get_workspace_repos", None)
        if not callable(getter):
            continue
        try:
            repo_slugs = getter()
        except Exception:
            logger.warning("Overlay %r get_workspace_repos() failed during inference", name, exc_info=True)
            continue
        result.append((name, [s for s in repo_slugs or [] if isinstance(s, str)]))
    result.extend(OverlayConfigResolver.path_only_workspace_repos())
    return result


@lru_cache(maxsize=1)
def _discover_overlays() -> "dict[str, OverlayBase]":
    import importlib.metadata  # noqa: PLC0415

    from teatree.core.overlay import OverlayBase  # noqa: PLC0415

    result: dict[str, OverlayBase] = {}

    # 1. Entry-point overlays (pip-installed packages)
    eps = importlib.metadata.entry_points(group="teatree.overlays")
    for ep in eps:
        cls = ep.load()
        if not issubclass(cls, OverlayBase):
            msg = f"Entry point {ep.name!r} ({ep.value}) does not subclass OverlayBase"
            raise ImproperlyConfigured(msg)
        overlay = cls()
        # Apply [overlays.<name>] overrides so entry-point overlays are
        # configurable from the DB overlays registry on the same footing as
        # registry-only overlays. Without this, OverlayConfig subclasses
        # would have to opt in by passing overlay_name to super().__init__.
        overlay.config.apply_toml_overrides(ep.name)
        result[ep.name] = overlay

    # 2. Registry-configured overlays (not already found via entry points)
    result.update(_discover_toml_overlays(OverlayBase, set(result)))

    return result


def _discover_toml_overlays(
    base_class: type["OverlayBase"],
    already_found: set[str],
) -> "dict[str, OverlayBase]":
    """Discover overlays from the DB overlays registry that aren't already entry-point-registered."""
    from teatree.config import load_config  # noqa: PLC0415

    result: dict[str, OverlayBase] = {}
    config = load_config()
    overlays_cfg = config.raw.get("overlays", {})

    for name, overlay_cfg in overlays_cfg.items():
        if name in already_found:
            continue

        class_path = overlay_cfg.get("class", "")
        if not class_path or ":" not in class_path:
            # No class path — this is a project-directory-only overlay without
            # a Python class.  These work through the CLI subprocess bridge
            # (OverlayAppBuilder) but can't be instantiated as OverlayBase.
            continue

        try:
            module_path, class_name = class_path.rsplit(":", 1)
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            if not issubclass(cls, base_class):
                logger.warning("TOML overlay %r class %s does not subclass OverlayBase", name, class_path)
                continue
            result[name] = cls()
        except (ImportError, AttributeError) as exc:
            logger.warning("TOML overlay %r failed to load class %s: %s", name, class_path, exc)

    return result


def reset_overlay_cache() -> None:
    """Fully reset overlay discovery state so the next call rebuilds from scratch.

    Two layers must reset together. First, ``_discover_overlays.cache_clear()``
    drops the cached ``dict`` of overlay instances so the next ``get_overlay()``
    rediscovers entry points and re-instantiates overlays. Second, the bundled
    overlay module ``teatree.contrib.t3_teatree.overlay`` is dropped from
    ``sys.modules`` so its class body re-evaluates on the next import.
    ``TeatreeOverlay.config`` is a *class-level* :class:`OverlayConfig`
    singleton built at class-definition time against the live
    ``teatree.config.load_config()``. If the module stays cached in
    ``sys.modules`` the class attribute survives test teardown — and any test
    that ``patch("teatree.config.load_config")`` then instantiates
    ``TeatreeOverlay()`` silently sees the stale pre-patch config. Popping the
    module forces the class body to re-evaluate under the patched ``load_config``.

    Production code never calls this function — it's a test-isolation
    helper. The ``sys.modules`` pop is therefore safe: at runtime the
    module imports once and stays; under pytest it gets a clean rebuild
    between tests, which is exactly what test isolation requires.

    Keeping the full reset behind this single entry point means
    conftests don't need to know about class-cache vs lru_cache vs
    module-level evaluation — they just call ``reset_overlay_cache()``
    and get a clean slate. See souliane/teatree#1108 for the test-
    pollution incident this design closes (originally surfaced by
    ``slack_bridge_e2e``, then independently by ``tests/teatree_core``).
    """
    import sys  # noqa: PLC0415

    _discover_overlays.cache_clear()
    sys.modules.pop("teatree.contrib.t3_teatree.overlay", None)


# Register the overlay-code-default provider (#36) at import time, injecting this
# module's ``get_overlay`` — mirrors ``teatree.cli`` registering the command-catalogue
# provider. The seam needs ``get_overlay`` to do anything, so registering here makes
# the provider live exactly when it can be useful; before this module is imported the
# seam fails safe to the dataclass default.
_register_overlay_code_defaults(get_overlay)
