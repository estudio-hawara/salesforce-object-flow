"""Reusable page layout helpers.

Ported from Hyprmod's ``ui/__init__.py:make_page_layout``.
"""

from gi.repository import Adw, Gtk


def make_page_layout(
    header: Adw.HeaderBar | None = None,
    spacing: int = 24,
) -> tuple[Adw.ToolbarView, Gtk.Box, Gtk.Box, Gtk.ScrolledWindow]:
    """Standard page layout: toolbar + scrollable clamped content.

    Returns ``(toolbar_view, page_box, content_box, scrolled)``. Insert
    banners or extra bars into ``page_box`` before the scrolled window with
    ``page_box.prepend(...)``.
    """
    toolbar_view = Adw.ToolbarView()
    toolbar_view.add_top_bar(header or Adw.HeaderBar())

    page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_vexpand(True)

    clamp = Adw.Clamp()
    clamp.set_maximum_size(800)
    clamp.set_tightening_threshold(600)

    content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    content_box.set_margin_top(24)
    content_box.set_margin_bottom(24)
    content_box.set_margin_start(12)
    content_box.set_margin_end(12)
    content_box.set_spacing(spacing)

    clamp.set_child(content_box)
    scrolled.set_child(clamp)
    page_box.append(scrolled)
    toolbar_view.set_content(page_box)
    return toolbar_view, page_box, content_box, scrolled


def clear_children(container: Gtk.Widget) -> None:
    """Remove all children from a GTK container widget."""
    while child := container.get_first_child():
        container.remove(child)  # type: ignore[attr-defined]
