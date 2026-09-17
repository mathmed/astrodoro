"""Message catalogues, over Babel.

    python scripts/i18n.py extract    # code -> astrodoro.pot
    python scripts/i18n.py update     # .pot -> every .po, keeping translations
    python scripts/i18n.py compile    # .po  -> .mo, what gettext actually reads
    python scripts/i18n.py check      # fail if the .pot is behind the code
    python scripts/i18n.py missing pt_BR

Babel does the work; this only drives it, and skips one check. Babel decides
`python-format` from the content of a message, so "peak at {pct:.0f}% of scale"
is flagged as printf *as well as* brace style, and its printf checker then
rejects the translation because `% o` looks like a `%o` placeholder. The flag
cannot be turned off in the file — `Message.__init__` recomputes it on every
read — so `compile` here runs the plural check and not that one. This project
has no printf strings at all: `CLAUDE.md` requires named `{placeholders}`.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from pathlib import Path

from babel.messages.checkers import num_plurals
from babel.messages.frontend import CommandLineInterface
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "astrodoro"
I18N = SOURCE / "i18n"
POT = I18N / "astrodoro.pot"
LOCALE = I18N / "locale"
DOMAIN = "astrodoro"
CONFIG = ROOT / "babel.cfg"


def _babel(*args: str) -> int:
    # From the project root, so the `#:` references land relative — an absolute
    # path would be one developer's checkout, baked into a committed file.
    import os

    here = os.getcwd()
    os.chdir(ROOT)
    try:
        return CommandLineInterface().run(["pybabel", *args]) or 0
    finally:
        os.chdir(here)


def extract(out: Path = POT) -> int:
    return _babel(
        "extract",
        "-F",
        str(CONFIG),
        "-o",
        str(out),
        "--project=astrodoro",
        "--copyright-holder=Mateus Medeiros",
        "--msgid-bugs-address=https://github.com/mathmed/astrodoro/issues",
        ".",
    )


def update() -> int:
    return _babel("update", "-i", str(POT), "-d", str(LOCALE), "-D", DOMAIN)


def compile_() -> int:
    rc = 0
    for po in sorted(LOCALE.glob(f"*/LC_MESSAGES/{DOMAIN}.po")):
        tag = po.parent.parent.name
        catalog = read_po(po.open(encoding="utf-8"), locale=tag, domain=DOMAIN)
        for message in catalog:
            if not message.id:
                continue
            try:
                num_plurals(catalog, message)
            except Exception as e:
                print(f"error: {po}: {str(message.id)[:50]!r}: {e}")
                rc = 1
        mo = po.with_suffix(".mo")
        with mo.open("wb") as fh:
            write_mo(fh, catalog)
        total = sum(1 for m in catalog if m.id and m.string)
        print(f"{tag}: {total} messages -> {mo}")
    return rc


def _ids(path: Path) -> set:
    return {m.id for m in read_po(path.open(encoding="utf-8")) if m.id}


def check() -> int:
    """Whether the committed .pot still matches the code.

    Only the messages are compared, never the file: Babel stamps the header
    with the current time, so two runs are never byte-identical.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "fresh.pot"
        # Babel narrates every file it walks; this one is a yes/no answer.
        with contextlib.redirect_stderr(io.StringIO()):
            extract(fresh)
        now, committed = _ids(fresh), _ids(POT)
    if now == committed:
        print(f"{POT} is up to date ({len(now)} messages)")
        return 0
    for message in sorted(now - committed, key=str)[:20]:
        print(f"  missing from the .pot: {str(message)[:70]!r}")
    for message in sorted(committed - now, key=str)[:20]:
        print(f"  no longer in the code: {str(message)[:70]!r}")
    print(f"\n{POT} is out of date — run: make i18n")
    return 1


def missing(tag: str) -> int:
    po = LOCALE / tag / "LC_MESSAGES" / f"{DOMAIN}.po"
    if not po.exists():
        print(f"no catalogue for {tag}")
        return 1
    catalog = read_po(po.open(encoding="utf-8"))
    todo = [m for m in catalog if m.id and not m.string]
    fuzzy = [m for m in catalog if m.id and m.fuzzy]
    for message in todo[:20]:
        print(f"  untranslated: {str(message.id)[:70]!r}")
    for message in fuzzy[:20]:
        print(f"  needs review: {str(message.id)[:70]!r}")
    total = sum(1 for m in catalog if m.id)
    print(f"\n{total - len(todo)}/{total} translated, {len(fuzzy)} to review")
    return 1 if todo or fuzzy else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("extract", "update", "compile", "check"):
        sub.add_parser(name)
    sp = sub.add_parser("missing")
    sp.add_argument("tag")
    args = ap.parse_args()
    if args.command == "missing":
        return missing(args.tag)
    return {"extract": extract, "update": update, "compile": compile_, "check": check}[
        args.command
    ]()


if __name__ == "__main__":
    sys.exit(main())
