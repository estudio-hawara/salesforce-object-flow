"""Welcome page — first thing the user sees when no connections exist."""

from importlib.metadata import PackageNotFoundError, version
from typing import ClassVar

from gi.repository import Adw, Gtk

from salesforce_object_flow.i18n import N_, _
from salesforce_object_flow.pages.groups import PageGroup
from salesforce_object_flow.ui.layout import make_page_layout


def _app_version() -> str:
    try:
        return version("salesforce-object-flow")
    except PackageNotFoundError:
        return "dev"


class WelcomePage:
    """Landing page: brand, current alpha status, and a pointer to Connections."""

    NAME: ClassVar[str] = "welcome"
    TITLE: ClassVar[str] = N_("Welcome")
    ICON_NAME: ClassVar[str] = "hand-openyay-symbolic"
    GROUP: ClassVar[PageGroup] = PageGroup.SETUP

    def build(self, header: Adw.HeaderBar | None = None) -> Adw.ToolbarView:
        toolbar_view, _page_box, content_box, _scrolled = make_page_layout(header)

        intro = Adw.PreferencesGroup()
        intro.set_title("Salesforce Object Flow")
        intro.set_description(
            _(
                "Compose multi-object, transactional creates against the Salesforce REST"
                " Composite API."
            )
        )
        content_box.append(intro)

        status = Adw.PreferencesGroup()
        status.set_title(_("Status"))
        status.set_description(
            _("Version {version} — alpha. Feedback and bug reports welcome.").format(
                version=_app_version()
            )
        )

        next_row = Adw.ActionRow()
        next_row.set_title(_("Get started"))
        next_row.set_subtitle(
            _(
                "Open Connections to register your first Salesforce org. Then build a "
                "File Format that describes your CSV and a Composite Request template "
                "that maps each row to one or more API calls."
            )
        )
        status.add(next_row)
        content_box.append(status)

        # Reserve a docs link for the future. Hidden until we have a real URL.
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        content_box.append(spacer)

        return toolbar_view
