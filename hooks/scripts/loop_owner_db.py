"""The skip-consult knob for the hooks' t3-master DB ``LoopLease`` read (#2851).

``_db_live_foreign_owner`` stays in the router (its tests patch the router's
``_db_lease_consult_disabled`` / ``bootstrap_teatree_django`` bindings).
"""

import os

# Skips the ``LoopLease`` DB cross-check (and its ``django.setup()``);
# collapses to the same fail-open value an absent DB already yields.
_SKIP_DB_LEASE_CONSULT_ENV = "T3_LOOP_SKIP_DB_LEASE_CONSULT"


def db_lease_consult_disabled() -> bool:
    return os.environ.get(_SKIP_DB_LEASE_CONSULT_ENV) == "1"
