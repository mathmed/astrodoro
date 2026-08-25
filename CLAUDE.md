# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

Astrodoro is capture + live stacking for EAA on macOS (arm64), SVBONY SV405CC
camera, aimed at a Dobsonian on an equatorial platform — no goto, no autoguiding.

## Language

- **All source is English**: identifiers, comments, docstrings, log messages,
  documentation. This was a deliberate migration; do not reintroduce Portuguese
  into the source.
- **User-visible strings go through `_()`** from `astrodoro.i18n` and are
  translated in `src/astrodoro/i18n/locale/<lang>/LC_MESSAGES/astrodoro.po`.
  After adding one, run `make i18n`; `make lint` fails on a stale catalogue.
- **Talk to the user in Portuguese.**

## Read these first

Most of what used to live in this file now lives where contributors will find it:

- **[CONTRIBUTING.md](CONTRIBUTING.md)** — repository layout, the invariants that
  cross files, how to run tests, how translations work. Read the invariants
  section before changing anything in `core/`.
- **[docs/design-notes.md](docs/design-notes.md)** — the measured reasoning
  behind the non-obvious decisions, and the reverted attempts. Consult it before
  "simplifying" the stacker, the alignment or the sky map.
- **[docs/hardware.md](docs/hardware.md)** — nine measured camera traps. Read
  before touching `drivers/` or calibration.

## Commands

`make` with no target lists everything. The main ones:

```bash
make setup      # uv sync --extra dev + make sdk + make catalog
make gui        # open the interface
make test       # pytest
make lint       # ruff + stale-catalogue check
make i18n       # refresh the .pot, report what pt_BR lacks
make replay FOLDER=~/Astrodoro/sessions/2026-08-18/2130_M8
make handset    # fake phone, feeds the sensor with no hardware
```

Capture-target variables: `EXP GAIN BIN FRAMES TEMP DARK OUT FOLDER`.

The interpreter is always `.venv/bin/python` (the Makefile calls it `PY`), never
the system Python — the vendor dylib and the native extensions are bound to that
venv.

## Working without hardware

`ReplaySource` (`core/source.py`) replays the FITS subs of a recorded session
honouring each frame's metadata, so the pipeline behaves as it did on the night
of capture. `scripts/fake_handset.py` does the same for the phone sensor,
crooked mount included. **Prefer both to mocks** — they exercise the real code.

In the GUI the replay has a speed control (default 4x) and a loop toggle, and the
integrate button's state is applied in `start()`. Without that the session ran as
fast as it could and finished before any frame was stacked.

## Architecture in one paragraph

Two front ends over one core. `astrodoro.ui` is the Qt window plus
`ui/worker.py` (`CaptureWorker`, the capture/processing thread that emits Qt
signals); `astrodoro.cli` is the headless commands. Both call `astrodoro.core`,
whose per-frame pipeline is `FrameSource` → `calibration.calibrate` →
debayer/luminance → `stars.detect` → `register.estimate` → `LiveStacker.add` →
`stretch` for display → `Recorder`. `astrodoro.drivers.svbony.sdk` is a 1:1
ctypes binding; `drivers.svbony.camera` is the Pythonic layer that neutralises
the camera's quirks inside `open()`. **Nothing above `drivers/` may talk to the
SDK directly**, and nothing in `core/`, `pointing/` or `drivers/` may import
`ui/`.

Calibration lives in exactly one function (`core/calibration.py`) because it used
to be duplicated between the GUI and the CLI and the order is not optional.

## User settings

`astrodoro.settings.Settings` is a JSON file outside the repository, shared by
the GUI and the CLI: capture/dark/flat/export folders, observing site, optics,
language, display preferences, capture defaults. Nothing is hardcoded any more —
if you need a new tunable value, add a field there rather than a constant.

## Conventions

Comments explain **why**, with the measured number when one exists ("bin1 and
bin2 take the same time, ~524 ms"), and record reverted attempts so they are not
repeated. When you change measured behaviour, **measure it again** and update the
number in the comment, in the README and in `docs/` — all three cite concrete
values.

Long design rationale belongs in `docs/design-notes.md`, with a one-line pointer
from the code. Keep the modules readable.
