---
name: Bug report
about: Something behaves differently from what it says it does
labels: bug
---

**What happened, and what you expected instead.**

**Your setup** — the part of it that could matter:

- camera, and whether it is the SV405CC
- telescope and focal length, mount or platform
- macOS version, and `astrodoro --version`
- source: live capture, or `astrodoro replay` on a recorded session?

**How to reproduce it.** A recorded session that reproduces it is worth more
than a description: `astrodoro replay <folder>` runs the same code path as the
live capture.

**What the log said.** The interface keeps it at the bottom of the window (L
toggles it); the CLI prints it.

**If it is a measurement** — a frame rejected that should have been kept, a
target ranked wrongly, an alignment correction in the wrong direction — say what
you measured and what the interface showed. Every verdict here is meant to be
auditable on screen; if it is not, that is a bug of its own.
