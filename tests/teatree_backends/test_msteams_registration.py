"""The MS Teams presence factory registers into core and reads its token (#2171)."""

from teatree.backends.msteams import registration
from teatree.core import presence


def test_install_registers_msteams_factory() -> None:
    presence._FACTORIES.pop(registration.MSTEAMS_PRESENCE_BACKEND, None)
    try:
        registration.install_presence_backends()
        assert registration.MSTEAMS_PRESENCE_BACKEND in presence._FACTORIES
    finally:
        presence._FACTORIES.pop(registration.MSTEAMS_PRESENCE_BACKEND, None)


def test_factory_returns_none_without_token(monkeypatch) -> None:
    monkeypatch.setattr(registration, "read_pass", lambda ref: "")
    assert registration._build_msteams_presence("ms/tok") is None


def test_factory_builds_backend_with_resolved_token(monkeypatch) -> None:
    monkeypatch.setattr(registration, "read_pass", lambda ref: "graph-access-tok")
    backend = registration._build_msteams_presence("ms/tok")
    assert isinstance(backend, registration.MsTeamsPresenceBackend)


def test_empty_token_ref_never_reads_pass(monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(registration, "read_pass", lambda ref: called.append(ref) or "x")
    assert registration._build_msteams_presence("") is None
    assert called == []
