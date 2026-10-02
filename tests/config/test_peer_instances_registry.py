# test-path: cross-cutting
"""The DB-home ``peer_instances`` registry, read off the cold ``ConfigSetting`` store.

The third ``REGISTRY_SETTINGS`` key, and the sibling of ``overlays`` / ``e2e_repos``: one
JSON-dict row that ``load_config._inject_db_registries`` reads via the Django-free
``cold_reader`` and populates into ``raw["peer_instances"]``, so ``load_peer_instances``
needs no write path of its own.

Integration-first, exactly as its siblings are tested: a real sqlite file at
``T3_CONFIG_DB`` (the canonical cold-path store), no mocks.

The urls are LOOPBACK TUNNEL addresses. Only the operator's own workstation lists peers, so
an unset registry — the state every other box is in — must be no peers rather than an error.
"""

from pathlib import Path

import pytest

from teatree.config import load_peer_instances
from teatree.config.peer_instance import DEFAULT_TIMEOUT_SECONDS

from ._shared import _seed_config_db


def test_a_registered_peer_loads_with_every_field_it_declares(config_db: Path) -> None:
    _seed_config_db(
        config_db,
        peer_instances={"box-b": {"url": "http://127.0.0.1:9401/", "note": "the laptop", "timeout_seconds": 2}},
    )

    peers = load_peer_instances()

    assert len(peers) == 1
    assert peers[0].name == "box-b"
    assert peers[0].url == "http://127.0.0.1:9401/"
    assert peers[0].note == "the laptop"
    assert peers[0].timeout_seconds == pytest.approx(2.0)


def test_a_peer_declaring_only_a_url_takes_the_shipped_timeout(config_db: Path) -> None:
    _seed_config_db(config_db, peer_instances={"box-b": {"url": "http://127.0.0.1:9401/"}})

    peer = load_peer_instances()[0]

    assert peer.note == ""
    assert peer.timeout_seconds == pytest.approx(DEFAULT_TIMEOUT_SECONDS)


def test_every_registered_peer_loads_not_just_the_first(config_db: Path) -> None:
    _seed_config_db(
        config_db,
        peer_instances={"box-b": {"url": "http://127.0.0.1:9401/"}, "box-c": {"url": "http://127.0.0.1:9402/"}},
    )

    assert sorted(peer.name for peer in load_peer_instances()) == ["box-b", "box-c"]


def test_no_registry_row_means_no_peers_rather_than_a_failure(config_db: Path) -> None:
    _seed_config_db(config_db)

    assert load_peer_instances() == []


def test_a_peer_declaring_no_tunnel_offers_no_command(config_db: Path) -> None:
    # The defect this whole field set answers: the registry key is a LABEL, and guessing
    # `ssh <label>` printed a command that resolved no host. Silence beats a wrong command.
    _seed_config_db(config_db, peer_instances={"box-b": {"url": "http://127.0.0.1:9401/"}})

    peer = load_peer_instances()[0]

    assert peer.tunnel is None
    assert peer.tunnel_command == ""


def test_an_ssh_peer_renders_a_forward_onto_the_host_it_declares_not_its_label(config_db: Path) -> None:
    _seed_config_db(
        config_db,
        peer_instances={
            "the-label-nothing-resolves": {
                "url": "http://127.0.0.1:9404/",
                "tunnel": {"host": "a-real-ssh-target"},
            }
        },
    )

    peer = load_peer_instances()[0]

    assert peer.tunnel_command.endswith("-L 9404:127.0.0.1:8000 a-real-ssh-target")
    assert "the-label-nothing-resolves" not in peer.tunnel_command


def test_the_forwards_local_port_is_read_off_the_url_so_the_two_cannot_drift(config_db: Path) -> None:
    _seed_config_db(
        config_db,
        peer_instances={"box-b": {"url": "http://127.0.0.1:9999/", "tunnel": {"host": "box-b.example"}}},
    )

    assert "-L 9999:127.0.0.1:" in load_peer_instances()[0].tunnel_command


def test_an_iap_peer_renders_the_gcloud_shape_because_plain_ssh_cannot_reach_it(config_db: Path) -> None:
    _seed_config_db(
        config_db,
        peer_instances={
            "jumpbox": {
                "url": "http://127.0.0.1:9405/",
                "tunnel": {
                    "host": "an-instance",
                    "transport": "gcloud-iap",
                    "options": ["--project", "a-project", "--zone", "a-zone"],
                },
            }
        },
    )

    assert load_peer_instances()[0].tunnel_command == (
        "gcloud compute ssh an-instance --project a-project --zone a-zone "
        "--tunnel-through-iap -- -N -o ExitOnForwardFailure=yes -L 9405:127.0.0.1:8000"
    )


def test_a_peer_may_state_a_remote_port_other_than_the_shipped_one(config_db: Path) -> None:
    _seed_config_db(
        config_db,
        peer_instances={"box-b": {"url": "http://127.0.0.1:9401/", "tunnel": {"host": "h", "remote_port": 9403}}},
    )

    assert load_peer_instances()[0].tunnel_command.endswith("9401:127.0.0.1:9403 h")


def test_an_unknown_transport_fails_loud_rather_than_falling_back_to_plain_ssh(config_db: Path) -> None:
    # Degrading to ssh here would print an unrunnable command for a box ssh cannot reach,
    # which is precisely the failure the transport field exists to end.
    _seed_config_db(
        config_db,
        peer_instances={"box-b": {"url": "http://127.0.0.1:9401/", "tunnel": {"host": "h", "transport": "carrier"}}},
    )

    with pytest.raises(ValueError, match="carrier"):
        load_peer_instances()


def test_a_url_naming_no_valid_port_offers_no_command_rather_than_raising(config_db: Path) -> None:
    # The port is READ off the url, and one typo there used to raise out of a property the
    # compare page evaluates for every peer — taking the whole page down, good peers included.
    _seed_config_db(
        config_db,
        peer_instances={"box-b": {"url": "http://127.0.0.1:94011/", "tunnel": {"host": "h"}}},
    )

    peer = load_peer_instances()[0]

    assert peer.local_port is None
    assert peer.tunnel_command == ""


def test_a_tunnel_that_is_not_a_table_fails_loud(config_db: Path) -> None:
    _seed_config_db(config_db, peer_instances={"box-b": {"url": "http://127.0.0.1:9401/", "tunnel": "h"}})

    with pytest.raises(ValueError, match="box-b"):
        load_peer_instances()


def test_a_tunnel_naming_no_host_fails_loud_rather_than_silently_offering_no_command(config_db: Path) -> None:
    _seed_config_db(
        config_db, peer_instances={"box-b": {"url": "http://127.0.0.1:9401/", "tunnel": {"remote_port": 8000}}}
    )

    with pytest.raises(ValueError, match="host"):
        load_peer_instances()


def test_a_null_host_fails_loud_rather_than_rendering_the_word_none(config_db: Path) -> None:
    _seed_config_db(config_db, peer_instances={"box-b": {"url": "http://127.0.0.1:9401/", "tunnel": {"host": None}}})

    with pytest.raises(ValueError, match="host"):
        load_peer_instances()


def test_a_remote_port_written_as_a_string_forwards_to_the_port_it_names(config_db: Path) -> None:
    # Silently substituting the shipped 8000 here RUNS and forwards to the wrong port, which
    # is worse than the unrunnable command the tunnel fields exist to end.
    _seed_config_db(
        config_db,
        peer_instances={"box-b": {"url": "http://127.0.0.1:9401/", "tunnel": {"host": "h", "remote_port": "9403"}}},
    )

    assert load_peer_instances()[0].tunnel_command.endswith("9401:127.0.0.1:9403 h")


def test_a_remote_port_naming_no_port_fails_loud(config_db: Path) -> None:
    _seed_config_db(
        config_db,
        peer_instances={"box-b": {"url": "http://127.0.0.1:9401/", "tunnel": {"host": "h", "remote_port": "eight"}}},
    )

    with pytest.raises(ValueError, match="remote_port"):
        load_peer_instances()


def test_a_boolean_remote_port_fails_loud_rather_than_forwarding_to_true(config_db: Path) -> None:
    # ``isinstance(True, int)`` is True, so an unguarded int check renders ``127.0.0.1:True``.
    _seed_config_db(
        config_db,
        peer_instances={"box-b": {"url": "http://127.0.0.1:9401/", "tunnel": {"host": "h", "remote_port": True}}},
    )

    with pytest.raises(ValueError, match="remote_port"):
        load_peer_instances()


def test_options_that_are_not_a_list_fail_loud_rather_than_dropping_every_one(config_db: Path) -> None:
    _seed_config_db(
        config_db,
        peer_instances={"box-b": {"url": "http://127.0.0.1:9401/", "tunnel": {"host": "h", "options": "--project p"}}},
    )

    with pytest.raises(ValueError, match="options"):
        load_peer_instances()


def test_a_malformed_tunnel_names_the_peer_it_came_from(config_db: Path) -> None:
    _seed_config_db(
        config_db,
        peer_instances={
            "good": {"url": "http://127.0.0.1:9401/"},
            "the-broken-one": {"url": "http://127.0.0.1:9402/", "tunnel": {"host": ""}},
        },
    )

    with pytest.raises(ValueError, match="the-broken-one"):
        load_peer_instances()
