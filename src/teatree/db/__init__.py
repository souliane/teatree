"""Database plumbing that Django's own backends do not cover.

:mod:`teatree.db.boundary` states the single-read-write-domain invariant for the
canonical control database; :mod:`teatree.db.sqlite3_boundary` is the Django
SQLite backend that enforces it at connection-open time.
"""
