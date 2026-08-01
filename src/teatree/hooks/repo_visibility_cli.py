"""Shell seam for resolving a git remote's repo visibility.

The shell pre-push gates cannot import Python, so they used to shell
``gh repo view`` directly. That hard-codes ONE forge: a ``gitlab.com`` remote
can never resolve through ``gh`` (it errors on the namespace), so the gate's
visibility came back undetermined on every push and it fell into its
fail-closed branch -- scanning a private repo forever.

This CLI is the thin seam onto :mod:`teatree.hooks._repo_visibility`, which
already routes the probe by the remote's host segment (``gh`` for GitHub,
``glab`` for GitLab) and day-caches the verdict per slug, so a repeat push
costs no network call at all. It is deliberately Django-free -- the import
chain is ``cold_reader`` / ``git_config_offline`` / ``utils.run`` only -- so a
pre-push hook pays an interpreter start, not a framework boot.

Usage::

    python -m teatree.hooks.repo_visibility_cli <remote-url-or-slug>

Prints exactly one line -- ``PUBLIC`` / ``PRIVATE`` / ``INTERNAL`` /
``UNKNOWN`` -- and exits 0 whenever it could run. ``UNKNOWN`` is the fail-safe
verdict: callers must treat it as "NOT confirmed private" and keep enforcing,
so an absent forge CLI, an unparsable remote, or a probe error never silently
skips a leak scan.
"""

import sys

from teatree.hooks._repo_visibility import slug_for_remote_url, slug_visibility

UNKNOWN_VERDICT = "UNKNOWN"


def visibility_for_remote(url: str) -> str:
    """Return ``url``'s visibility verdict, or ``UNKNOWN_VERDICT``.

    The remote is normalized to a HOST-QUALIFIED slug first
    (:func:`_repo_visibility.slug_for_remote_url`) so the host-keyed probe
    routes to the forge the remote actually lives on. A host-stripped slug
    would default every remote to the GitHub probe.
    """
    slug = slug_for_remote_url(url.strip())
    if not slug:
        return UNKNOWN_VERDICT
    return slug_visibility(slug) or UNKNOWN_VERDICT


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].strip():
        sys.stdout.write(f"{UNKNOWN_VERDICT}\n")
        return 0
    sys.stdout.write(f"{visibility_for_remote(args[0])}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
