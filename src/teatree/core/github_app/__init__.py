"""GitHub App inbound-payload processing (#4795).

Pure payload logic consumed by :mod:`teatree.core.views.github_webhook` (the
webhook transport) and the ``github_polling`` scanner (the poll transport) —
never a GitHub API caller itself, which is why this lives under
:mod:`teatree.core` rather than :mod:`teatree.backends.github` (concrete
backend implementations depend on core; core never depends on a concrete
backend). :func:`teatree.core.github_app.event_identity.identity_for` and
:func:`teatree.core.github_app.webhook_normalize.normalize` are the shared
seam both transports call, so they "invoke the identical persistence/dispatch
path" by construction.
"""
