"""Placeholder welcome page shown until the Composite API form lands."""

from typing import ClassVar

from gi.repository import Adw, Gtk

from salesforce_object_flow.ui.layout import make_page_layout


class WelcomePage:
    """Trivial first page: explain what the app is and what's coming."""

    TITLE: ClassVar[str] = "Welcome"

    def build(self, header: Adw.HeaderBar | None = None) -> Adw.ToolbarView:
        toolbar_view, _page_box, content_box, _scrolled = make_page_layout(header)

        intro = Adw.PreferencesGroup()
        intro.set_title("Salesforce Object Flow")
        intro.set_description(
            "Compose multi-object, transactional creates against the Salesforce REST Composite API."
        )
        content_box.append(intro)

        status = Adw.PreferencesGroup()
        status.set_title("Status")
        status.set_description("Version 0.0.1 — project scaffolding only.")

        next_row = Adw.ActionRow()
        next_row.set_title("What's next")
        next_row.set_subtitle(
            "The Composite API form lands in a follow-up release. For now, this "
            "window confirms that the GTK4 + libadwaita stack is wired up correctly."
        )
        status.add(next_row)
        content_box.append(status)

        # Reserve a docs link for the future. Hidden until we have a real URL.
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        content_box.append(spacer)

        return toolbar_view
