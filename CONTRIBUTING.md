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
│   ├── calibration.py  (raw - dark) / flat, hot pixels, scaling
│   ├── debayer.py      mosaic → RGB, and → luminance
│   ├── stars.py        detection and per-frame quality metrics
│   ├── register.py     frame → reference transform
│   ├── stacker.py      the accumulator and the acceptance decision
│   ├── stretch.py      display autostretch (MTF and arcsinh)
│   ├── background.py   gradient extraction
│   ├── source.py       FrameSource: live camera and replay
│   ├── recorder.py     session on disk
│   ├── focus.py        HFR history, trend, loupe
│   ├── equatorial.py   residual rotation of the platform
│   ├── polar.py        polar alignment from solved positions
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
- **The transform is a similarity, not a translation** (residual platform
  rotation), in two layers: asterisms via astroalign, then translation voting
  for fields with 3–14 stars.
- **`background` and `stretch` only affect the display.** The float32
  accumulator is never modified by them. Their controls live in the INTEGRATE
  panel, beside the stack they act on; the CONFIG panel holds only settings that
  are set once and persisted.
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

A test that needs the camera does not belong in the suite; put it behind a CLI
command in `astrodoro.cli.probe` instead.

## Pull requests

- One topic per PR, and say what you measured.
- `make lint` and `make test` pass.
- If you touched an invariant above, say which and why in the description.
