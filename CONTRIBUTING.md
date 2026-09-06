# Contributing to Astrodoro

Thanks for looking. This project grew out of one telescope, one camera and one
back garden, so the most valuable contributions are usually **measurements from
different hardware** and the bugs they expose.

## Ground rules

- **Everything in the source is English** — identifiers, comments, docstrings,
  log messages. User-visible text goes through `_()` and is translated in
  `src/astrodoro/i18n/locale/`.
- **Comments explain _why_, with the measured number when there is one.** A
  comment that restates the code is deleted; a comment recording a reverted
  attempt stays, so nobody pays for that mistake twice.
- **If you change measured behaviour, measure it again** and update the number
  in the comment, in the README and in `docs/`. Three places cite concrete
  values on purpose.
- Prefer a replay session or the fake handset over a mock. Both exercise the
  real code path.

## Getting set up

```bash
git clone https://github.com/mathmed/astrodoro
cd astrodoro
uv sync --extra dev
uv pip install -e .
```

The interpreter is always `.venv/bin/python` (the Makefile calls it `PY`), never
the system Python — the vendor dylib and the native extensions are bound to that
venv.

You do **not** need the camera. `make sdk` only matters for live capture; every
test and most of the interface run without it.

```bash
make test     # pytest
make lint     # ruff, plus a check that the message catalogue is current
make fmt      # apply the fixes ruff can make itself
```

## Repository layout

```
src/astrodoro/
├── core/         frame pipeline — no Qt, no hardware
│   ├── calibration.py  (raw - dark|bias) / flat, hot pixels, scaling
│   ├── masters.py      how a bias, a dark and a flat are built and named
│   ├── debayer.py      mosaic → RGB, and → luminance
│   ├── stars.py        detection and per-frame quality metrics
│   ├── register.py     frame → reference transform
│   ├── stacker.py      the accumulator and the acceptance decision
│   ├── stretch.py      display autostretch (MTF and arcsinh)
│   ├── background.py   sky-background tile sampling
│   ├── source.py       FrameSource: live camera and replay
│   ├── recorder.py     session on disk
│   ├── focus.py        HFR history, trend, loupe
│   ├── equatorial.py   residual rotation of the platform
│   ├── polar.py        an axis → the two screw movements; also from positions
│   ├── platform_align.py  polar alignment from the residual field rotation
│   ├── cooling.py      TEC ramp, saturation, warm-up
│   ├── catalog.py      OpenNGC lookup
│   ├── tonight.py      which objects are worth imaging now, and why
│   └── previews.py     DSS cutouts of an object: cache first, network optional
├── pointing/     handset server, orientation maths, star and constellation data
├── drivers/      svbony: ctypes binding plus a Pythonic wrapper
├── ui/           Qt: window, capture thread, design system, sky map, history,
│                 ranked target list, floating loupe
├── cli/          argparse commands
├── i18n/         catalogues and the runtime loader
└── settings.py   user settings
```

The dependency direction is one-way: `ui` and `cli` may import `core`,
`pointing` and `drivers`; none of those may import `ui`.

## Invariants that cross files

Break one of these and the stack degrades silently, which is the worst failure
mode this program has. The reasoning behind each is in
[docs/design-notes.md](docs/design-notes.md).

- **Luminance is half resolution.** `debayer.cfa_to_luminance` sums the 2x2
  Bayer quad, so star coordinates return to full resolution via
  `scale`/`lum_scale=2.0`, and the detection saturation limit compares against
  `debayer.LUM_SUM` (4.0), not 1.0. Passing 1.0 discards every star above ~24%
  of sensor scale as saturated.
- **The registration reference is the accumulated stack**, not the first frame.
  The reference *geometry* is locked by the first accepted frame; only the star
  list is re-extracted.
- **`register.warp` takes M in the forward direction** (src → destination),
  without `WARP_INVERSE_MAP`. Inverting it is the classic bug of that stage;
  there is a dedicated test.
- **A dark and a bias are alternatives, never a sum.** `calibrate` subtracts
  the dark when its shape matches and the bias otherwise: a dark is taken at
  the lights' exposure with the sensor capped, so it already contains the
  pedestal a bias measures, and subtracting both drives the sky negative into
  the `maximum` clamp. A **flat** is the opposite case: it wants the bias, not
  the session's dark, because a flat is milliseconds long and that dark carries
  thermal signal the flat never collected.
- **A master is a median and its name is its contract.** `core/masters.py`
  builds, names and checks all three kinds for both front ends. The geometry is
  the only refusal; gain, offset, exposure and temperature are warnings with the
  number in them (`masters.mismatch`), and which of those matter depends on the
  kind — the offset defines a bias, the exposure and temperature define a dark,
  a flat cares about neither.
- **The transform is a similarity, not a translation** (residual platform
  rotation), in two layers: asterisms via astroalign, then translation voting
  for fields with 3–14 stars.
- **`stretch` only affects the display.** The float32 accumulator is never
  modified by it. Its controls live in the INTEGRATE panel, beside the stack
  they act on; the configuration window holds only settings that are set once
  and persisted.
- **Rejection is categorised.** `FrameOutcome.kind` feeds
  `LiveStacker.rejections`, which the GUI shows per reason. A new filter needs a
  new `kind`, otherwise the counts lie.
- **The weight decides acceptance, not the limits.** Elongation, halo and FWHM
  are safety belts, identical at all three strictness levels.
- **`best_fwhm` is monotonic.** Only an accepted frame with enough stars may
  move it.
- **Every frame's verdict is auditable on screen.** `FrameOutcome` carries
  `elong`, `halo` and the `limits` dict; a new criterion has to appear there too.
- **Detection uses the central 70% only.** Edge coma biases the centroid. The
  stack uses the whole frame.
- **A suggestion's score is auditable, factor by factor.** `tonight.rank` fills
  `Suggestion.factors` and the TARGETS panel shows every one of them, for the
  same reason a rejected frame shows its measurements. A new factor has to
  appear there too, otherwise the score becomes an oracle.
- **PLANETS is not a view of the deep-sky pipeline, it is a branch out of it.**
  `ui/worker.py::_process` leaves before star detection, so nothing there
  reaches the stacker, the registration or the platform monitor, and
  `can_integrate` excludes the mode. The display branches too: `_stretch_lucky`
  is a fixed linear mapping, because the autostretch renormalises per frame and
  the disc would pulse while you focus. Anything added to the deep-sky path has
  to be checked against both branches.
- **In that mode nothing is measured over the frame.** `lucky.window` finds the
  body and `levels` and `sharpness` are computed inside it. The Moon fills a
  third of a bin1 frame and the distinction does not show; Jupiter fills a
  two hundredths of a percent of it, where the frame's sharpness is the
  sharpness of the sky and
  its lit fraction reads as an empty frame. The window's *size* is fixed once
  and only its centre follows the body: the size is the denominator of both
  measurements, and one that breathed with the seeing would move the sharpness
  meter while the focuser stood still.
- **Following the body is a viewport move, never a pixel move.** The window's
  centre is refound on every drawn frame and the GUI re-ranges the ViewBox onto
  it. Nothing warps the frame: the recorded burst is raw, and `lucky_stack`
  aligns it afterwards.
- **Grey-world is the Moon's alone.** The lunar surface really is grey, which is
  why matching the three channel medians over the disc is a measurement there.
  Mars is red and Jupiter is tan: the same operation would correct the camera
  for a colour the planet has, so the button is disabled off the Moon.
- **The Sun is not in `lucky.BODIES` and must not be added.** Nothing in this
  program can know whether there is a filter on the tube.
- **The alignment solve lives in hour angle, at the middle of a run.** The
  platform's axis is fixed against the ground, so the error only stands still
  in a frame that turns with the Earth, and the rate a run reports is the
  average over its span. `Station` therefore carries the sidereal time of its
  own middle, and anything that builds one has to supply it. Using RA, or the
  start of the run, biases the answer by 3% over ten minutes and 13% over
  forty.
- **`platform_parity` is measured, never derived.** The rotation is read in
  sensor coordinates and an odd number of reflections mirrors it. Since a
  backwards correction doubles the error rather than leaving it alone,
  re-measuring one field after correcting settles the sign — `platform_align.
  verify` is the only thing that should decide it.
- **GUI parameters reach the worker through `request(**kw)` / `flag(name)`** — a
  dict under a lock, applied between frames. **Do not use Qt slots** for this:
  the loop is blocked inside `SVBGetVideoData` for the whole exposure and that
  thread's event loop does not run.

## Translations

Catalogues are plain `.po` files read directly at runtime. There is no
compilation step and no system `gettext` needed.

```bash
make i18n     # refresh astrodoro.pot and report what pt_BR still lacks
```

To add a language:

1. `cp -r src/astrodoro/i18n/locale/pt_BR src/astrodoro/i18n/locale/<tag>`
2. Add the tag to `LANGUAGES` in `src/astrodoro/i18n/__init__.py`.
3. Fill in the `msgstr` lines. Keep every `{placeholder}` exactly as it appears
   in the `msgid` — they are `str.format` fields.
4. `python scripts/extract_messages.py --missing <tag>` must report 0 missing.

When you add a new user-visible string in code, wrap it in `_()`, use named
`{placeholders}` rather than positional ones, and run `make i18n`.

## Brand assets

`assets/` holds two generated masters (the engraved medallion and the app icon)
plus the variants derived from them. Never hand-edit a variant:

```bash
python scripts/derive_brand.py
```

The reasoning behind each variant — and why the application icon is built from
two different drawings — is in [assets/README.md](assets/README.md).

## Tests

Plain pytest. `tests/conftest.py` sets `QT_QPA_PLATFORM=offscreen` and points
the settings at a throwaway directory, so no test touches your real
configuration.

```bash
.venv/bin/python -m pytest tests/test_register_warp.py     # one file
.venv/bin/python -m pytest -k smear                        # one topic
```

What the suite covers, and why each test exists:

| file | what would break without it |
| --- | --- |
| `test_register_warp.py` | the direction of the transform, inverted silently |
| `test_stacker_synthetic.py` | end-to-end SNR gain and surviving a platform reset |
| `test_stacker_smear.py` | the smear that elongation cannot see |
| `test_orientation.py` | a flipped sign in the alignment, which doubles the error |
| `test_pushto.py` | the arrow pointing confidently the wrong way, and the alignment star suggested on the far side of the sky |
| `test_gui_framing.py` | every gesture of the Frame mode actually running |
| `test_gui_targets.py` | the suggestion list, the hour field and the loupe running |
| `test_tonight.py` | the ranking suggesting something below the horizon, or ignoring the Moon |
| `test_previews.py` | a thumbnail cache that re-downloads, or an offline night raising instead of shrugging |
| `test_gui_frame_review.py` | clicking a health mark and getting that frame back |
| `test_settings.py` | a corrupt settings file must never stop the program |
| `test_i18n.py` | a missing translation must fall back, never blank a label |
| `test_branding.py` | the app icon losing either of its two drawings |
| `test_lucky.py` | the phase reading backwards (Venus is the case where the Moon's own formula fails), an exposure guard blind to a whole Bayer phase, a measuring window that loses a small planet or takes noise for a body, a burst that never stops |
| `test_lucky_stack.py` | the alignment shift applied backwards, which stacks a quiet blur, a sharpness ranking that keeps the worst frames, and a planet averaged where it was instead of where it went |
| `test_gui_lucky.py` | the view autostretching, a finished burst leaving its button pressed, bright-body exposures overwriting the deep-sky defaults, grey-world offered on a planet |
| `test_platform_align.py` | the sign of the correction, and the frame it is solved in — both checked against a simulated mount, not against the module's own formula |
| `test_gui_align.py` | alignment creeping back into the rail as a step, a station kept without the sidereal time that gives it meaning, a re-measurement counted as a second equation instead of as the check it is |
| `test_gui_config.py` | configuration creeping back into the rail as a step, opening it moving the mode or stopping the stack, the night toggles ending up behind a window |
| `test_calibration.py` | the calibration order, a bias and a dark subtracted together, a flat normalised with the pedestal still in it, a master's name losing what it has to match |
| `test_worker_calibration.py` | a master recorded with the session's exposure instead of the minimum (or not put back afterwards), a recorded master not loaded, a flat corrected by the wrong pedestal |
| `test_gui_calibration.py` | one kind's refusal blanking another's line, the panel claiming a correction the worker refused |

A test that needs the camera does not belong in the suite; put it behind a CLI
command in `astrodoro.cli.probe` instead.

## Pull requests

- One topic per PR, and say what you measured.
- `make lint` and `make test` pass.
- If you touched an invariant above, say which and why in the description.
