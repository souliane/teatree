"""GitHub token permission preflight (#3405).

The deploy loop drives GitHub through ``gh`` with ``TEATREE_GH_TOKEN``. A token
that authenticates (``gh auth status`` green) but lacks a *write* permission the
loop needs — ``issues: write`` for labelling/closing, ``pull_requests: write``
for opening/merging, ``contents: write`` for pushing — does not fail at deploy
time. It fails much later, mid-run, with ``Resource not accessible by personal
access token`` on the first ``gh issue edit`` / ``gh pr merge`` — a silent,
hard-to-diagnose block on autonomy.

This probes the token's *effective* permissions up front so the failure is a
one-line bootstrap error instead of a late runtime surprise. The probe adapts to
the token *class*, because the two GitHub PAT kinds signal a missing permission
differently.

A **fine-grained** PAT gets a route-level ``403 Resource not accessible`` for a
permission it lacks. Each write permission is checked with a side-effect-free
mutation aimed at a resource number that never exists (issue/PR ``0``, a bogus
ref): a token that *has* the permission gets a harmless ``404``, a token that
*lacks* it gets the ``403`` GitHub returns before it ever loads the resource.
Nothing is created, edited, or deleted either way. A write probe that reaches
NEITHER verdict -- a transport/network fault, no ``403`` and no ``404`` -- is
*indeterminate*, never read as a grant: the deploy skips (a network blip must not
falsely certify a token) rather than passing preflight then failing mid-run.

A **classic** PAT does NOT get that route-level ``403`` — the write probe would
fail *open* for it. Instead GitHub reports a classic token's granted scopes in
the ``X-OAuth-Scopes`` response header, and the single ``repo`` scope is what
grants write to issues, pull requests, and contents. So a classic token is
judged by REQUIRING ``repo`` in that header, not by the per-route probe.

The metadata read carries the header (``gh api -i``): its presence means the
token is classic (fine-grained tokens omit it), which selects the scope check
over the per-permission probes.

``deploy/entrypoint.sh`` runs the same contract in pure bash during ``init``
(before the editable install exists, so it cannot call ``t3``); the
``teatree.cli.doctor`` mirror check and this module share the canonical
:data:`REQUIRED_PERMISSION_LABELS`, and a test pins the entrypoint's labels to
it so the two implementations cannot drift.
"""

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from teatree.utils.run import run_allowed_to_fail

# The permissions the deploy loop actually exercises, in report order. The
# labels are the human ``"<permission>: <level>"`` form GitHub's fine-grained
# token UI uses, so an operator can map a FAIL straight onto a token setting.
# Pinned to ``deploy/entrypoint.sh`` by ``tests/test_deploy_entrypoint_token_preflight``.
REQUIRED_PERMISSION_LABELS: tuple[str, ...] = (
    "metadata: read",
    "issues: write",
    "pull_requests: write",
    "contents: write",
)

# Substrings (lowercased) in a ``gh api`` failure that mean the *token* is
# denied — a permission/visibility signal, not a transient network fault. Used
# to tell "the token lacks this" apart from "the API was unreachable".
_DENIED_SIGNALS: tuple[str, ...] = (
    "not accessible",  # "Resource not accessible by personal access token / integration"
    "not found",  # a fine-grained token with no access sees the repo as 404
    "bad credentials",
    "requires authentication",
    "must be authenticated",
)

# The single write-permission signal: GitHub returns exactly this at the route
# level for a token missing the permission, before validating the target.
_FORBIDDEN_SIGNAL = "not accessible"

# Substrings that mean a write probe REACHED the route past the write-authorization
# gate: a token WITH the permission gets a 404/422 on the deliberately non-existent
# target (never a 403 ``not accessible``). Their presence -- or a zero exit code --
# means the permission is PRESENT. Their ABSENCE, with no ``not accessible`` denial,
# means the probe never reached a verdict (a transport/network fault) -> indeterminate.
_WRITE_REACHED_SIGNALS: tuple[str, ...] = (
    "not found",  # 404 on the non-existent issue/PR/ref (JSON body and gh's message)
    "(http ",  # gh appends the HTTP status on any reached response (e.g. "(HTTP 404)")
)

type _WriteVerdict = Literal["denied", "present", "indeterminate"]

# (permission label, gh-api argv template) for the three write probes. Each
# mutates a resource id that cannot exist, so a permitted token gets a 404 and a
# denied token gets a 403 — never a real write.
_WRITE_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("issues: write", ("--method", "PATCH", "repos/{slug}/issues/0", "-f", "state=open")),
    ("pull_requests: write", ("--method", "PATCH", "repos/{slug}/pulls/0", "-f", "state=open")),
    ("contents: write", ("--method", "PATCH", "repos/{slug}/git/refs/heads/teatree-preflight-nonexistent")),
)

# The write labels, derived from the probes so the two never drift. A classic PAT
# grants (or denies) all of them through the single ``repo`` scope, so a classic
# token missing ``repo`` reports every one of these as missing.
_WRITE_PERMISSION_LABELS: tuple[str, ...] = tuple(label for label, _ in _WRITE_PROBES)

# The classic-PAT OAuth scope that grants write to issues, pull requests, and
# repository contents. Matched as an exact scope token (never a substring, so
# ``repo:status`` is not read as ``repo``).
_CLASSIC_WRITE_SCOPE = "repo"

# The response header GitHub returns for a classic (OAuth) token, listing its
# granted scopes; a fine-grained token omits it. Its presence is the signal that
# the token is classic and must be judged by scope rather than per-route probe.
_OAUTH_SCOPES_HEADER = "x-oauth-scopes"

type GhRunner = Callable[[list[str]], tuple[int, str]]


@dataclass(frozen=True)
class GhTokenProbe:
    """Outcome of a token-permission probe.

    ``missing`` is the denied permission labels (empty == the token has every
    required permission). ``indeterminate_reason`` is set only when the probe
    could not run to a verdict (``gh`` absent, the metadata read unreachable, or a
    write probe that reached no 403/404) — the caller then skips rather than
    failing on a network fault. A genuine denial always takes precedence over an
    indeterminate write probe, so a real permission gap is never masked.
    """

    missing: tuple[str, ...]
    indeterminate_reason: str | None = None

    @property
    def ok(self) -> bool:
        return not self.missing and self.indeterminate_reason is None


def _default_run(args: list[str]) -> tuple[int, str]:
    """Run ``gh api <args>`` capturing combined stdout+stderr; ``(returncode, text)``.

    Routes through :func:`teatree.utils.run.run_allowed_to_fail` (the subprocess
    chokepoint) with ``expected_codes=None`` — a ``gh api`` 4xx is an expected
    probe outcome, not an error to raise on. ``args`` are the ``gh api`` operands
    (``repos/{slug}``, ``--method PATCH …``), so ``api`` is prepended here.
    """
    result = run_allowed_to_fail(["gh", "api", *args], expected_codes=None)
    return result.returncode, f"{result.stdout}\n{result.stderr}"


def _has_signal(text: str, signals: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(signal in lowered for signal in signals)


def _write_probe_verdict(code: int, out: str) -> _WriteVerdict:
    """Classify one write probe as ``denied`` / ``present`` / ``indeterminate``.

    ``denied`` when GitHub returned the route-level 403 :data:`_FORBIDDEN_SIGNAL`
    (the token lacks the permission). ``present`` when the probe REACHED a verdict
    past the write-authorization gate -- a zero exit or a :data:`_WRITE_REACHED_SIGNALS`
    (404/422) response on the non-existent target. ``indeterminate`` otherwise: a
    non-zero exit with NEITHER signal is a transport/network fault, NOT a grant --
    the fail-open the old ``not accessible``-only test collapsed into ``present``.
    """
    lowered = out.lower()
    if _FORBIDDEN_SIGNAL in lowered:
        return "denied"
    if code == 0 or _has_signal(lowered, _WRITE_REACHED_SIGNALS):
        return "present"
    return "indeterminate"


def _oauth_scopes(headers_text: str) -> frozenset[str] | None:
    """Return the classic-PAT scopes from an ``X-OAuth-Scopes`` response header.

    ``None`` when the header is absent — the signal that the token is a
    fine-grained PAT (which the caller judges by per-permission probe). A
    present-but-empty header yields an empty set (a classic token with no scopes).
    """
    for line in headers_text.splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == _OAUTH_SCOPES_HEADER:
            return frozenset(scope for scope in (s.strip() for s in value.split(",")) if scope)
    return None


def probe_token_permissions(slug: str, run: GhRunner | None = None) -> GhTokenProbe:
    """Probe whether ``gh``'s token holds every :data:`REQUIRED_PERMISSION_LABELS` on *slug*.

    ``slug`` is ``owner/repo``. Returns a :class:`GhTokenProbe`. Metadata is
    probed first with a read (``GET repos/{slug}`` with ``-i`` so the response
    headers come back): if the token cannot even read the repo the write probes
    cannot be interpreted (a no-access token 404s on everything), so the metadata
    failure short-circuits. A non-permission failure of the metadata read
    (network) yields an *indeterminate* result so a caller never fails the
    deploy/doctor on an unreachable API. On a successful read the token class is
    read from the ``X-OAuth-Scopes`` header: a classic PAT is judged by requiring
    the ``repo`` scope, a fine-grained PAT by the per-permission route probes.
    """
    run = run or _default_run
    if shutil.which("gh") is None:
        return GhTokenProbe(missing=(), indeterminate_reason="gh CLI not found on PATH")

    meta_code, meta_out = run(["-i", f"repos/{slug}"])
    if meta_code != 0:
        if _has_signal(meta_out, _DENIED_SIGNALS):
            return GhTokenProbe(missing=("metadata: read",))
        return GhTokenProbe(missing=(), indeterminate_reason=f"could not read repos/{slug} (API unreachable?)")

    scopes = _oauth_scopes(meta_out)
    if scopes is not None:
        # Classic PAT: the per-route 403 probe fails open for it, so gate on the
        # single write-granting ``repo`` scope. Missing it denies every write.
        if _CLASSIC_WRITE_SCOPE in scopes:
            return GhTokenProbe(missing=())
        return GhTokenProbe(missing=_WRITE_PERMISSION_LABELS)

    return _probe_fine_grained_writes(slug, run)


def _probe_fine_grained_writes(slug: str, run: GhRunner) -> GhTokenProbe:
    """Per-route write probes for a fine-grained PAT: classify each, then aggregate.

    A genuine denial is a definite gap -> report it (a loud FAIL) even alongside a
    transient probe, so a real permission gap is never masked. Only when NO probe
    was denied but one could not reach a verdict do we return indeterminate, so a
    network blip on a write probe SKIPS the preflight rather than falsely certifying
    (or falsely failing) the token.
    """
    missing: list[str] = []
    indeterminate: list[str] = []
    for label, template in _WRITE_PROBES:
        args = [part.format(slug=slug) for part in template]
        code, out = run(args)
        verdict = _write_probe_verdict(code, out)
        if verdict == "denied":
            missing.append(label)
        elif verdict == "indeterminate":
            indeterminate.append(label)
    if missing:
        return GhTokenProbe(missing=tuple(missing))
    if indeterminate:
        reason = f"write probe(s) did not reach a verdict: {', '.join(indeterminate)} (API unreachable?)"
        return GhTokenProbe(missing=(), indeterminate_reason=reason)
    return GhTokenProbe(missing=())


__all__ = [
    "REQUIRED_PERMISSION_LABELS",
    "GhRunner",
    "GhTokenProbe",
    "probe_token_permissions",
]
