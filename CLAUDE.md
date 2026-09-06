# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Astrodoro captures and live-stacks frames for electronically assisted astronomy
(EAA) on macOS: one Qt window that grabs from an SVBONY SV405CC, calibrates,
detects stars, registers, accumulates, autostretches for display and records the
subs — plus a headless CLI over the same core.

The target setup is a Dobsonian on an equatorial platform: **no goto, no
autoguiding, no plate solving**. A phone strapped to the tube is the only
pointing sensor. Most of the non-obvious code follows from those three
constraints, so before "fixing" something that looks odd, check whether it is
compensating for one of them.

## Setup and commands

`make` with no target prints every shortcut. The interpreter is **always**
`.venv/bin/python` (`PY` in the Makefile) — the vendor dylib and the native
extensions are bound to that venv, so the system Python fails in confusing ways.

```bash
make setup      # uv sync --extra dev, then make sdk, then make catalog
make gui        # python -m astrodoro.ui
make test       # pytest (testpaths=tests, -q)
make lint       # ruff check src tests scripts + extract_messages.py --check
make fmt        # ruff check --fix
make i18n       # rewrite the .pot, then report what pt_BR lacks
```

`make setup` has two steps that can fail independently of Python:
`scripts/setup_sdk.sh` copies and re-signs the non-redistributable SVBony arm64
dylib into `vendor/lib` (needed only for live capture), and `make catalog`
downloads OpenNGC to `data/NGC.csv` (needed by the TARGETS panel).

CLI, all under `astrodoro <command>` (`cli/stack.py` and `cli/probe.py` register
the subparsers):

| command | purpose |
| --- | --- |
| `bias` / `dark` / `flat` | build calibration masters |
| `run` | headless stacking session |
| `replay <folder>` | reprocess a recorded session |
| `lucky <folder>` | stack a Moon or planet burst offline: rank by sharpness, align, average |
| `post <fits>` | post-process a linear stack: gradient, colour, optics, stretch |
| `info` / `usb` / `bench` / `tec` / `grab` / `sensor` | camera diagnostics |
| `catalog` | download OpenNGC to `Settings.catalog_path()` |
| `settings [--set K V]` | print or change any settings field |

Makefile wrappers take `EXP GAIN BIN FRAMES TEMP BIAS DARK FLAT OUT FOLDER`, e.g.
`make dark EXP=5 GAIN=250 TEMP=-10`, `make replay FOLDER=~/Astrodoro/sessions/2026-08-18/2130_M8`.

## Tests

Plain pytest, no plugins. `tests/conftest.py` does two things every test relies
on: sets `QT_QPA_PLATFORM=offscreen` *before* Qt is imported, and points
`ASTRODORO_CONFIG_DIR` / `ASTRODORO_DATA_DIR` at temp dirs so nothing reads or
writes the real configuration. Fixtures: `qapp` (session-scoped, Qt allows only
one `QApplication`) and `settings` (defaults backed by `tmp_path`).

```bash
.venv/bin/python -m pytest tests/test_register_warp.py   # one file
.venv/bin/python -m pytest -k smear                      # one topic
.venv/bin/python -m pytest tests/test_tonight.py::test_x  # one test
```

The suite must stay hardware-free: CI (`.github/workflows/ci.yml`) runs `ruff
check`, `extract_messages.py --check` and `pytest` on Linux with no camera and
no dylib. A check that needs the camera belongs behind a `cli/probe.py` command,
not in `tests/`.

## Architecture

Layering is one-way and enforced by convention, not tooling — keep it:

```
ui/  cli/        may import core, pointing, drivers, settings, i18n
core/            frame pipeline; no Qt, no hardware
pointing/        where the tube points; no Qt in model.py, Qt only in handset.py
drivers/svbony/  sdk.py = 1:1 ctypes binding; camera.py = Pythonic wrapper
```

Nothing in `core/`, `pointing/` or `drivers/` may import `ui/`, and **nothing
above `drivers/` may touch `sdk` directly** — `Camera.open()` is where the
camera's documented quirks get neutralised (`_force_linear()` reasserts neutral
WB/gamma and disables bad-pixel correction on every open, because the SDK
persists those to disk between sessions).

**Per-frame pipeline**, run by `ui/worker.py::CaptureWorker` in a thread and by
`cli/stack.py` headless — same functions, deliberately:

```
FrameSource.read()          core/source.py — CameraSource (live) | ReplaySource (FITS)
  → calibration.calibrate() (raw-dark|bias)/flat → hot px → /full_scale → clip
  → debayer.to_rgb()        display path
    debayer.cfa_to_luminance()  detection path
  → stars.detect()          sep, per-frame quality metrics
  → register.estimate()     similarity against the accumulated stack
  → LiveStacker.add()       float32 accumulator + weight map, returns FrameOutcome
  → stretch                 display only
  → Recorder                RICE-compressed subs, session.json, stacks
```

`core/calibration.py` is one function on purpose: it was duplicated between the
GUI and the CLI and the order of operations is not optional. `core/masters.py`
is the other half of it — how a bias, a dark and a flat are built, named and
checked against the camera — shared by `_capture_*` in the worker and the
`bias`/`dark`/`flat` commands, which used to carry two copies of the naming
convention.

The rest of `core/` hangs off that spine: `stacker.py` (acceptance and the
accumulator), `background.py` and `stretch.py` (display only), `focus.py` (HFR
and, for a surface target, gradient contrast; loupe), `equatorial.py` (residual
platform rotation), `polar.py`, `cooling.py`, `catalog.py`, `tonight.py` (the
target ranking), `previews.py` (DSS cutouts, disk cache first — in the field
there is no network).

`core/platform_align.py` is the alignment procedure: it turns the residual
rotation `equatorial.py` measures into the two screw movements of the platform,
by way of `polar.py`, which knew how to do that half and had no axis to do it
to since the plate solver was removed. One target gives the projection of the
error along it, two far enough apart give the whole of it. `ui/main.py` drives
it from a window (`_build_align_window`), and the worker measures a station on
a `request`, off the star lists it already produces — no accumulator, no disk.

`core/lucky.py` is off that spine on purpose. The Moon and the planets invert
every assumption the pipeline holds — milliseconds instead of seconds,
saturation instead of noise, no stars to register on — so the lucky path in
`ui/worker.py` branches before star detection and never reaches the stacker.
What it produces instead is an ephemeris of the bodies no catalogue carries
(`lucky.body_at`), an exposure guard (`lucky.levels`), a contrast focus metric
(`focus.SharpnessMeter`) and a `recorder.Burst`: a bounded run of frames written
straight to disk.

Both measurements happen inside a **window around the body** (`lucky.window`),
not over the frame. That is the whole difference between the Moon and a planet:
the Moon fills a third of a bin1 frame and the two agree, Jupiter covers two
hundredths of a percent of it, where the frame's sharpness is the sharpness of
the sky noise and its lit fraction reads as an empty frame. The exposure of each
body is scaled off the Moon's by the ratio of surface brightnesses
(`lucky.exposure_for`), capped where a longer frame would average the seeing
instead of freezing it.

`core/lucky_stack.py` is what puts them back together, offline: lucky imaging
picks frames by comparing them against each other, which cannot be done while
they arrive. It ranks on the `SHARPNS` the burst wrote into every header — a
header read instead of a gigabyte — keeps the sharpest fraction, crops each one
to a window it finds by the body's own centroid, and aligns them by phase
correlation, translation only. `astrodoro lucky <folder>` runs it.

`core/postprocess.py` is also offline and off the spine: gradient removal,
atmospheric-dispersion channel alignment, colour calibration, optional
deconvolution and PSF matching, the arcsinh stretch, then chroma/luminance
denoise and masked saturation — in that order because gradient and colour only
mean what they say on linear data, and the stretch is the one non-linear
transition. `astrodoro post <fits>` runs it once on a finished stack; nothing in
it runs per frame.

`pointing/` is the phone: `handset.py` serves the page and the WebSocket over
**one** TLS port (DeviceOrientation only fires in a secure context, and a
self-signed exception is per host *and* port), `model.py` joins samples +
`orientation.py` maths + `brightstars.py`, `pushto.py` turns that into an arrow.

`ui/main.py` is one window organised by **task mode**, not by settings category
— `MODES` = frame / targets / integrate / lucky ("PLANETS"), in the order of a
night — with a vitals bar that never leaves the screen and, above it, a top
bar for what is not a phase: the night-mode / image-only / log toggles and the configuration, which
opens in its own window (`_build_config_window`) and moves neither the mode nor
the view. `ui/design.py` owns tokens, typography and shared widgets; new UI
composes from there.

## Things that break silently

These are load-bearing. Each is explained with its measurement in
`docs/design-notes.md`; `CONTRIBUTING.md` has the full list.

- **`register.warp` takes `M` forward** (src → dst), no `WARP_INVERSE_MAP`.
  Inverting it is this stage's classic bug — `tests/test_register_warp.py` exists
  only for that.
- **The registration reference is the accumulated stack**, not the first frame.
  The first accepted frame locks the reference *geometry*; the star list is
  re-extracted.
- **Luminance is half resolution.** `debayer.cfa_to_luminance` sums the 2x2 quad,
  so coordinates come back to full scale via `lum_scale=2.0` and any level
  comparison there must use `debayer.LUM_SUM` (4.0), not 1.0.
- **A dark and a bias are never both subtracted.** A dark contains the bias;
  `calibrate` takes the dark when there is one and the bias otherwise. A flat,
  though, wants the *bias* — it is milliseconds long, so the session's dark
  carries thermal signal the flat never collected.
  `tests/test_calibration.py` and `tests/test_worker_calibration.py` hold both.
- **The weight decides acceptance, not the limits.** `STRICTNESS` only moves
  `min_weight`; elongation, halo and FWHM are identical safety belts at all
  three levels.
- **Rejections are categorised** (`FrameOutcome.kind`) and every verdict is
  auditable on screen (`elong`, `halo`, `limits`). A new filter needs a new kind
  and a new readout, or the counts and the explanation lie.
- **The target score is auditable factor by factor** (`Suggestion.factors`), for
  the same reason.
- **`background` and `stretch` never touch the accumulator** — display only.
- **Worker parameters arrive via `request(**kw)` / `flag(name)`**, a dict under a
  lock applied between frames. **Not Qt slots**: the loop sits inside
  `SVBGetVideoData` for the whole exposure, so that thread's event loop is dead.
- **Text must not decide the window width.** A `QLabel`'s minimum width is its
  full text and Qt satisfies it by growing the *window*, which once pushed the
  right edge off-screen. Use `ElidedLabel` (`ui/design.py`) for anything whose
  content changes during the night; `tests/test_gui_window_width.py` holds the
  floor.
- **CONFIG is a window, not a mode.** Putting it back in `MODES` silently
  stops the stack when it is opened — `can_integrate` is `mode == "stack"`
  again, and the old code had to list `"config"` there for that reason.
  `tests/test_gui_config.py` holds it.
- **PLANETS never stacks and never autostretches.** `can_integrate` excludes
  the mode, and `_stretch` branches to a fixed linear mapping: the deep-sky
  stretch renormalises per frame, which makes the disc pulse while you focus. Closing the window there also does not write the capture strip over the
  deep-sky defaults — `tests/test_gui_lucky.py` holds both.
- **Everything in that mode is measured in a window, not in a frame.**
  `lucky.window` finds the body; `levels` and `sharpness` are computed inside it.
  Its *size* is settled once and only its centre follows the body afterwards —
  the size is the denominator of both measurements, so a window that resized
  itself frame by frame would move the sharpness meter while the focuser stood
  still. `tests/test_lucky.py` holds it.
- **The view follows the body; the frame never moves.** `lucky.centroid` on
  every drawn frame gives where the body is, and the GUI moves the *viewport*
  onto it (`_follow_body`). Shifting the pixels instead would resample 11.7 MP
  per drawn frame to correct something the offline stack corrects for free, and
  would put an interpolation between the eye and the focus it is judging.
- **Grey-world belongs to the Moon alone.** The lunar surface really is grey, so
  matching the channel medians over the disc is a measurement. On Mars the same
  operation corrects the camera for a colour the planet has, and the button is
  disabled off the Moon.
- **PLANETS draws fewer frames than it captures.** `_due_for_display` caps the
  display at `lucky_display_fps`; the `frame` signal is queued across threads
  and unbounded, so at capture rate the queue grows by a 140 MB frame
  faster than the GUI can drain it. Every frame is still measured and recorded.
  The stretch there is a LUT for the same reason — 67 ms against 384 ms at bin1.
- **The alignment solve is in hour angle, at the middle of the run.** The
  platform's axis is fixed against the ground, not against the stars, so the
  error only stands still in a frame that turns with the Earth — and the rate a
  run reports is an average, which belongs to the middle of its span. Using RA,
  or the start of the run, biases the answer by 3% over ten minutes and 13%
  over forty; `tests/test_platform_align.py` measures both against a simulated
  sensor.
- **`platform_parity` cannot be derived, only measured.** The rotation is read
  in sensor coordinates and an odd number of reflections mirrors it. A
  backwards correction *doubles* the error, so re-measuring one field after
  correcting settles the sign on the first try — that is what `verify` reports
  and the only thing that should ever set it.
- **bin2, not bin3/bin4** — those clip highlights, and binning is host-side so
  it buys no speed. `docs/hardware.md` has nine such measured traps; read it
  before touching `drivers/` or calibration.

## Working without hardware

This is the normal development path, and why the abstractions exist. Prefer
both of these to mocks — they exercise the real code path:

- **No camera:** `ReplaySource` replays a session's FITS subs honouring each
  frame's metadata (`astrodoro replay <folder>`, or pick "replay" as the source
  in the GUI, where there is a speed control and a loop toggle).
- **No phone:** `make handset` runs `scripts/fake_handset.py`, a fake device
  including the crooked-mount twist, against the GUI's sensor server.

## Conventions

- **The source is English** — identifiers, comments, docstrings, log messages.
  Do not reintroduce Portuguese into the source. **Talk to the user in
  Portuguese.**
- **User-visible strings go through `_()`** (`N_()` for deferred ones) from
  `astrodoro.i18n`, with named `{placeholders}`. Catalogues are `.po` read at
  runtime — no build step, no system gettext. After adding a string run
  `make i18n`; `make lint` fails on a stale `.pot`.
- **Comments are the exception, not the habit.** The default is no comment: if
  the code already says it, a comment repeating it is noise and gets deleted.
  Never annotate a decision with the alternatives it rejected — that is what
  `docs/design-notes.md` is for, and a wall of justification above every line
  makes the module unreadable.
  What earns a comment is only what the code cannot say on its own: a measured
  number ("bin1 and bin2 take the same time"), a constraint coming from outside
  (an SDK quirk, a Qt behaviour, a FITS convention), or an order of operations
  that is load-bearing and not visible locally. If you change something a
  comment measures, measure it again and update the number there, in the README
  and in `docs/` — all three quote concrete values.
- Long rationale goes to `docs/design-notes.md`, with at most a one-line pointer
  from the code.
- Ruff, line length 88, `py311`. The ignores in `pyproject.toml` are each
  justified there (`bin` shadows a builtin but is the SDK's own name, `°`/`'`/`"`
  in the interface are deliberate, `int(round(x))` on numpy scalars is not
  redundant).
- `assets/` variants are generated: run `make brand`, never hand-edit one.

## Where files land

Never inside the repository. `Settings` (`settings.py`) is a JSON dataclass at
`~/Library/Application Support/astrodoro/settings.json` (overridable with
`ASTRODORO_CONFIG_DIR`), shared by the GUI and the CLI, holding folders,
observing site, optics, language, display and capture defaults. It never raises
on a corrupt file — it would fail in the dark, in the field. **New tunable
values become fields there, not module constants.**

Sessions go to `<capture_dir>/YYYY-MM-DD/HHMM_target/` (`subs/*.fits`,
`session.json`, `stack*.fits`), with bias, darks, flats and exports alongside —
all five configurable, default `~/Astrodoro/`. `astrodoro settings` prints the path
and every value.

## Further reading

- `CONTRIBUTING.md` — layout, the complete invariant list, what each test
  protects, how to add a language.
- `docs/design-notes.md` — the measured reasoning, and the attempts that were
  reverted. Read before simplifying the stacker, the alignment, the sky map or
  the ranking.
- `docs/hardware.md` — the SV405CC's measured traps.
- `docs/third-party.md` — embedded/downloaded data and their licences.
