"""Command line interface.

`astrodoro <command>` groups everything that does not need a window: building
calibration masters, running a headless stacking session, replaying a recorded
one, and probing the camera.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .. import __version__
from ..i18n import set_language
from ..settings import Settings


def build_parser() -> argparse.ArgumentParser:
    from . import probe, stack

    p = argparse.ArgumentParser(
        prog="astrodoro",
        description="Capture and live stacking for electronically assisted "
                    "astronomy.")
    p.add_argument("--version", action="version",
                   version=f"astrodoro {__version__}")
    p.add_argument("--lang", default=None,
                   help="interface language (e.g. en, pt_BR); overrides settings")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    stack.add_parsers(sub)
    probe.add_parsers(sub)
    _add_settings_parser(sub)
    _add_catalog_parser(sub)
    return p


def _add_settings_parser(sub) -> None:
    sp = sub.add_parser("settings", help="show or change stored settings")
    sp.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"), action="append",
                    help="assign a setting, e.g. --set capture_dir ~/captures")
    sp.set_defaults(fn=cmd_settings)


#: OpenNGC, complete NGC + IC, ~14 thousand objects. CC BY-SA 4.0, Mattia Verga.
CATALOG_URL = ("https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/"
               "database_files/NGC.csv")


def _add_catalog_parser(sub) -> None:
    sp = sub.add_parser("catalog",
                        help="download the deep-sky object catalogue")
    sp.add_argument("--out", default=None,
                    help="where to write it (default: the path the program "
                         "looks in)")
    sp.add_argument("--force", action="store_true",
                    help="download again even if the file already exists")
    sp.set_defaults(fn=cmd_catalog)


def cmd_catalog(args) -> None:
    """Fetch OpenNGC to wherever `Settings.catalog_path()` looks for it.

    A command rather than a shell script so it works the same from a checkout
    and from an installed copy, and so the "not found" error can name something
    the user can actually run. Uses urllib, so it needs no curl.
    """
    import urllib.request

    dest = (Path(args.out).expanduser() if args.out
            else Settings.load().catalog_path())
    if dest.exists() and not args.force:
        rows = sum(1 for _line in dest.open(encoding="utf-8", errors="replace"))
        print(f"already present: {dest} ({rows} lines)")
        print("use --force to download it again")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Download to a sibling first: interrupted halfway, a partial CSV would
    # parse into a silently truncated catalogue.
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"downloading {CATALOG_URL}")
    try:
        with urllib.request.urlopen(CATALOG_URL, timeout=60) as r, \
                tmp.open("wb") as out:
            shutil.copyfileobj(r, out)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    rows = sum(1 for _line in dest.open(encoding="utf-8", errors="replace"))
    print(f"{dest}  ({rows} lines, {dest.stat().st_size/1e6:.1f} MB)")


def cmd_settings(args) -> None:
    """Show the settings file, or change one value from the shell.

    The GUI has the same controls, but a headless machine has no GUI, and
    pointing the capture folder at an external disk is exactly the kind of thing
    you want to script.
    """
    from dataclasses import fields

    s = Settings.load()
    known = {f.name: f.type for f in fields(Settings)
             if not f.name.startswith("_")}
    if args.set:
        for key, value in args.set:
            if key not in known:
                sys.exit(f"unknown setting: {key}\n"
                         f"available: {', '.join(sorted(known))}")
            current = getattr(s, key)
            try:
                if isinstance(current, bool):
                    parsed = str(value).strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(current, int):
                    parsed = int(value)
                elif isinstance(current, float):
                    parsed = float(value)
                else:
                    parsed = value
            except ValueError:
                sys.exit(f"{key}: {value!r} is not a valid value")
            setattr(s, key, parsed)
        path = s.save()
        print(f"saved to {path}")
    for key in sorted(known):
        print(f"{key:20} {getattr(s, key)}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    set_language(args.lang or Settings.load().language)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, OSError, ValueError) as e:
        # An expected failure — no camera, an unreadable folder, an out-of-range
        # value — deserves one line, not a traceback. Anything else is a bug and
        # keeps its traceback.
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
