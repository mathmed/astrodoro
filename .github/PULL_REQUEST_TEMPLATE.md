**What this changes, and why.**

**What you measured.** If the change touches anything the code cites a number
for — exposure, frame rate, rejection thresholds, the field of view — measure it
again and update the number in the comment, in the README and in `docs/`. Three
places quote concrete values on purpose.

**Invariants.** `CONTRIBUTING.md` lists the ones that break silently. If you
touched one, say which and why.

**Checks.**

- [ ] `make lint` passes (ruff, plus the message catalogue check)
- [ ] `make test` passes
- [ ] `make smoke` passes (the CLI, a replay and the GUI boot for real)
- [ ] `make lint-imports` passes, and no contract was relaxed
- [ ] the Quality Report comment on this PR has no ❌ (mutation testing included)
- [ ] user-visible strings go through `_()` and `make i18n` was run
