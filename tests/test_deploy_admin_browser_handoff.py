# test-path: cross-cutting — drives deploy/t3 (no src mirror).
"""``deploy/t3`` owns the last two steps of ``t3 admin`` / ``t3 dashboard``.

The CLI runs inside the stack. The image carries no browser and no ``$BROWSER``, and the
CLI's own ``127.0.0.1`` is the CONTAINER's — so it can neither open the operator's browser
nor prove the HOST url it published actually answers. This wrapper is the only layer that
executes on the host, which is why both steps live there.

Two properties are pinned. The url is READ from the file the CLI wrote rather than rebuilt
in bash, because a second copy of the port is free to disagree with ``--port`` — the same
unreachable-url defect moved one layer out. And an url that does not answer is REFUSED, so
the operator gets the cause instead of ERR_CONNECTION_REFUSED.
"""

from pathlib import Path

import pytest

from teatree.cli.admin import BROWSE_URL_FILE

_WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"


@pytest.fixture(scope="module")
def wrapper() -> str:
    return _WRAPPER.read_text(encoding="utf-8")


def _body(wrapper: str, signature: str) -> str:
    """The named shell function's body — closed on the column-0 ``}``, never the first one.

    A parameter expansion (``${1:-}``) carries a brace of its own, so splitting on any
    ``}`` truncates the body to nothing and the assertion below passes vacuously.
    """
    return wrapper.split(signature, 1)[1].split("\n}\n", 1)[0]


class TestTheBrowserHandoffIsSingleSourced:
    def test_the_wrapper_reads_the_file_the_cli_writes(self, wrapper: str) -> None:
        # The one coordinate both sides must agree on. `teatree.cli.admin` writes it under
        # `data_dir_root()`, which is bind-mounted, so the host reads the same bytes.
        assert BROWSE_URL_FILE in wrapper

    def test_the_wrapper_never_rebuilds_the_url_from_its_own_port(self, wrapper: str) -> None:
        # A hardcoded port here would silently ignore --port and open a dead page.
        handoff = wrapper.split("wants_host_browser()", 1)[1]
        assert "8803" not in handoff
        assert "8000" not in handoff


class TestOnlyTheTwoBrowserVerbsTakeTheHostHop:
    @pytest.mark.parametrize("verb", ["admin", "dashboard"])
    def test_the_page_verbs_are_routed_to_the_host(self, wrapper: str, verb: str) -> None:
        assert verb in _body(wrapper, "wants_host_browser()")

    def test_no_browser_opts_out_of_the_host_hop(self, wrapper: str) -> None:
        # `--no-browser` is what the admin ROLE passes; it must keep the plain dispatch.
        assert "--no-browser" in _body(wrapper, "wants_host_browser()")


class TestAnUnreachableUrlIsRefused:
    def test_the_wrapper_probes_before_opening(self, wrapper: str) -> None:
        hop = _body(wrapper, "dispatch_then_open_browser()")
        assert "curl" in hop
        assert "refuse_unreachable_admin" in hop

    def test_the_refusal_names_what_to_check(self, wrapper: str) -> None:
        refusal = _body(wrapper, "refuse_unreachable_admin()")
        assert "teatree-admin-forward" in refusal
        assert "teatree-admin" in refusal
