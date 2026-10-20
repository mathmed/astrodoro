## Risk
**<low | medium | high | critical>** — <one line: why>
<!-- Most PRs are low or medium. low: no observable behavior change (docs, tests, CI, formatting, dependency
patch/minor bumps, refactors covered by tests). medium: new behavior behind an existing contract, a new option or
camera driver, or any deploy step. high: can break the main capture or stacking flow or changes behavior
drastically, recoverable by revert. critical: a failure loses captures or user data irrecoverably and is hard to
revert (see "Critical surfaces" in README.md). High and critical name what can break in one concrete sentence;
without it, lower the level. Touching pipeline files is not by itself high. Exactly one risk:<level> label. -->

## Summary
<one sentence: what changes in behavior, and why>

## Behavior
<!-- Max 5, most critical first. Plain words for a person who has not read the code: rules and observable behavior,
short and objective — what the user sees on screen, what the CLI accepts or prints, which frames are accepted or
rejected, what is written to disk, which check blocks what. No backticks, paths, file, function or test names:
evidence goes in Look here. -->
- **<rule or behavior, in plain words>:** <before> → <after>

**Unchanged:** <what a reviewer might fear broke, or "behavior-preserving refactor">

## Look here
<!-- Most important first. The only place for file, function and test references. Every Behavior item has its evidence
here: the test that asserts it, or the code that implements it when no test does (one item may cover several Behavior
items). Each item says which behavior it proves or contains and why it deserves human eyes. Also list:
- changes to pipeline config or checks, mismatches and anything that departs from the task;
- every invariant from CONTRIBUTING.md ("Invariants that break silently") the change touches, and why;
- what was measured when a number the code, the README or docs/ quote changed (exposure, frame rate, rejection
  thresholds, field of view), and where the number was updated. -->
1. `<test or file:function>` — <which behavior it proves or contains, and why it deserves human eyes>

## Deploy steps
<!-- Always present. If nothing is needed, write: None: normal merge and deploy.
Here a deploy is a release: settings or session files written by an older version, a refreshed SDK dylib, a new
catalogue download, a release secret or repository setting. -->
- [ ] Before merge: <repository settings or variables, secrets, labels>
- [ ] After release: <what users or the owner must do: re-create masters, refresh the SDK, re-download data>
- [ ] Rollback: <how to revert, and whether files written by this version still load in the previous one>

**Not verified:** <what the author could not check — anything that needs the camera, the phone or the real sky, which
no CI runner has — or "nothing">
<!-- Only if the diff contradicts the author's handoff notes, add: **Mismatch:** <what the notes said vs what the diff does> -->
