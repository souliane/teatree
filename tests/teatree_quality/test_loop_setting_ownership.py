"""The loop-ownership derivation, exercised over a real source tree on disk.

Each case builds the smallest teatree-shaped tree that exhibits one property and reads
the answer back, so the derivation is tested against files an AST actually parses rather
than a mocked parse result.
"""

from pathlib import Path

from teatree.quality.loop_setting_ownership import LoopLayerIndex, loop_owned_settings

_READS_SHARED_GATE = "def _build_jobs(**_):\n    from teatree.loop.shared_gate import armed\n\n    return armed()\n"

_JOB_IDENTITY = '''
class Domain:
    """Stand-in for the real partition enum."""

    SHIP = "ship"
    ARCH_REVIEW = "arch_review"
'''

_DOMAIN_JOBS = """
from teatree.loop.scanner_factories import _ship_scanner_for

def _ship_jobs_for_overlay(backend):
    return [_ship_scanner_for(backend)]

_JOBS_BY_DOMAIN = {Domain.SHIP: _ship_jobs_for_overlay}
"""

#: 6 of the 11 real entry points arrive this way — defined in a sibling module and only
#: NAMED in the dispatch dict.
_DOMAIN_JOBS_IMPORTING_ITS_BUILDER = """
from teatree.loop.domain_optional_scanner_jobs import _arch_review_jobs_for_overlay

_PER_OVERLAY_DOMAIN_BUILDERS = {Domain.ARCH_REVIEW: _arch_review_jobs_for_overlay}
"""


def _tree(root: Path, files: dict[str, str]) -> Path:
    source = root / "teatree"
    written = {"loop/job_identity.py": _JOB_IDENTITY, "loop/domain_jobs.py": _DOMAIN_JOBS, **files}
    for name, text in written.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (source / "loops").mkdir(parents=True, exist_ok=True)
    return source


class TestAKeyReadInsideOneLoopIsThatLoops:
    def test_a_read_in_the_loop_package_is_owned(self, tmp_path: Path) -> None:
        source = _tree(
            tmp_path,
            {
                "loops/dream/loop.py": "MINI_LOOP = 1\n",
                "loops/dream/pass_config.py": "def phases(settings):\n    return settings.dream_merge\n",
            },
        )
        assert loop_owned_settings(source, ["dream_merge"]) == {"dream_merge": "dream"}

    def test_a_factory_the_loop_imports_at_tick_time_is_the_loops_code(self, tmp_path: Path) -> None:
        source = _tree(
            tmp_path,
            {
                "loops/news/loop.py": (
                    "def _build_jobs(**_):\n"
                    "    from teatree.loop.global_scanner_factories import _news_scanner\n\n"
                    "    return [_news_scanner()]\n"
                ),
                "loop/global_scanner_factories.py": "def _news_scanner():\n    return settings.scanning_news_skill\n",
            },
        )
        assert loop_owned_settings(source, ["scanning_news_skill"]) == {"scanning_news_skill": "news"}

    def test_a_scanner_reached_through_a_package_reexport_is_still_the_loops_code(self, tmp_path: Path) -> None:
        source = _tree(
            tmp_path,
            {
                "loops/housekeeping/loop.py": (
                    "def _build_jobs(**_):\n"
                    "    from teatree.loop.global_scanner_factories import _self_update_scanner\n\n"
                    "    return [_self_update_scanner()]\n"
                ),
                "loop/global_scanner_factories.py": (
                    "from teatree.loop.scanners import SelfUpdateScanner\n\n"
                    "def _self_update_scanner():\n    return SelfUpdateScanner()\n"
                ),
                "loop/scanners/__init__.py": "from teatree.loop.scanners.self_update import SelfUpdateScanner\n",
                "loop/scanners/self_update.py": (
                    "class SelfUpdateScanner:\n"
                    "    def scan(self, settings):\n        return settings.synthetic_loop_setting\n"
                ),
            },
        )
        assert loop_owned_settings(source, ["synthetic_loop_setting"]) == {"synthetic_loop_setting": "housekeeping"}

    def test_a_per_overlay_loop_reaches_its_factory_through_the_domain_dispatch_dict(self, tmp_path: Path) -> None:
        source = _tree(
            tmp_path,
            {
                "loops/ship/loop.py": (
                    "def _build_jobs(backends=None, **_):\n"
                    "    from teatree.loop.domain_jobs import jobs_for_domain\n"
                    "    from teatree.loop.job_identity import Domain\n\n"
                    "    return jobs_for_domain(Domain.SHIP, backends)\n"
                ),
                "loop/scanner_factories.py": (
                    "def _ship_scanner_for(backend):\n    return settings.synthetic_loop_setting\n"
                ),
            },
        )
        assert loop_owned_settings(source, ["synthetic_loop_setting"]) == {"synthetic_loop_setting": "ship"}

    def test_an_entry_point_only_imported_into_the_dispatch_dict_still_propagates(self, tmp_path: Path) -> None:
        source = _tree(
            tmp_path,
            {
                "loop/domain_jobs.py": _DOMAIN_JOBS_IMPORTING_ITS_BUILDER,
                "loops/arch_review/loop.py": (
                    "def _build_jobs(backends=None, **_):\n"
                    "    from teatree.loop.domain_jobs import jobs_for_domain\n"
                    "    from teatree.loop.job_identity import Domain\n\n"
                    "    return jobs_for_domain(Domain.ARCH_REVIEW, backends)\n"
                ),
                "loop/domain_optional_scanner_jobs.py": (
                    "def _arch_review_jobs_for_overlay(backend):\n    return settings.architectural_review_enabled\n"
                ),
            },
        )
        owned = loop_owned_settings(source, ["architectural_review_enabled"])
        assert owned == {"architectural_review_enabled": "arch_review"}


class TestAKeyTwoConcernsReadIsNobodys:
    def test_a_key_two_loops_read_is_owned_by_neither(self, tmp_path: Path) -> None:
        source = _tree(
            tmp_path,
            {
                "loops/dream/loop.py": _READS_SHARED_GATE,
                "loops/news/loop.py": _READS_SHARED_GATE,
                "loop/shared_gate.py": "def armed():\n    return settings.send_proxy_mode\n",
            },
        )
        assert loop_owned_settings(source, ["send_proxy_mode"]) == {}

    def test_a_read_outside_the_loop_layer_disqualifies_the_key(self, tmp_path: Path) -> None:
        source = _tree(
            tmp_path,
            {
                "loops/review/loop.py": "def _build_jobs(**_):\n    return settings.review_skill\n",
                "core/gates/review_skill_gate.py": "def resolve(settings):\n    return settings.review_skill\n",
            },
        )
        assert loop_owned_settings(source, ["review_skill"]) == {}

    def test_a_declaration_is_not_a_read(self, tmp_path: Path) -> None:
        source = _tree(
            tmp_path,
            {
                "loops/dream/loop.py": "def _build_jobs(**_):\n    return settings.dream_merge\n",
                "config/settings.py": "class UserSettings:\n    dream_merge: bool = True\n",
            },
        )
        assert loop_owned_settings(source, ["dream_merge"]) == {"dream_merge": "dream"}

    def test_a_migration_naming_a_key_is_frozen_history_not_a_consumer(self, tmp_path: Path) -> None:
        source = _tree(
            tmp_path,
            {
                "loops/dream/loop.py": "def _build_jobs(**_):\n    return settings.dream_merge\n",
                "core/migrations/0001_initial.py": 'KEYS = ["dream_merge"]\n',
            },
        )
        assert loop_owned_settings(source, ["dream_merge"]) == {"dream_merge": "dream"}

    def test_a_comment_mentioning_a_key_is_not_a_read(self, tmp_path: Path) -> None:
        source = _tree(
            tmp_path,
            {
                "loops/dream/loop.py": "def _build_jobs(**_):\n    return settings.dream_merge\n",
                "core/notes.py": "# dream_merge is described here but never read\nVALUE = 1\n",
            },
        )
        assert loop_owned_settings(source, ["dream_merge"]) == {"dream_merge": "dream"}


class TestTheIndexReportsTheLoopsItFound:
    def test_a_package_without_a_loop_module_is_not_a_loop(self, tmp_path: Path) -> None:
        source = _tree(
            tmp_path,
            {"loops/dream/loop.py": "MINI_LOOP = 1\n", "loops/shared/helpers.py": "VALUE = 1\n"},
        )
        assert LoopLayerIndex(source).loop_names == ("dream",)
