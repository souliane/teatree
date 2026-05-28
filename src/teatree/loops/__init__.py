"""Per-domain mini-loops + orchestrator (#1432, #1434).

The fat /loop tick (BLUEPRINT §5.6) is split into per-domain mini-loops with
a top-level :class:`Orchestrator` that fans out to enabled mini-loops on
their configured cadence. Each domain (dispatch, inbox, review, ship,
followup, tickets, housekeeping, arch_review, news, dogfood, audit) lives
in its own subpackage with a single :data:`MINI_LOOP` definition.

Discovery is via :mod:`pkgutil.iter_modules` — never a hardcoded list, so
adding a new mini-loop only requires dropping a new package under
``loops/``.
"""
