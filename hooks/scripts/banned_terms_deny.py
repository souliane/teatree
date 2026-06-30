"""Destination-aware emit for a REAL banned-terms match (#1415).

Split out of ``hook_router.py`` (a shrink-only module-health-capped god-module)
so the banned-term deny decision and its carve-out chain live in a bare sibling
module the router imports. The router keeps only the thin call site; this module
owns "given a configured banned term in a resolved publish payload, does a
carve-out downgrade it to a warn, or does it hard-block?".

The carve-out chain, in order. (1) ``publish_surface.carve_out_applies`` — a
``git commit`` to a known-private repo or a pure ``gh``/``glab`` post to a
known-private target, where the repo's own domain words are expected. (2)
``own_slug_term_downgrades`` — a ``git commit`` whose tripped term IS the repo's
own org/repo slug, landing in that private repo; a work-item URL naming the repo
is not a leak. (3) ``own_repo_url_carve_out`` — a structured ``gh``/``glab`` post
to a PUBLIC surface whose term appears ONLY inside a URL of one of the overlay's
own configured repos; the address of the repo is not a leak. (4)
``own_dest_slug_carve_out.term_is_destination_own_slug`` (#2597) — a structured
post whose tripped term IS the OWN repo slug of every publish destination (the
overlay's private tracker, named after the overlay); the repo's own name on its
own surface is not a leak. Unlike (1)-(3) this keys off the resolved destination
slug, not ``private_repos``, so it needs no config; a public destination whose
slug omits the term still hard-blocks.

Anything not downgraded hard-blocks, after emitting the #1657 unknown-visibility
NOTE when the target's visibility could not be resolved in-hook.
"""

import sys
from pathlib import Path


def emit_banned_term_deny(tool_name: str, command: str, payload: str, term: str, cwd_repo: Path | None) -> bool:
    from hook_router import emit_pretooluse_deny  # noqa: PLC0415

    from teatree.hooks import (  # noqa: PLC0415
        banned_terms_scanner,
        own_dest_slug_carve_out,
        own_repo_url_carve_out,
        publish_surface,
    )

    if publish_surface.carve_out_applies(tool_name, command, payload, cwd_repo):
        sys.stderr.write(
            f"WARNING: banned-terms gate (#1415) — term '{term}' on a private-repo commit; "
            "downgraded to warn (#126). The repo's own domain words are expected on its commits.\n"
        )
        return False
    if tool_name == "Bash" and publish_surface.own_slug_term_downgrades(command, term, cwd_repo):
        sys.stderr.write(
            f"WARNING: banned-terms gate (#1415) — term '{term}' is this private repo's own slug "
            "on its own commit; downgraded to warn (#126). A work-item URL naming the repo is not a leak.\n"
        )
        return False
    if (
        tool_name == "Bash"
        and publish_surface.is_gh_glab_posting_command(command)
        and own_repo_url_carve_out.term_only_inside_own_repo_urls(payload, term)
    ):
        sys.stderr.write(
            f"WARNING: banned-terms gate (#1415) — term '{term}' appears only inside a URL of "
            "the overlay's own configured repo; downgraded to warn. The address of the repo is not a leak.\n"
        )
        return False
    if tool_name == "Bash" and own_dest_slug_carve_out.term_is_destination_own_slug(command, term, cwd_repo):
        sys.stderr.write(
            f"WARNING: banned-terms gate (#1415/#2597) — term '{term}' is the OWN repo slug of this "
            "post's private destination; downgraded to warn. The tracker named after the overlay is "
            "not a leak on its own surface; a public destination whose slug omits the term still blocks.\n"
        )
        return False
    unknown_slug = publish_surface.visibility_unknown_for_block(command, cwd_repo)
    if unknown_slug:
        sys.stderr.write(
            f"NOTE: banned-terms gate (#1415/#1657) — target '{unknown_slug}' visibility unknown in-hook "
            "(probe unavailable). If private, add it to [teatree] private_repos in ~/.teatree.toml "
            "for a reliable offline carve-out.\n"
        )
    return emit_pretooluse_deny(banned_terms_scanner.format_block_message(term))
