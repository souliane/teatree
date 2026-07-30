"""The dashboard shell every page extends — identity, landmarks, and the morph config.

Three properties that belong to the shell rather than to any one page: which BOX the
operator is looking at, whether a keyboard user can reach the content, and whether a
polled morph swap is allowed to overwrite what someone is typing.
"""

import socket
from html.parser import HTMLParser

from django.test import TestCase
from django.urls import reverse

from teatree.core.models import ConfigSetting, Loop, Mode, ModeSchedule
from teatree.dash.views.base import instance_label, nav_context
from tests.factories import TicketFactory


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

    PAGES = ("dash:board", "dash:health", "dash:loops", "dash:presets", "dash:settings")

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
    dirty-value flag, so a focused field inside a polled region is reset on every tick.

    ``ignoreActiveValue`` is the wrong instrument for that: the vendored bundle also
    consults it to skip morphing the active element's CHILDREN, so with it on, the
    label of the button just clicked never updates — pause stays "pause" after its own
    swap. The guard must name the value attribute of a focused text field, nothing else.
    """

    def test_the_shell_guards_a_focused_fields_value_and_nothing_else(self) -> None:
        body = self.client.get(reverse("dash:loops")).content.decode()
        assert "beforeAttributeUpdated" in body
        assert 'name === "value"' in body
        assert "Idiomorph.defaults.ignoreActiveValue" not in body


class HtmxFormSubmitterTestCase(TestCase):
    """An ``hx-post`` form must not depend on WHICH submit button was clicked.

    The vendored htmx (``htmx-2.0.4.min.js``) has no ``submitter`` support — grep the
    bundle and the word does not appear, and it is a native DOM property name a
    minifier never renames. A native form POST sends only the clicked button's
    ``name``/``value``; htmx serializes the form's fields and drops it. So a form
    carrying two ``<button name="action">`` submitters silently posts NO action once
    ``hx-post`` is added, and the view refuses every click.

    The Django test client posts a dict directly, so it can never reproduce that —
    which is why this asserts the MARKUP rather than the round-trip. Carry the value
    in a hidden input instead, one form per action.
    """

    PAGES = ("dash:board", "dash:health", "dash:loops", "dash:presets", "dash:settings")

    def setUp(self) -> None:
        Loop.objects.create(name="submitter-probe", delay_seconds=60, script="run.py", enabled=True)
        Mode.objects.get_or_create(name="engaged", defaults={"entries": {}})
        ModeSchedule.objects.get_or_create(name="weekly")

    def test_no_htmx_form_carries_a_named_submit_button(self) -> None:
        offenders: list[str] = []
        for name in self.PAGES:
            body = self.client.get(reverse(name)).content.decode()
            offenders.extend(f"{name}: {button}" for button in _named_submitters_in_htmx_forms(body))
        drawer = self.client.get(reverse("dash:ticket_drawer", args=[TicketFactory().pk])).content.decode()
        offenders.extend(f"drawer: {button}" for button in _named_submitters_in_htmx_forms(drawer))
        assert not offenders, (
            "the vendored htmx drops the clicked submitter, so these buttons post nothing:\n" + "\n".join(offenders)
        )

    def test_the_probe_detects_a_planted_violation(self) -> None:
        planted = '<form hx-post="/x"><button name="action" value="pause">pause</button></form>'
        assert _named_submitters_in_htmx_forms(planted) == ['button name="action" value="pause"']


def _named_submitters_in_htmx_forms(html_text: str) -> list[str]:
    """Every named submit button sitting inside a form wired with ``hx-post``."""

    class _Finder(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.in_htmx_form = False
            self.found: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            attributes = dict(attrs)
            if tag == "form":
                self.in_htmx_form = "hx-post" in attributes
            elif tag == "button" and self.in_htmx_form and attributes.get("name"):
                self.found.append(f'button name="{attributes["name"]}" value="{attributes.get("value", "")}"')

        def handle_endtag(self, tag: str) -> None:
            if tag == "form":
                self.in_htmx_form = False

    finder = _Finder()
    finder.feed(html_text)
    return finder.found
