"""The /dash/ e2e lane runs against an externally-provided chromium when one is named.

Playwright ships browser builds per distro and refuses to install on a platform it has
no build for, which makes the lane unrunnable on such a host even when a working
chromium is installed. `E2E_CHROMIUM_EXECUTABLE` points the lane at that binary. CI
leaves it unset and must keep launching exactly as before.
"""

from e2e.dash.browser_launch import browser_launch_overrides


class TestBrowserLaunchOverrides:
    def test_no_overrides_when_no_executable_is_named(self) -> None:
        assert browser_launch_overrides(None) == {}

    def test_an_empty_value_is_treated_as_unset(self) -> None:
        assert browser_launch_overrides("") == {}

    def test_a_named_executable_is_launched_instead_of_playwrights_own(self) -> None:
        overrides = browser_launch_overrides("/usr/bin/chromium")

        assert overrides["executable_path"] == "/usr/bin/chromium"

    def test_a_named_executable_disables_the_sandbox_it_cannot_provide(self) -> None:
        """A distro chromium has no SUID helper at the path Playwright's build uses."""
        overrides = browser_launch_overrides("/usr/bin/chromium")

        assert "--no-sandbox" in overrides["args"]
