"""Sidebar group identity.

Each member's value is the displayed label, wrapped with ``N_()`` so
``pybabel extract`` picks it up. Resolve to a translated string with
``_(group.value)`` at use-site (see ``window._sidebar_header_func``).

Lives in its own module so page implementations can import ``PageGroup``
without a circular dependency on ``window``.
"""

from __future__ import annotations

from enum import Enum

from salesforce_object_flow.i18n import N_


class PageGroup(Enum):
    SETUP = N_("Setup")
    DATA_MODEL = N_("Data model")
    RUN = N_("Run")
