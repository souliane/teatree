"""Register the MS Teams presence backend into core (backends → core inversion, #2171).

Called from :meth:`teatree.backends.apps.BackendsConfig.ready`. Mirrors
:func:`teatree.backends.backend_provider.install_backend_provider`: ``core``
never imports ``backends``; ``backends`` registers the factory at app-ready.

The factory takes the token ``pass`` ref (resolved from ``[teatree.speak]
presence_token_ref`` by :func:`teatree.core.presence.current_presence`, which
may read config; backends may not) and reads the token via
:func:`teatree.utils.secrets.read_pass`. No token ⇒ no backend ⇒ the resolver
degrades to ``UNKNOWN`` (do not mute).
"""

from teatree.backends.msteams.presence import MsTeamsPresenceBackend
from teatree.core.presence import PresenceBackend, register_presence_backend
from teatree.utils.secrets import read_pass

MSTEAMS_PRESENCE_BACKEND = "msteams"


def _build_msteams_presence(token_ref: str) -> PresenceBackend | None:
    token = read_pass(token_ref) if token_ref else ""
    if not token:
        return None
    return MsTeamsPresenceBackend(access_token=token)


def install_presence_backends() -> None:
    register_presence_backend(MSTEAMS_PRESENCE_BACKEND, _build_msteams_presence)
