"""Pure path classifier for customer-display impact (#1967).

The mandatory-E2E gate must decide whether a changed-file set could affect what
is displayed to the customer. The decision is per-overlay (each project's
serializer/view/frontend/template layout differs) but the *rule* is uniform and
fail-closed: a path is presumed display-impacting unless it is explicitly known
NOT to ship to the customer.

So the only input that can mark a path safe is the overlay's set of
``non_impacting`` globs — test files, migration-only files, tooling. Every other
path — one the overlay considers display-impacting AND one the overlay's rules
never anticipated — resolves to impacting. This is deliberate: an unanticipated
path is not proof of no impact, it is a gap the rules did not cover, so it must
not silently skip the gate. Only when *every* path is explicitly non-impacting
does the set resolve to ``False`` — which includes the empty set, because the
gate only ever classifies a diff it actually read; a diff that could not be read
refuses upstream (``resolve_gate_inputs``) and never reaches this function.

The classifier owns no I/O and no ORM: it is a pure function over the file list
and the non-impacting glob tuple, so it is exhaustively testable and the same
code path serves the dogfood overlay, a product overlay, and the fail-closed
default.
"""

import fnmatch
from collections.abc import Sequence


def is_non_impacting_path(path: str, non_impacting: Sequence[str]) -> bool:
    """True iff *path* matches an explicit non-impacting glob (test/migration/tooling)."""
    return any(fnmatch.fnmatch(path, glob) for glob in non_impacting)


def classify_paths(changed_files: Sequence[str], non_impacting: Sequence[str]) -> bool:
    """True iff the changed-file set could impact customer display (fail-closed).

    Returns ``False`` only when *every* path matches a ``non_impacting`` glob (a
    verified empty diff included). Any impacting or unanticipated path returns ``True``.
    """
    return not all(is_non_impacting_path(path, non_impacting) for path in changed_files)
