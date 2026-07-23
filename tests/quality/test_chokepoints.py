"""Conformance ledger for the chokepoint registry (call-site authorization).

Sibling of ``test_catalog.py`` / ``test_regression_rules.py``. The green-on-tree
assertion is what lets the checker be a trusted blocking gate and keeps main
green: a real violation on ``src/teatree`` turns it red. The anti-vacuous suite
proves the gate actually bites. The reachability ledger proves no entry can cite
a symbol/module that does not exist. The Tier-2 self-maintenance assertion pins
the subprocess coverage so the registry can never regress below the deleted
``check_subprocess_ban.py``.
"""

import importlib.util
import inspect
from pathlib import Path
from typing import Protocol

import pytest

import scripts.hooks.check_chokepoints as checker
from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
from teatree.quality.chokepoints import Chokepoint, ChokepointError, load_registry, registry_path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"

_HISTORICAL_SUBPROCESS_ATTRS = frozenset({"run", "Popen", "check_output", "check_call", "call"})


@pytest.fixture(scope="module")
def registry() -> tuple[Chokepoint, ...]:
    return load_registry()


@pytest.fixture
def src_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "src" / "teatree" / "feature" / "mod.py"
    target.parent.mkdir(parents=True)
    return target


class TestSchemaInvariants:
    def test_registry_is_non_empty(self, registry: tuple[Chokepoint, ...]) -> None:
        assert registry

    def test_ids_are_unique_and_kebab(self, registry: tuple[Chokepoint, ...]) -> None:
        ids = [e.id for e in registry]
        assert len(ids) == len(set(ids))
        assert all("-" not in i or i.islower() for i in ids)

    def test_match_kind_in_enum(self, registry: tuple[Chokepoint, ...]) -> None:
        assert all(e.match_kind in {"module_attr", "method"} for e in registry)

    def test_attrs_and_allowed_modules_non_empty(self, registry: tuple[Chokepoint, ...]) -> None:
        for entry in registry:
            assert entry.protected_attrs, f"{entry.id}: empty protected_attrs"
            assert entry.allowed_modules, f"{entry.id}: empty allowed_modules"

    def test_protected_symbol_present_iff_module_attr(self, registry: tuple[Chokepoint, ...]) -> None:
        for entry in registry:
            if entry.match_kind == "module_attr":
                assert entry.protected_symbol
            else:
                assert entry.protected_symbol == ""


class TestReachabilityLedger:
    def test_every_allowed_module_resolves_to_a_real_file(self, registry: tuple[Chokepoint, ...]) -> None:
        for entry in registry:
            for module in entry.allowed_modules:
                rel = Path(*module.split(".")).with_suffix(".py")
                pkg = _SRC / Path(*module.split(".")) / "__init__.py"
                assert (_SRC / rel).is_file() or pkg.is_file(), (
                    f"{entry.id}: allowed_module {module!r} resolves to no file under src/"
                )

    def test_module_attr_symbol_is_importable(self, registry: tuple[Chokepoint, ...]) -> None:
        for entry in registry:
            if entry.match_kind != "module_attr":
                continue
            assert importlib.util.find_spec(entry.protected_symbol) is not None, (
                f"{entry.id}: protected_symbol {entry.protected_symbol!r} is not importable"
            )

    def test_on_behalf_attrs_are_real_backend_methods(self, registry: tuple[Chokepoint, ...]) -> None:
        by_id = {e.id: e for e in registry}
        routed = by_id["on-behalf-routed-egress"]
        for attr in routed.protected_attrs:
            assert callable(getattr(SlackBotBackend, attr, None)), f"SlackBotBackend has no {attr}"
            assert hasattr(MessagingBackend, attr), f"MessagingBackend protocol has no {attr}"
        colleague = by_id["on-behalf-colleague-primitives"]
        for attr in colleague.protected_attrs:
            assert callable(getattr(SlackBotBackend, attr, None)), f"SlackBotBackend has no {attr}"


class TestCheckerBehavior:
    def test_green_on_real_tree(self) -> None:
        assert checker.main(["--all"]) == 0

    def test_flags_subprocess_run_outside_allowed(self, src_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
        src_file.write_text("import subprocess\nsubprocess.run(['ls'], check=False)\n", encoding="utf-8")
        assert checker.main([str(src_file)]) == 1
        err = capsys.readouterr().err
        assert "subprocess.run(...)" in err
        assert "subprocess-egress" in err

    def test_allows_subprocess_in_wrapper_module(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "src" / "teatree" / "utils" / "run.py"
        target.parent.mkdir(parents=True)
        target.write_text("import subprocess\nsubprocess.run(['ls'], check=False)\n", encoding="utf-8")
        assert checker.main([str(target)]) == 0

    def test_subprocess_annotations_and_except_not_flagged(self, src_file: Path) -> None:
        src_file.write_text(
            "import subprocess\n"
            "procs: list[subprocess.Popen[str]] = []\n"
            "try:\n"
            "    pass\n"
            "except subprocess.CalledProcessError:\n"
            "    pass\n",
            encoding="utf-8",
        )
        assert checker.main([str(src_file)]) == 0

    def test_os_system_not_flagged(self, src_file: Path) -> None:
        src_file.write_text("import os\nos.system('ls')\n", encoding="utf-8")
        assert checker.main([str(src_file)]) == 0

    def test_flags_post_routed_outside_egress(self, src_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
        src_file.write_text("backend.post_routed(channel='c', text='t')\n", encoding="utf-8")
        assert checker.main([str(src_file)]) == 1
        assert "on-behalf-routed-egress" in capsys.readouterr().err

    def test_allows_post_routed_inside_egress(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "src" / "teatree" / "core" / "on_behalf_egress.py"
        target.parent.mkdir(parents=True)
        target.write_text("self._messaging.post_routed(channel='c', text='t')\n", encoding="utf-8")
        assert checker.main([str(target)]) == 0

    def test_method_definition_not_flagged(self, src_file: Path) -> None:
        src_file.write_text("def post_routed(self, *, channel, text):\n    return {}\n", encoding="utf-8")
        assert checker.main([str(src_file)]) == 0

    def test_flags_raw_backend_react(self, src_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
        src_file.write_text("backend.react(channel='c', ts='1', emoji='eyes')\n", encoding="utf-8")
        assert checker.main([str(src_file)]) == 1
        assert "on-behalf-colleague-primitives" in capsys.readouterr().err

    def test_egress_receiver_react_is_exempt(self, src_file: Path) -> None:
        src_file.write_text(
            "egress.react(channel='c', ts='1', emoji='eyes')\n"
            "OnBehalfSlackEgress(backend).react(channel='c', ts='1', emoji='eyes')\n",
            encoding="utf-8",
        )
        assert checker.main([str(src_file)]) == 0

    def test_allows_post_message_at_documented_sink(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "src" / "teatree" / "core" / "notify.py"
        target.parent.mkdir(parents=True)
        target.write_text("backend.post_message(channel='c', text='t', thread_ts='')\n", encoding="utf-8")
        assert checker.main([str(target)]) == 0

    def test_flags_django_setup_outside_bootstrap(self, src_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
        src_file.write_text("import django\ndjango.setup()\n", encoding="utf-8")
        assert checker.main([str(src_file)]) == 1
        err = capsys.readouterr().err
        assert "django.setup(...)" in err
        assert "django-setup-bootstrap" in err

    def test_allows_django_setup_inside_bootstrap(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "src" / "teatree" / "utils" / "django_bootstrap.py"
        target.parent.mkdir(parents=True)
        target.write_text("import django\ndjango.setup()\n", encoding="utf-8")
        assert checker.main([str(target)]) == 0

    def test_tests_directory_never_scanned(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "tests" / "test_thing.py"
        target.parent.mkdir(parents=True)
        target.write_text("import subprocess\nsubprocess.run(['ls'])\n", encoding="utf-8")
        assert checker.main([str(target)]) == 0

    def test_flags_close_issue_outside_the_forge_write_seam(
        self, src_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src_file.write_text("host.close_issue(issue_url='u', comment='leak Contoso')\n", encoding="utf-8")
        assert checker.main([str(src_file)]) == 1
        err = capsys.readouterr().err
        assert "close_issue(...)" in err
        assert "forge-comment-write-seam" in err

    def test_flags_post_pr_comment_outside_the_forge_write_seam(
        self, src_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src_file.write_text("host.post_pr_comment(repo='o/r', pr_iid=1, body='raw')\n", encoding="utf-8")
        assert checker.main([str(src_file)]) == 1
        assert "forge-comment-write-seam" in capsys.readouterr().err

    def test_flags_create_issue_outside_the_forge_write_seam(
        self, src_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src_file.write_text("host.create_issue(repo='o/r', title='t', body='b')\n", encoding="utf-8")
        assert checker.main([str(src_file)]) == 1
        assert "forge-comment-write-seam" in capsys.readouterr().err

    def test_allows_forge_write_inside_a_scrubbing_seam_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "src" / "teatree" / "loop" / "mechanical.py"
        target.parent.mkdir(parents=True)
        target.write_text("host.close_issue(issue_url='u', comment=clean)\n", encoding="utf-8")
        assert checker.main([str(target)]) == 0

    def test_forge_write_method_definition_not_flagged(self, src_file: Path) -> None:
        src_file.write_text("def close_issue(self, *, issue_url, comment=''):\n    return {}\n", encoding="utf-8")
        assert checker.main([str(src_file)]) == 0


_FORGE_WRITE_BODY_METHODS = frozenset(
    {
        "post_pr_comment",
        "update_pr_comment",
        "post_issue_comment",
        "update_issue_comment",
        "close_issue",
        "create_issue",
        "create_sub_issue",
        "update_issue",
    }
)

#: The reviewed allowlist of NON-content parameter names — every parameter a
#: ``CodeHostBackend`` method may carry that does NOT ferry outbound colleague-
#: visible body text (identifiers, slugs, filters, upload handles). A method is
#: body-bearing when its signature carries ANY parameter outside this set, so a
#: NEW parameter name is treated as content by default: the guard fails closed —
#: adding a body-shaped parameter (or a whole new forge-write method) turns the
#: seam test red the moment it lands, even if nobody remembered to register it.
#: Documented exclusions that are content-free by construction: ``create_pr``
#: carries its body inside a ``PullRequestSpec`` (``spec``), ``search_open_issues``
#: takes a ``query`` filter, and ``upload_file`` takes a ``filepath`` — none is a
#: colleague-visible message body.
_NON_CONTENT_PARAMS = frozenset(
    {
        "self",
        "issue_url",
        "repo",
        "repo_slugs",
        "parent_url",
        "comment_id",
        "slug",
        "pr_id",
        "pr_iid",
        "pr_url",
        "reviewer",
        "login",
        "assignee",
        "author",
        # Singular ``label`` is a READ-query scope (``list_labeled_issues``), like
        # ``author``/``assignee``. The plural ``labels`` — what a write SETS — is
        # deliberately absent, so a labelling write still enumerates as content.
        "label",
        "updated_after",
        "state",
        "query",
        "expected_head_oid",
        "child_type",
        "spec",
        "filepath",
        "upload",
    }
)


def _protocol_body_bearing_methods(protocol: type) -> set[str]:
    """Every ``protocol`` method whose signature carries a non-allowlisted (body-bearing) parameter."""
    found: set[str] = set()
    for name in dir(protocol):
        if name.startswith("_"):
            continue
        attr = getattr(protocol, name)
        if not callable(attr):
            continue
        try:
            params = inspect.signature(attr).parameters
        except (TypeError, ValueError):
            continue
        if set(params) - _NON_CONTENT_PARAMS:
            found.add(name)
    return found


class TestSelfMaintenance:
    def test_subprocess_attrs_cover_historical_ban(self, registry: tuple[Chokepoint, ...]) -> None:
        entry = next(e for e in registry if e.id == "subprocess-egress")
        assert set(entry.protected_attrs) >= _HISTORICAL_SUBPROCESS_ATTRS

    def test_forge_write_seam_registers_every_body_bearing_protocol_method(
        self, registry: tuple[Chokepoint, ...]
    ) -> None:
        """The seam registers EXACTLY the protocol's body-bearing forge-write methods.

        This is the anti-false-assurance guard: it ENUMERATES the CodeHostBackend
        protocol and derives "body-bearing" from each method's signature, so a NEW
        colleague-visible forge-write method (any method taking body/title/comment/
        labels/description) that is added to the protocol but NOT registered here
        turns this test RED — the exact way ``create_sub_issue`` slipped through a
        hardcoded-list check. The registry, the local constant, and the live
        protocol enumeration must all agree.
        """
        entry = next(e for e in registry if e.id == "forge-comment-write-seam")
        enumerated = _protocol_body_bearing_methods(CodeHostBackend)
        assert enumerated, "protocol enumeration found no body-bearing methods — the heuristic is broken"
        assert enumerated == _FORGE_WRITE_BODY_METHODS, (
            "protocol body-bearing methods drifted from the registered set: "
            f"unregistered={sorted(enumerated - _FORGE_WRITE_BODY_METHODS)}, "
            f"stale={sorted(_FORGE_WRITE_BODY_METHODS - enumerated)}"
        )
        assert set(entry.protected_attrs) == _FORGE_WRITE_BODY_METHODS

    def test_new_body_bearing_method_is_flagged_by_default(self) -> None:
        """A method carrying an un-allowlisted parameter is flagged — the fail-closed teeth.

        The allowlist inversion means any NEW parameter name (here ``message``) is
        body-bearing by default, so a forge-write method added to the protocol
        without touching the registry is caught automatically.
        """

        class _Probe(Protocol):
            def announce(self, *, repo: str, message: str) -> None: ...

        assert _protocol_body_bearing_methods(_Probe) == {"announce"}


class TestLoaderValidation:
    def _load(self, tmp_path: Path, body: str) -> tuple[Chokepoint, ...]:
        path = tmp_path / "chokepoints.yaml"
        path.write_text(body, encoding="utf-8")
        return load_registry(path)

    def test_real_registry_loads(self) -> None:
        assert registry_path().is_file()
        assert load_registry()

    def test_bad_match_kind_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  concern: c\n  match_kind: regex\n"
            "  protected_attrs: [run]\n  allowed_modules: [teatree.utils.run]\n"
        )
        with pytest.raises(ChokepointError, match="match_kind must be one of"):
            self._load(tmp_path, body)

    def test_duplicate_id_rejected(self, tmp_path: Path) -> None:
        one = (
            "- id: dup\n  name: X\n  concern: c\n  match_kind: method\n"
            "  protected_attrs: [react]\n  allowed_modules: [teatree.core.notify]\n"
        )
        with pytest.raises(ChokepointError, match="duplicate id"):
            self._load(tmp_path, one + one)

    def test_non_kebab_id_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: Not_Kebab\n  name: X\n  concern: c\n  match_kind: method\n"
            "  protected_attrs: [react]\n  allowed_modules: [teatree.core.notify]\n"
        )
        with pytest.raises(ChokepointError, match="kebab slug"):
            self._load(tmp_path, body)

    def test_empty_allowed_modules_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  concern: c\n  match_kind: method\n"
            "  protected_attrs: [react]\n  allowed_modules: []\n"
        )
        with pytest.raises(ChokepointError, match="allowed_modules must be a non-empty list"):
            self._load(tmp_path, body)

    def test_empty_protected_attrs_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  concern: c\n  match_kind: method\n"
            "  protected_attrs: []\n  allowed_modules: [teatree.core.notify]\n"
        )
        with pytest.raises(ChokepointError, match="protected_attrs must be a non-empty list"):
            self._load(tmp_path, body)

    def test_module_attr_without_symbol_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  concern: c\n  match_kind: module_attr\n"
            "  protected_attrs: [run]\n  allowed_modules: [teatree.utils.run]\n"
        )
        with pytest.raises(ChokepointError, match="requires a non-empty protected_symbol"):
            self._load(tmp_path, body)

    def test_method_with_symbol_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  concern: c\n  match_kind: method\n  protected_symbol: foo\n"
            "  protected_attrs: [react]\n  allowed_modules: [teatree.core.notify]\n"
        )
        with pytest.raises(ChokepointError, match="protected_symbol is forbidden on a method entry"):
            self._load(tmp_path, body)

    def test_bad_module_path_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  concern: c\n  match_kind: method\n"
            "  protected_attrs: [react]\n  allowed_modules: ['src/teatree/core/notify.py']\n"
        )
        with pytest.raises(ChokepointError, match="not a dotted module path"):
            self._load(tmp_path, body)

    def test_malformed_yaml_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ChokepointError):
            self._load(tmp_path, "- id: x\n  name: [unterminated\n")

    def test_top_level_not_a_list_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ChokepointError, match="top-level YAML list"):
            self._load(tmp_path, "id: x\nname: X\n")

    def test_empty_list_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ChokepointError, match="top-level YAML list"):
            self._load(tmp_path, "[]\n")

    def test_non_mapping_entry_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ChokepointError, match="each entry must be a mapping"):
            self._load(tmp_path, "- just-a-string\n")

    def test_scalar_str_list_field_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  concern: c\n  match_kind: method\n"
            "  protected_attrs: react\n  allowed_modules: [teatree.core.notify]\n"
        )
        with pytest.raises(ChokepointError, match="must be a list of strings"):
            self._load(tmp_path, body)

    def test_non_string_list_element_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  name: X\n  concern: c\n  match_kind: method\n"
            "  protected_attrs: [3]\n  allowed_modules: [teatree.core.notify]\n"
        )
        with pytest.raises(ChokepointError, match="must be a list of non-empty strings"):
            self._load(tmp_path, body)

    def test_missing_required_field_rejected(self, tmp_path: Path) -> None:
        body = (
            "- id: x\n  concern: c\n  match_kind: method\n"
            "  protected_attrs: [react]\n  allowed_modules: [teatree.core.notify]\n"
        )
        with pytest.raises(ChokepointError, match="required string field missing or empty: 'name'"):
            self._load(tmp_path, body)
