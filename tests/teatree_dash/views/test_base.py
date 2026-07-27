"""The dashboard shell every page extends — identity, landmarks, and the morph config.

Three properties that belong to the shell rather than to any one page: which BOX the
operator is looking at, whether a keyboard user can reach the content, and whether a
polled morph swap is allowed to overwrite what someone is typing.
"""

import socket

from django.test import TestCase
from django.urls import reverse

from teatree.core.models import ConfigSetting
from teatree.dash.views.base import instance_label, nav_context


class InstanceLabelTestCase(TestCase):
    """The header names THIS box — teatree runs on several and they look identical."""

    def test_the_configured_label_is_used(self) -> None:
        ConfigSetting.objects.set_value("dashboard_instance_label", "laptop")
        assert instance_label() == "laptop"

    def test_an_unset_label_falls_back_to_the_hostname(self) -> None:
        """The shipped default is empty — a machine name cannot be a shipped constant."""
        assert instance_label() == socket.gethostname()

    def test_the_label_reaches_every_page_through_the_nav_context(self) -> None:
        ConfigSetting.objects.set_value("dashboard_instance_label", "build-box")
        assert nav_context("dash:board")["instance_label"] == "build-box"

    def test_the_rendered_header_shows_the_label(self) -> None:
        ConfigSetting.objects.set_value("dashboard_instance_label", "build-box")
        response = self.client.get(reverse("dash:board"))
        assert b"build-box" in response.content


class ShellAccessibilityTestCase(TestCase):
    """Landmarks a screen-reader and keyboard user need before any page content."""

    PAGES = ("dash:board", "dash:health", "dash:loops", "dash:presets", "dash:config", "dash:settings")

    def test_every_page_has_exactly_one_h1(self) -> None:
        for name in self.PAGES:
            body = self.client.get(reverse(name)).content.decode()
            assert body.count("<h1") == 1, f"{name} has {body.count('<h1')} <h1> elements, expected exactly 1"

    def test_every_page_offers_a_skip_link_to_the_main_content(self) -> None:
        for name in self.PAGES:
            body = self.client.get(reverse(name)).content.decode()
            assert 'href="#dash-main"' in body, f"{name} has no skip link"
            assert 'id="dash-main"' in body, f"{name} has no #dash-main target"

    def test_the_nav_is_a_labelled_landmark(self) -> None:
        body = self.client.get(reverse("dash:board")).content.decode()
        assert 'aria-label="Dashboard sections"' in body


class MorphConfigTestCase(TestCase):
    """A polled surface must not overwrite the input the operator is typing into.

    Idiomorph assigns the DOM ``value`` PROPERTY during a morph, which bypasses the
    dirty-value flag, and its ``ignoreActiveValue`` default is falsy — so a focused
    field inside a polled region is reset on every tick unless the default is flipped.
    """

    def test_the_shell_opts_every_morph_swap_out_of_clobbering_the_focused_field(self) -> None:
        body = self.client.get(reverse("dash:loops")).content.decode()
        assert "Idiomorph.defaults.ignoreActiveValue = true" in body
