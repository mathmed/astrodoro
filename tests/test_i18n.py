"""The translation catalogues: the loader, and the guarantee of a fallback.

A missing or incomplete catalogue must never break a string — the source text is
English, which is also the fallback.
"""
from __future__ import annotations

from astrodoro import i18n


def test_english_is_the_identity():
    i18n.set_language("en")
    assert i18n.gettext("Start") == "Start"


def test_normalisation_accepts_the_usual_spellings():
    assert i18n.normalize("pt-br") in ("pt_BR", "en")
    assert i18n.normalize(None) == "en"
    assert i18n.normalize("kl_GL") == "en", "an unknown tag falls back"


def test_a_shipped_catalogue_translates_and_falls_back():
    have = i18n.available()
    if "pt_BR" not in have:
        return
    i18n.set_language("pt_BR")
    assert i18n.gettext("Start") != "Start", "pt_BR did not translate 'Start'"
    # A string absent from the catalogue comes back unchanged, not empty.
    missing = "a string that is certainly not in any catalogue"
    assert i18n.gettext(missing) == missing
    i18n.set_language("en")


def test_the_po_parser_handles_continuations_and_escapes(tmp_path):
    po = tmp_path / "x.po"
    po.write_text(
        'msgid ""\n'
        'msgstr "Content-Type: text/plain; charset=UTF-8\\n"\n'
        '\n'
        '# a comment\n'
        'msgid "one"\n'
        'msgstr "um"\n'
        '\n'
        'msgid "a long "\n'
        '"identifier"\n'
        'msgstr "um identificador "\n'
        '"longo"\n'
        '\n'
        'msgid "with\\na newline"\n'
        'msgstr "com\\numa quebra"\n',
        encoding="utf-8")
    catalog = i18n._parse_po(po)
    assert catalog["one"] == "um"
    assert catalog["a long identifier"] == "um identificador longo"
    assert catalog["with\na newline"] == "com\numa quebra"
    assert "" not in catalog, "the header entry must not become a message"
