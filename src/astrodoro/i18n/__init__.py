"""Translation catalogs for both the GUI and the CLI.

Catalogs live in `locale/<lang>/LC_MESSAGES/astrodoro.po` and are read
directly: `.po` files are plain text, so a translator can edit one and see the
result on the next run with no build step and no system `gettext`. A compiled
`.mo` next to the `.po` wins when present, for anyone who prefers the standard
toolchain.

Source strings are English, which is also the fallback when a catalog is
missing or incomplete.
"""
from __future__ import annotations

import gettext as _gettext
import os
import re
from pathlib import Path

DOMAIN = "astrodoro"
LOCALE_DIR = Path(__file__).resolve().parent / "locale"
DEFAULT_LANGUAGE = "en"

# Language tag -> name written in that language, for the settings menu.
LANGUAGES = {"en": "English", "pt_BR": "Português (Brasil)"}

_current = DEFAULT_LANGUAGE
_translation: _gettext.NullTranslations = _gettext.NullTranslations()


def available() -> dict[str, str]:
    """Languages with a catalog on disk, plus the untranslated source language."""
    found = {DEFAULT_LANGUAGE: LANGUAGES[DEFAULT_LANGUAGE]}
    for path in sorted(LOCALE_DIR.glob("*/LC_MESSAGES/" + DOMAIN + ".*")):
        if path.suffix not in (".po", ".mo"):
            continue
        tag = path.parent.parent.name
        found[tag] = LANGUAGES.get(tag, tag)
    return found


def normalize(tag: str | None) -> str:
    """Best available match for a language tag: `pt-br`, `pt_BR`, `pt` all work."""
    if not tag:
        return DEFAULT_LANGUAGE
    want = tag.replace("-", "_")
    have = available()
    for candidate in (want, want.lower(), want.split("_")[0]):
        for tag_have in have:
            if tag_have.lower() == candidate.lower():
                return tag_have
        for tag_have in have:
            if tag_have.lower().startswith(candidate.lower() + "_"):
                return tag_have
    return DEFAULT_LANGUAGE


def set_language(tag: str | None) -> str:
    """Install a catalog process-wide. Returns the tag actually used."""
    global _current, _translation
    _current = normalize(tag)
    _translation = _load(_current)
    return _current


def language() -> str:
    return _current


def gettext(message: str) -> str:
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)


#: Short alias, the conventional name for the lookup function.
_ = gettext


def N_(message: str) -> str:
    """Mark a string for translation without translating it yet.

    For text defined in a module-level table and translated at display time. The
    extractor sees the literal here; `_()` does the lookup at the point of use.
    Returning the string unchanged means the value still works as a dict key or
    an identifier.
    """
    return message


def _load(tag: str) -> _gettext.NullTranslations:
    if tag == DEFAULT_LANGUAGE:
        return _gettext.NullTranslations()
    base = LOCALE_DIR / tag / "LC_MESSAGES" / DOMAIN
    mo = base.with_suffix(".mo")
    if mo.exists():
        with mo.open("rb") as fh:
            return _gettext.GNUTranslations(fh)
    po = base.with_suffix(".po")
    if po.exists():
        return _PoTranslations(po)
    return _gettext.NullTranslations()


class _PoTranslations(_gettext.NullTranslations):
    """A `.po` file read at runtime, with the same interface as `GNUTranslations`."""

    def __init__(self, path: Path):
        super().__init__()
        self._catalog = _parse_po(path)

    def gettext(self, message: str) -> str:
        entry = self._catalog.get(message)
        if isinstance(entry, str):
            return entry or message
        if isinstance(entry, list) and entry and entry[0]:
            return entry[0]
        return message

    def ngettext(self, singular: str, plural: str, n: int) -> str:
        entry = self._catalog.get(singular)
        # Only the two-form plural rule is supported here, which covers every
        # language currently shipped. A catalog needing more forms should be
        # compiled to `.mo`, where the real plural expression is honoured.
        if isinstance(entry, list) and len(entry) >= 2:
            chosen = entry[0] if n == 1 else entry[1]
            if chosen:
                return chosen
        return singular if n == 1 else plural


_TOKEN = re.compile(r'^(msgid|msgid_plural|msgstr)(?:\[(\d+)\])?\s+(.*)$')
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        return ""
    out, i, body = [], 0, text[1:-1]
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            out.append(_ESCAPES.get(body[i + 1], body[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _parse_po(path: Path) -> dict[str, str | list[str]]:
    """msgid -> msgstr, or msgid -> [form0, form1, ...] for plurals."""
    catalog: dict[str, str | list[str]] = {}
    msgid = ""
    plurals: list[str] = []
    singular = ""
    field: str | None = None

    def flush() -> None:
        if not msgid:
            return
        catalog[msgid] = list(plurals) if plurals else singular

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _TOKEN.match(line)
        if m:
            keyword, index, rest = m.group(1), m.group(2), m.group(3)
            if keyword == "msgid":
                flush()
                msgid, singular, plurals, field = _unquote(rest), "", [], "msgid"
            elif keyword == "msgid_plural":
                field = "msgid_plural"
            else:
                slot = int(index or 0)
                value = _unquote(rest)
                if index is None:
                    singular, field = value, "msgstr"
                else:
                    while len(plurals) <= slot:
                        plurals.append("")
                    plurals[slot] = value
                    field = f"msgstr[{slot}]"
            continue
        # Continuation line: a bare quoted string appends to the last field.
        chunk = _unquote(line)
        if field == "msgid":
            msgid += chunk
        elif field == "msgstr":
            singular += chunk
        elif field and field.startswith("msgstr["):
            plurals[int(field[7:-1])] += chunk
    flush()
    catalog.pop("", None)
    return catalog


set_language(os.environ.get("ASTRODORO_LANG"))
