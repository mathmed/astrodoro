# Contributing to Astrodoro

This grew out of one telescope, one camera and one back garden, so the most
useful contributions are usually **measurements from different hardware** and
the bugs they expose.

## Getting set up

```bash
git clone https://github.com/mathmed/astrodoro
cd astrodoro
uv sync --extra dev && uv pip install -e .

make test              # pytest
make lint              # ruff, vulture, bandit, xenon, mypy, i18n,
                       # suppressions, import contracts — what CI runs
make lint-imports      # the layering contracts alone (import-linter)
make suppressions      # the suppression ratchet alone
make suppressions-update  # lock in a lower baseline after a cleanup
make risk-floor        # minimum risk level of the branch (BASE=origin/main)
make risk-labels       # create the risk:<level> labels on GitHub (needs gh)
make smoke             # boot the CLI, a replay and the GUI, offscreen
make mutation          # mutmut over the decision logic; slow
make mutation-changed  # mutmut over what the branch changed, as a PR
make fmt               # the fixes ruff can make itself
make hooks             # run the checks before each commit
```

The interpreter is always `.venv/bin/python` (`PY` in the Makefile), never the
system one: the vendor dylib and the native extensions are bound to that venv.

**You do not need the camera.** `make sdk` only matters for live capture; the
whole suite and most of the interface run without it.

## Ground rules

- **The source is English** — identifiers, comments, log messages — and carries
  **no comments and no docstrings**. The reasoning lives in the invariants
  below and in the tests named after them. The only text left in the source is
  what a tool reads: `# noqa`, `# type: ignore` — and every one of those is
  counted by the [suppression ratchet](#the-suppression-ratchet).
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

`make lint-imports` enforces all of this: the contracts are in
`[tool.importlinter]` in `pyproject.toml`, one per rule above. A change that
breaks one changes the code, not the contract — relaxing a contract is an
architecture decision and needs the owner's approval in the PR.

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

## Smoke, import contracts and mutation testing

`make smoke` runs `scripts/smoke.py`, which starts the program the way a user
does, in a throwaway config, data and capture folder: `astrodoro --version`,
`astrodoro settings --set ...`, a synthetic session written by the real
`Recorder` and reprocessed with `astrodoro replay` into a `stack_final.fits`,
and the `astrodoro-gui` entry point brought up offscreen until its window is
shown. There is no HTTP API, so this is the "up and ready" check: each step
fails on a non-zero exit, a timeout or a traceback, and prints its output. No
camera, no phone and no network. CI runs it in every cell of the `checks`
matrix — Linux on 3.12 and 3.13, macOS and Windows — because a bundle
ships for each of those platforms.

`make lint-imports` checks the contracts in `[tool.importlinter]` (see
[Layout](#layout)). They were introduced with no violation, so there is no
`ignore_imports` baseline. **Do not relax a contract to make a change pass** —
no new `ignore_imports` line, no layer moved, no module taken off a
`forbidden` list — without the owner's approval asked for in the PR. Change
the code instead.

[mutmut](https://github.com/boxed/mutmut) mutates the modules listed in
`only_mutate` under `[tool.mutmut]` — the decision logic: which master
applies, how the polar error is read, how the Moon and planets are measured
and cut, how the sky is ranked and where the arrow points — and runs the tests
listed next to them. The pixel pipeline (stars, register, stacker), the
drivers, the front ends and the I/O are out: they are numerics around OpenCV
and sep, hardware or Qt, and their tests take far too long per mutant. mutmut
needs `fork`, so it does not run on Windows.

- `make mutation` runs the whole scope and writes `mutants/report.md`. Slow
  (tens of minutes locally); CI runs it weekly and on demand (the `mutation`
  workflow, *Run workflow*), reports the score by file and the survivors, and
  does not gate.
- `make mutation-changed` is what every pull request runs (the `mutation` job
  in `ci.yml`): only the functions the branch changed inside the scope, found
  from the diff against `BASE` (default `origin/main`). A change to a module
  constant mutates the whole module. It fails below `MUTATION_MIN_SCORE`.

`MUTATION_MIN_SCORE` is a ratchet, set below the measured baseline so that
touching an existing, weakly tested function does not block a PR by itself.
The baseline, measured in CI in September 2026, is **62.9%** (2198 of 3495
mutants killed), from 47.1% in `platform_align.py` to 86.2% in
`calibration.py`; the ratchet starts at **45%**, under the weakest file.
The default lives in `DEFAULT_MIN_SCORE` in `scripts/mutation.py` and in
`ci.yml`; the owner raises it without a commit through the repository
variable `MUTATION_MIN_SCORE` (*Settings → Secrets and variables → Actions →
Variables*). Raise it when the weekly score has gone up, never above it.

To widen or narrow the scope, edit `only_mutate` and
`pytest_add_cli_args_test_selection` together, then run the `mutation`
workflow on demand to measure the new baseline before touching the ratchet.
Read the survivors rather than chasing the number: some are equivalent
mutants no test can kill.

## The suppression ratchet

`make suppressions` runs `scripts/suppressions.py`, which counts, per file and
per kind, every way of silencing a check in `src/`, `scripts/`, `tests/`,
`packaging/` and `hatch_build.py`:

| Kind | Matches |
| --- | --- |
| `noqa` | `# noqa`, `# noqa: E501`, `# ruff: noqa` |
| `type-ignore` | `# type: ignore`, `# type: ignore[code]` |
| `nosec` | `# nosec` (bandit) |
| `no-cover` | `# pragma: no cover`, `# pragma: no branch` |
| `pylint-disable`, `mypy-directive`, `pyright-directive` | `# pylint: disable=...`, `# mypy: ...`, `# pyright: ...` |
| `formatter-off` | `# fmt: off`, `# fmt: skip`, `# isort: skip_file`... |
| `test-skip`, `test-xfail` | `pytest.mark.skip`/`skipif`/`xfail`, `pytest.skip()`/`importorskip()`/`xfail()`, `unittest.skip*`/`expectedFailure`, `self.skipTest()` |

Comments are read with `tokenize` and tests with `ast`, so the same words inside
a string do not count. The counts are compared with the committed
`suppressions-baseline.json`, and any difference fails:

- **a count went up** — a new suppression, or one moved to another file or
  swapped for another kind. Remove it and fix the cause. Raising the baseline
  (`.venv/bin/python scripts/suppressions.py update --allow-increase`) needs
  the owner's approval, asked for in the PR, and shows up in its diff.
- **a count went down** — the check fails until `make suppressions-update`
  writes the lower baseline, committed in the same PR, so the improvement is
  locked in where a reviewer sees it. `update` never raises a count.

The baseline started at 12: two `# type: ignore` in `ui/` and ten
`pytest.skip()` calls in the pointing and targets tests, which skip when the
real sky at test time has no suitable star — they are a measurement of the
night, not a silenced failure, but they are counted all the same.

It runs in `make lint`, as a pre-commit hook and as a step of every cell of
the `checks` matrix. What disables a check for a whole file or project —
`[tool.ruff.lint] ignore` and `per-file-ignores`, bandit's `skips`, vulture's
confidence — lives in `pyproject.toml` and is not counted here: changing it puts
the pull request at the `high` risk floor (see
[Risk floors and the risk label](#risk-floors-and-the-risk-label)).

## Risk floors and the risk label

Every pull request carries exactly one `risk:low`, `risk:medium`, `risk:high`
or `risk:critical` label, assigned by the `pr-writer` agent when it opens the
PR (see [Coding agents and the pull request flow](#coding-agents-and-the-pull-request-flow))
or by whoever opens it by hand. Some files set a minimum:
`.github/risk-floors.yaml` lists, per category, the globs that put a PR at
least at that level.

| Category | Level | Paths |
| --- | --- | --- |
| pipeline config | high | `pyproject.toml`, `uv.lock`, `.pre-commit-config.yaml`, `Makefile`, `suppressions-baseline.json`, `hatch_build.py`, `packaging/**`, `scripts/**`, `.github/**`, `.claude/**`, `CONTRIBUTING.md`, `CLAUDE.md`, `AGENTS.md` |
| migrations and persistent formats | high | `**/migrations/**`, `settings.py`, `core/recorder.py`, `core/source.py`, `core/masters.py` |
| auth and secrets | high | `**/*auth*`, `**/*permission*`, `**/*secret*`, `**/*credential*`, `pointing/handset.py` |
| camera SDK bindings | high | `drivers/svbony/**`, `vendor/**` |
| capture and stacking core | medium | `core/**`, `drivers/**`, `ui/worker.py` |

There is no database here, so what plays the part of a migration is whatever a
later version reads back from disk: the settings file, the recorded sessions
replay opens, and the calibration masters matched by name and header. There is
no login either; the phone link generates a TLS private key and serves on the
local network. The SDK bindings are ctypes calls into a closed library that no
CI runner can exercise.

`make risk-floor` prints the floor of the current branch against `BASE` and the
files behind it; the `pr-writer` agent runs the same command, so the agent and
CI agree. The `risk-label` workflow runs `scripts/risk.py check` on every push
and every label change and fails when the PR has no risk label, more than one,
an unknown one, or one below the floor. It is a workflow of its own so that a
label change does not re-run the matrix, and for that reason it is not in the
Quality Report. The second floor (any deploy step other than "None" is at
least `medium`) depends on the PR description, so only the agent applies it.

The floor keeps a change to the pipeline from passing as a minor one: such a PR
is labelled at least `high` and its pipeline changes are listed under **Look
here**. It does not require anyone's approval in particular; merging stays with
the owner. Lowering a level or dropping a path relaxes a gate and needs the
owner's approval, asked for in the PR. `make risk-labels` creates the four
labels (idempotent: `gh label create --force`).

## The Quality Report

Every pull request gets one comment, *Quality Report*, updated in place on
each push (it is found by the `<!-- quality-report -->` marker). Each CI step
runs through `scripts/quality_report.py run`, which keeps the tool's exit
code and saves its output as a fragment; the `quality-report` job, which runs
after `checks`, `wheel` and `mutation` even when they fail, turns the
fragments into the comment:

- the table at the top has one line per analysis — ruff, mypy, bandit,
  vulture, xenon, the suppression ratchet, import-linter, the message
  catalogue, pytest with coverage, the smoke, the wheel and mutation testing;
- *By platform* shows each check in each cell of the matrix, with its
  duration. When the platforms agree, a section quotes Linux on 3.12; when one
  fails, it names that platform and quotes its output;
- each section below has the details: the failing lines, the least covered
  files, the contracts, the smoke steps, the mutants that survived with their
  diff.

The report only informs. What blocks a merge is each job, exactly as before.
On a pull request from a fork the token cannot write comments: the report is
then only in the summary of the `quality-report` job.

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
- `make lint`, `make lint-imports`, `make test` and `make smoke` pass. This
  applies to coding agents exactly as to people.
- No import contract, mutation scope or ratchet — the suppression baseline
  included — is relaxed without the owner's approval, asked for in the PR.
- If you touched an invariant above, say which and why.
- The description follows `.github/pull_request_template.md`, which GitHub
  pre-fills: **Risk** first (level and one line of why, never below
  `make risk-floor`), a one-sentence **Summary**, at most five **Behavior**
  items in plain words as *before → after* with no file or code names,
  **Unchanged**, **Look here** (the only place for file, function and test
  pointers: the evidence for every Behavior item, pipeline changes, invariants
  touched and what was measured), **Deploy steps** (always present; "None:
  normal merge and deploy." when nothing is needed) and an honest **Not
  verified** — anything that needs the camera, the phone or the real sky
  belongs there unless you checked it on hardware.
- Exactly one `risk:<level>` label, at or above the floor.
