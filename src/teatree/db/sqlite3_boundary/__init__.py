"""Django SQLite backend enforcing :mod:`teatree.db.boundary`'s one-writer-domain rule.

Referenced from settings as ``"ENGINE": "teatree.db.sqlite3_boundary"``; Django
imports :mod:`teatree.db.sqlite3_boundary.base` for the ``DatabaseWrapper``.
"""
