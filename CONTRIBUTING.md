# Contributing to Astrodoro

This grew out of one telescope, one camera and one back garden, so the most
useful contributions are usually **measurements from different hardware** and
the bugs they expose.

## Getting set up

```bash
git clone https://github.com/mathmed/astrodoro
cd astrodoro
uv sync --extra dev && uv pip install -e .

make test     # pytest
make lint     # ruff, vulture, bandit, xenon, mypy, i18n check — what CI runs
make fmt      # the fixes ruff can make itself
make hooks    # run the checks before each commit
```

The interpreter is always `.venv/bin/python` (`PY` in the Makefile), never the
system one: the vendor dylib and the native extensions are bound to that venv.

**You do not need the camera.** `make sdk` only matters for live capture; the
whole suite and most of the interface run without it.

## Ground rules

- **The source is English** — identifiers, comments, log messages — and carries
  **no comments and no docstrings**. The reasoning lives in the invariants
  below and in the tests named after them. The only text left in the source is
  what a tool reads: `# noqa`, `# type: ignore`.
- **User-visible strings go through `_()`** (`N_()` when deferred) with named
  `{placeholders}`, and `make i18n` after.
- **If you change measured behaviour, measure it again** and update the number
  where it is written.
- **Prefer a replay session or the fake handset to a mock.** Both exercise the
  real code path.
- **Qt enums use the Qt6 spelling** — `Qt.GlobalColor.transparent`, not
  `Qt.transparent`. PySide6 accepts both at runtime but types only the first.
- **New tunable values become `Settings` fields**, not module constants.

## Layout

```
src/astrodoro/
├── core/       the frame pipeline; no Qt, no hardware
├── pointing/   where the tube points; Qt only in handset.py
├── drivers/    base.py = what a camera is; svbony/ = one implementation
├── ui/         the window, the capture thread, the design system
├── cli/        the headless commands
└── settings.py JSON dataclass shared by both front ends
```

Layering is one-way: `ui/` and `cli/` may import everything below; nothing below
imports `ui/`. **Nothing above `drivers/` touches an SDK or names a vendor** —
go through `drivers.list_cameras()`, `drivers.camera()`, `drivers.open_camera()`
and take `Bayer`, `ImgType`, `Geometry` and `CameraError` from `drivers.base`.
Adding a manufacturer is a module under `drivers/` plus a line in `_DRIVERS`.

## Invariants that break silently

Each of these is easy to "simplify" back into a bug, and several were. The test
named after each one is where the measurement lives.

- **`register.warp` takes `M` forward** (src → dst), no `WARP_INVERSE_MAP`.
- **The registration reference is the accumulated stack**, not the first frame;
  the first accepted frame locks only the reference *geometry*.
- **Luminance is half resolution.** `cfa_to_luminance` sums the 2x2 quad, so
  coordinates come back via `lum_scale=2.0` and saturation compares against
  `debayer.LUM_SUM` (4.0), never 1.0.
- **A dark and a bias are alternatives, never a sum** — a dark already contains
  the pedestal. A *flat*, being milliseconds long, wants the bias instead.
- **The weight decides acceptance**, not the limits: elongation, halo and FWHM
  are identical safety belts at all three strictness levels.
- **Every verdict is auditable on screen.** A new rejection filter needs a new
  `FrameOutcome.kind` and a new readout, and a new ranking factor needs its own
  line in `Suggestion.factors`, or the numbers become an oracle.
- **`background` and `stretch` never touch the accumulator** — display only.
- **Worker parameters arrive by `request(**kw)` / `flag(name)`**, a dict under a
  lock applied between frames. **Never Qt slots**: the loop sits inside
  `SVBGetVideoData` for the whole exposure, so that thread's event loop is dead.
- **PLANETS is a branch out of the pipeline, not a view of it.** It leaves
  before star detection, never stacks, never autostretches, and measures inside
  a window around the body whose *size* is fixed once. Following the body moves
  the viewport, never the pixels. Grey-world belongs to the Moon alone, and the
  Sun is not in `lucky.BODIES`.
- **The alignment reading never appears without its uncertainty**, and restarts
  whenever the tube moves — a slope fitted across that jump means nothing.
- **Text must not decide the window width.** Use `ElidedLabel` for anything
  whose content changes during the night.
- **CONFIG is a window, not a mode.** Putting it back in `MODES` silently stops
  the stack while it is open.
- **bin2, not bin3/bin4** — those clip highlights, and binning is host-side so
  it buys no speed.

## Tests

Plain pytest, no plugins. The tree mirrors `src/astrodoro/`; `tests/conftest.py`
sets `QT_QPA_PLATFORM=offscreen` before Qt is imported and points the config and
data directories at temp dirs, so nothing touches your real settings.

```bash
.venv/bin/python -m pytest tests/core                       # one layer
.venv/bin/python -m pytest tests/core/test_register_warp.py # one file
.venv/bin/python -m pytest -k smear                         # one topic
```

Every test is named after the thing that would break without it; read the names
before adding one. **The suite must stay hardware-free** — CI runs it on Linux
with no camera and no dylib. A check that needs the camera belongs behind a
command in `astrodoro.cli.probe`, not in `tests/`.

## Translations

Standard gettext through [Babel](https://babel.pocoo.org/), a dev dependency, so
no system `gettext` is needed. `make i18n` extracts into `astrodoro.pot`, merges
into every `.po` keeping what is translated, compiles the `.mo` and reports what
is still missing. `make lint` fails if the `.pot` is behind the code.

A new language is: copy `src/astrodoro/i18n/locale/pt_BR` to your tag, add the
tag to `LANGUAGES` in `src/astrodoro/i18n/__init__.py`, fill in the `msgstr`
lines keeping every `{placeholder}` exactly as it appears, then
`python scripts/i18n.py missing <tag>` until it reports nothing.

## Brand assets

`assets/` and `src/astrodoro/ui/assets/icon.png` are committed images, edited by
hand — there is no generator. The icon carries **two** drawings on purpose: the
engraved plate is unreadable below about 48 px, so small sizes come from the
stroke glyph in `ui/icons.py`. `tests/ui/test_branding.py` fails if either goes
missing.

## Pull requests

- One topic per PR, and say what you measured.
- `make lint` and `make test` pass.
- If you touched an invariant above, say which and why.
