<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.png">
    <img src="assets/logo-light.png" alt="Astrodoro" width="300">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/mathmed/astrodoro/actions/workflows/ci.yml"><img src="https://github.com/mathmed/astrodoro/actions/workflows/ci.yml/badge.svg" alt="ci"></a>
  <a href="https://github.com/mathmed/astrodoro/releases/latest"><img src="https://img.shields.io/github/v/release/mathmed/astrodoro?sort=semver" alt="latest release"></a>
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue" alt="python">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey" alt="platform">
  <a href="LICENSE"><img src="https://img.shields.io/badge/licence-GPL--3.0--or--later-blue" alt="licence"></a>
</p>

# Astrodoro

Capture and live stacking for **electronically assisted astronomy** (EAA). One
window that grabs frames, calibrates, detects stars, registers, stacks,
autostretches for display and records the subs — plus a headless CLI over the
same core.

It exists because there is no SharpCap equivalent on the Mac: you can capture
(AstroDMx, oaCapture) or live-stack from a watched folder (Siril, ASTAP, ALS),
but nothing does both in one loop.

It is built around a specific, awkward setup — a **Dobsonian on an equatorial
platform, with no goto, no autoguiding and no plate solving** — and most of the
non-obvious decisions follow from that. A phone strapped to the tube is the only
pointing sensor. Frame acceptance is judged against what the night is actually
delivering, not against absolute thresholds a different telescope would meet.

**Camera:** SVBONY SV405CC. The driver is isolated (`astrodoro.drivers`), so
another vendor means writing one module and adding a line to `_DRIVERS`.

## Install

A build from [the latest release](https://github.com/mathmed/astrodoro/releases/latest)
carries its own Python and Qt: unpack it and run it.

| download | for |
| --- | --- |
| `astrodoro-*-macos-arm64.zip` | Apple Silicon; move `Astrodoro.app` to Applications |
| `astrodoro-*-windows-x86_64.zip` | Windows 10 and 11 |
| `astrodoro-*-linux-x86_64.tar.gz` | glibc 2.35 and newer |

**Live capture is macOS only.** The SVBony SDK is not redistributable, so no
download contains it: install it separately and point `SVB_SDK_PATH` at the
folder holding `libSVBCameraSDK.dylib`. On Windows and Linux there is no camera
driver at all — those builds run the interface, the replay source and the CLI,
which is enough to reprocess a night and to work on the code.

From source, with Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/mathmed/astrodoro
cd astrodoro
make setup          # uv sync, prepare the SDK (macOS), download the catalogue
make gui
```

`make` with no target lists every shortcut. `make setup` also runs
`scripts/setup_sdk.sh`, which takes the SDK from a local AstroDMx installation,
rewrites its libusb path and re-signs it — on arm64, modifying a dylib
invalidates its signature and dyld refuses to load it.

## Use

The window is organised by **task mode**, not by settings category — each phase
of the night needs a different screen:

| mode | key | what it is for |
| --- | --- | --- |
| **Frame** | `1` | find and centre the target: live frame, phone sensor, push-to arrow |
| **Targets** | `2` | what is worth imaging at this hour, ranked, with every factor shown |
| **DSO** | `3` | stack, record, calibrate, align the platform |
| **Planets** | `4` | the Moon and the planets: exposure guard, contrast focus, burst |

Other keys: `V` stack/frame, `M` the sky map, `Z` the loupe, `F` image only,
`N` night mode, `L` the log, `A` align the platform, `R` record a burst,
`Esc` leave a frame review, `Ctrl+S` save what is on screen.

```bash
astrodoro bias  --gain 250 --offset 20 --frames 30
astrodoro dark  --exp 5 --gain 250 --frames 20 --target-temp -10
astrodoro flat  --exp 0.5 --gain 250 --bias ~/Astrodoro/bias/bias_g250_o20_bin2.fits
astrodoro run   --exp 5 --gain 250 --dark ... --flat ...
astrodoro replay ~/Astrodoro/sessions/2026-08-18/2130_M8
astrodoro lucky  ~/Astrodoro/sessions/2026-08-18/2210_Moon --best 25
astrodoro info | usb | bench | tec | sensor | catalog | settings
```

## What it does differently

- **The registration reference is the accumulated stack**, not the first frame,
  so the reference improves as the night goes on.
- **Acceptance is a relative weight.** A frame is judged against what tonight is
  delivering; every rejection is categorised and every verdict is on screen, so
  the counts can be argued with.
- **The phone is the pointing sensor.** One alignment star, chosen for you:
  bright, unmistakable, comfortable to reach and as close to the target as
  possible, because one star corrects two axes and its accuracy is local.
- **Targets ranks the sky by six factors** — altitude, the Moon, size against
  the field, surface brightness, the time left before it sets, and fame — and
  shows all six, so the ranking can be argued with too.
- **The Moon and the planets get their own mode.** Milliseconds instead of
  seconds, saturation instead of noise, no stars to register on: nothing is
  stacked live. Frames go to disk in a burst and `astrodoro lucky` ranks them by
  sharpness afterwards, keeps the best and aligns them.
- **The platform is aligned from the field rotation itself**, live and with no
  plate solve: press `A` and the polar error in degrees is read off the rotation
  of the stars, with its own uncertainty, while you turn the screws.
- **Three masters, recordable mid-session** — bias, dark and flat, each with the
  gain, offset, bin and temperature of the session in progress, which is exactly
  what a master has to match.

## Working without hardware

This is the normal development path, and the reason the abstractions exist.
Prefer both of these to mocks — they exercise the real code path.

```bash
astrodoro replay ~/Astrodoro/sessions/2026-08-18/2130_M8   # no camera
make handset                                               # no phone
```

`ReplaySource` plays back a recorded session's FITS subs honouring each frame's
metadata; in the GUI it has a speed control and a loop toggle.
`scripts/fake_handset.py` connects a fake phone to the sensor server, crooked
mount included.

## Where files go

Nothing is written into the repository:

```
~/Astrodoro/sessions/YYYY-MM-DD/HHMM_target/
    subs/sub_00001.fits ...   RICE-compressed raw subs
    session.json              settings, target, statistics
    stack.fits, stack_final.fits, previews
~/Astrodoro/bias/  darks/  flats/  exports/
```

All five folders are configurable, in **config → folders** or with
`astrodoro settings --set capture_dir ...`. Settings live in a JSON file outside
the project; `astrodoro settings` prints the path and every value.

The interface ships in **English** and **Brazilian Portuguese**
(`astrodoro --lang pt_BR`). Adding a language means copying one `.po` file and
translating it.

## Architecture

```
src/astrodoro/
├── core/         frame pipeline: calibration → stars → registration → stacking,
│                 plus the target ranking and the lucky-imaging path
├── pointing/     where the tube points: phone server, orientation, sky data
├── drivers/      base.py says what a camera is; svbony/ implements it.
│                 Nothing above here talks to an SDK, or names a vendor
├── ui/           Qt interface: window, capture thread, design system, sky map
├── cli/          headless commands
└── settings.py   user settings, shared by the GUI and the CLI
```

`FrameSource` → `calibration.calibrate` → debayer/luminance → `stars.detect` →
`register.estimate` → `LiveStacker.add` → `stretch` for display → `Recorder`.
The GUI and the CLI call the same functions; calibration in particular lives in
exactly one function so the two cannot drift apart.

The decisions that are easy to "simplify" back into a bug — and the camera
quirks they compensate for — are listed in
[CONTRIBUTING.md](CONTRIBUTING.md#invariants-that-break-silently).

## Contributing

Bug reports, measurements and patches are welcome — especially from anyone with
different hardware. This grew out of one telescope, one camera and one back
garden, so a number measured on a different setup is the most useful thing
anyone can send. Start at [CONTRIBUTING.md](CONTRIBUTING.md); security reports
go through [SECURITY.md](SECURITY.md).

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE) and
[docs/third-party.md](docs/third-party.md) for the embedded data, the
attributions it carries and the projects this one learned from.
