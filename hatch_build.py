"""Compile the message catalogues into the wheel.

The `.po` files are committed and the `.mo` files are not, so the wheel has to
make its own. Without this the installed program would fall back to parsing the
`.po` at runtime, which works but is not what `gettext` is for.
"""
from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

LOCALE = Path("src/astrodoro/i18n/locale")
DOMAIN = "astrodoro"


class CatalogHook(BuildHookInterface):
    PLUGIN_NAME = "catalogs"

    def initialize(self, version, build_data):
        from babel.messages.mofile import write_mo
        from babel.messages.pofile import read_po

        for po in sorted(LOCALE.glob(f"*/LC_MESSAGES/{DOMAIN}.po")):
            tag = po.parent.parent.name
            with po.open(encoding="utf-8") as fh:
                catalog = read_po(fh, locale=tag, domain=DOMAIN)
            mo = po.with_suffix(".mo")
            with mo.open("wb") as fh:
                write_mo(fh, catalog)
            build_data["artifacts"].append(f"/{mo.as_posix()}")
