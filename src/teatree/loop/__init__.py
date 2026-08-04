"""The machinery one loop tick runs on — not the loops themselves.

``teatree.loop`` and its sibling :mod:`teatree.loops` are one letter apart and
hold different layers, so read this before reaching for either:

- **``loop``** (here) is the shared engine: claiming and dispatching a unit
    (:mod:`~teatree.loop.dispatch`, :mod:`~teatree.loop.admission`), the
    scanners that decide there is work at all (:mod:`~teatree.loop.scanners`),
    the phase runners (:mod:`~teatree.loop.phases`), and the statusline. It
    knows nothing about any particular domain.
- **``loops``** is the domains: one subpackage per mini-loop — ``inbox``,
    ``review``, ``ship``, ``tickets``, ``news``, ``dream`` and the rest — each
    exporting a single ``MINI_LOOP`` definition, discovered by ``pkgutil``.

So a change to *how a tick is claimed, gated or dispatched* belongs here; a
change to *what one named loop does on its cadence* belongs under ``loops``.
"""
