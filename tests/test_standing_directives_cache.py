"""The published standing directives the Django-free hooks read (``teatree.standing_directives_cache``).

The worker publishes, a hook on the host reads, and the bind-mounted primary data
dir is the one place both reach — so the file is replaced atomically, rewritten only
when the published list changes, and anything unreadable reads as "not published".
"""

import json
import threading
from pathlib import Path

import pytest

from teatree import standing_directives_cache
from teatree.standing_directives_cache import StandingDirectivePayload

_ALPHA: list[StandingDirectivePayload] = [
    {"slot_id": "slot-a", "cadence_seconds": 300, "text": "Alpha rule.", "scope": "attended"}
]
_BETA: list[StandingDirectivePayload] = [
    {
        "slot_id": "slot-a",
        "cadence_seconds": 300,
        "text": "Alpha rule, edited by the owner. " * 30,
        "scope": "attended",
    },
    {"slot_id": "slot-b", "cadence_seconds": 600, "text": "Beta rule.", "scope": "attended-singleton"},
]


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"XDG_DATA_HOME": str(tmp_path / "data")}


@pytest.fixture
def cache_file(tmp_path: Path) -> Path:
    return tmp_path / "data" / "teatree" / "standing-directives.json"


class TestPublishAndRead:
    def test_a_published_list_reads_back(self, env: dict[str, str]) -> None:
        assert standing_directives_cache.write(_BETA, env=env) is True

        assert standing_directives_cache.read(env=env) == _BETA

    def test_it_lives_in_the_primary_data_dir_under_a_versioned_envelope(
        self, env: dict[str, str], cache_file: Path
    ) -> None:
        standing_directives_cache.write(_ALPHA, env=env)

        document = json.loads(cache_file.read_text(encoding="utf-8"))
        assert document["schema"] == 1
        assert isinstance(document["written_at"], float)
        assert document["directives"] == _ALPHA

    def test_an_unchanged_list_is_not_rewritten(self, env: dict[str, str], cache_file: Path) -> None:
        standing_directives_cache.write(_ALPHA, env=env)
        before = cache_file.read_bytes(), cache_file.stat().st_ino

        assert standing_directives_cache.write(_ALPHA, env=env) is False
        assert (cache_file.read_bytes(), cache_file.stat().st_ino) == before

    def test_a_changed_list_is_rewritten(self, env: dict[str, str]) -> None:
        standing_directives_cache.write(_ALPHA, env=env)

        assert standing_directives_cache.write(_BETA, env=env) is True
        assert standing_directives_cache.read(env=env) == _BETA

    def test_every_slot_off_is_a_publication_not_an_absence(self, env: dict[str, str]) -> None:
        standing_directives_cache.write([], env=env)

        assert standing_directives_cache.read(env=env) == []


def _document(*directives: dict) -> str:
    return json.dumps({"schema": 1, "written_at": 1.0, "directives": list(directives)})


class TestAnUnreadableFileIsNotPublished:
    def test_nothing_published_yet(self, env: dict[str, str]) -> None:
        assert standing_directives_cache.read(env=env) is None

    @pytest.mark.parametrize(
        "content",
        [
            "{ not json",
            json.dumps([]),
            json.dumps({"schema": 2, "written_at": 1.0, "directives": []}),
            json.dumps({"schema": 1, "written_at": 1.0, "directives": "slot-a"}),
            _document({"slot_id": "a", "text": "x", "scope": "attended"}),
            _document({"slot_id": "a", "cadence_seconds": 300, "text": "x"}),
            _document({"slot_id": "a", "cadence_seconds": "300", "text": "x", "scope": "attended"}),
            _document({"slot_id": "a", "cadence_seconds": True, "text": "x", "scope": "attended"}),
            _document({"slot_id": "a", "cadence_seconds": 300, "text": "x", "scope": None}),
        ],
        ids=[
            "corrupt",
            "not-an-object",
            "unknown-schema",
            "not-a-list",
            "missing-cadence",
            "missing-scope",
            "string-cadence",
            "bool-cadence",
            "non-string-scope",
        ],
    )
    def test_a_malformed_file_reads_as_nothing_published(
        self, env: dict[str, str], cache_file: Path, content: str
    ) -> None:
        cache_file.parent.mkdir(parents=True)
        cache_file.write_text(content, encoding="utf-8")

        assert standing_directives_cache.read(env=env) is None


class TestAtomicity:
    _REWRITES = 400

    def test_a_concurrent_reader_never_sees_a_torn_file(self, env: dict[str, str]) -> None:
        standing_directives_cache.write(_ALPHA, env=env)
        seen: list[list[StandingDirectivePayload] | None] = []
        done = threading.Event()

        def _read_until_done() -> None:
            while not done.is_set():
                seen.append(standing_directives_cache.read(env=env))

        reader = threading.Thread(target=_read_until_done)
        reader.start()
        try:
            for index in range(self._REWRITES):
                standing_directives_cache.write(_BETA if index % 2 == 0 else _ALPHA, env=env)
        finally:
            done.set()
            reader.join()

        assert seen
        assert all(published in {json.dumps(_ALPHA), json.dumps(_BETA)} for published in map(json.dumps, seen))

    def test_no_staging_file_is_left_behind(self, env: dict[str, str], cache_file: Path) -> None:
        standing_directives_cache.write(_ALPHA, env=env)
        standing_directives_cache.write(_BETA, env=env)

        assert sorted(path.name for path in cache_file.parent.iterdir()) == sorted(
            [cache_file.name, standing_directives_cache.LOCK_FILENAME]
        )
