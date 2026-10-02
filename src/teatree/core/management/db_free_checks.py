"""System checks that never open a database, for commands that must run where none is reachable.

Django 6.1 runs a command's checks against every configured database when none is
named, so the JSONField support check connects before ``handle()`` runs. A command
whose own contract is to answer without the database — ``pr``'s host-side
control-DB refusal, a static docs generator — would instead die on an
``OperationalError`` from that connection. Naming no databases keeps every static
check and drops only the database-tagged ones.
"""

from typing import Any


def without_database_checks(check_kwargs: dict[str, Any]) -> dict[str, Any]:
    return {**check_kwargs, "databases": []}
