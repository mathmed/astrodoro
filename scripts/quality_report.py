"""One Quality Report comment per pull request, built from every CI analysis.

    python scripts/quality_report.py run mypy -- mypy        # in a CI step
    python scripts/quality_report.py render --output report.md

`run` executes a check, streams its output, keeps its exit code — the step is
still the gate — and stores the output as a fragment under quality-fragments/.
The `checks` matrix sets QUALITY_PLATFORM, so each interpreter and platform
leaves its own fragment. `render` reads every fragment and writes the Markdown
that the `quality-report` job posts on the pull request. The report only
informs: it never decides whether a job passes.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

MARKER = "<!-- quality-report -->"
DEFAULT_DIRECTORY = Path("quality-fragments")
PLATFORM_ENV = "QUALITY_PLATFORM"
# The platform whose output a section quotes when every platform agrees.
PRIMARY_PLATFORM = "ubuntu-latest · py3.12"
MAX_DETAILS_CHARS = 6000
# GitHub rejects comments above 65536 characters; the report is re-rendered
# with less detail until it fits.
MAX_REPORT_CHARS = 60000
REDUCED_DETAILS_CHARS = (1500, 0)
MAX_SURVIVORS_LISTED = 30
MAX_COVERAGE_ROWS = 10
# Must match scripts/mutation.py, which prints this prefix and a JSON document.
MUTATION_RESULT_PREFIX = "MUTATION_RESULT: "
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# mutmut's progress spinner prints one braille frame per line.
SPINNER_LINE = re.compile(r"^[⠀-⣿] .*(?:\n|$)", re.MULTILINE)
SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9.]+")
# Unbuffered, so stdout and stderr keep their order in the merged output, and
# UTF-8, which is how the output is read back — also on Windows.
CHILD_ENV = {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}


class Analysis(StrEnum):
    RUFF_CHECK = "ruff-check"
    RUFF_FORMAT = "ruff-format"
    MYPY = "mypy"
    BANDIT = "bandit"
    VULTURE = "vulture"
    XENON = "xenon"
    IMPORT_LINTER = "import-linter"
    CATALOGUE = "catalogue"
    PYTEST = "pytest"
    SMOKE = "smoke"
    WHEEL_BUILD = "wheel-build"
    WHEEL_DATA = "wheel-data"
    MUTATION = "mutation"


class Status(StrEnum):
    OK = "✅"
    WARNING = "⚠️"
    FAILED = "❌"
    SKIPPED = "⏭️"


class DetailsFormat(StrEnum):
    TEXT = "text"
    MARKDOWN = "markdown"


SEVERITY_ORDER = (Status.OK, Status.SKIPPED, Status.WARNING, Status.FAILED)


def worst(statuses: list[Status]) -> Status:
    return max(statuses, key=SEVERITY_ORDER.index, default=Status.OK)


def slug(text: str) -> str:
    return SLUG_UNSAFE.sub("-", text).strip("-")


@dataclass(frozen=True)
class Fragment:
    analysis: Analysis
    exit_code: int
    output: str
    platform: str = ""
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        suffix = f"@{slug(self.platform)}" if self.platform else ""
        path = directory / f"{self.analysis}{suffix}.json"
        payload = {
            "analysis": self.analysis.value,
            "exit_code": self.exit_code,
            "output": self.output,
            "platform": self.platform,
            "seconds": self.seconds,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    @classmethod
    def read(cls, path: Path) -> Fragment:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            analysis=Analysis(data["analysis"]),
            exit_code=int(data["exit_code"]),
            output=str(data["output"]),
            platform=str(data.get("platform", "")),
            seconds=float(data.get("seconds", 0.0)),
        )


@dataclass(frozen=True)
class Finding:
    status: Status
    summary: str
    details: str = ""
    details_title: str = "Output"
    details_format: DetailsFormat = DetailsFormat.TEXT


@dataclass(frozen=True)
class Check:
    analysis: Analysis
    job: str
    label: str = ""


@dataclass(frozen=True)
class Section:
    title: str
    checks: tuple[Check, ...]


# One entry per section. `job` is the CI job that uploads the fragments
# (quality-fragment-<job>...); it must be in the `needs` of quality-report.
SECTIONS = (
    Section(
        "Lint and format (ruff)",
        (
            Check(Analysis.RUFF_CHECK, "checks", "lint"),
            Check(Analysis.RUFF_FORMAT, "checks", "format"),
        ),
    ),
    Section("Types (mypy)", (Check(Analysis.MYPY, "checks"),)),
    Section("Security (bandit)", (Check(Analysis.BANDIT, "checks"),)),
    Section("Dead code (vulture)", (Check(Analysis.VULTURE, "checks"),)),
    Section("Complexity (xenon)", (Check(Analysis.XENON, "checks"),)),
    Section("Architecture (import-linter)", (Check(Analysis.IMPORT_LINTER, "checks"),)),
    Section("Message catalogue (i18n)", (Check(Analysis.CATALOGUE, "checks"),)),
    Section("Tests and coverage (pytest)", (Check(Analysis.PYTEST, "checks"),)),
    Section("Boot smoke (CLI, replay, GUI)", (Check(Analysis.SMOKE, "checks"),)),
    Section(
        "Packaging (wheel)",
        (
            Check(Analysis.WHEEL_BUILD, "wheel", "build"),
            Check(Analysis.WHEEL_DATA, "wheel", "package data"),
        ),
    ),
    Section("Mutation testing (mutmut)", (Check(Analysis.MUTATION, "mutation"),)),
)


@dataclass(frozen=True)
class CheckResult:
    check: Check
    finding: Finding
    seconds: float = 0.0
    platforms: tuple[tuple[str, Status, float], ...] = ()


@dataclass(frozen=True)
class SectionResult:
    section: Section
    status: Status
    results: list[CheckResult]


def clean(output: str) -> str:
    return SPINNER_LINE.sub("", ANSI_ESCAPE.sub("", output)).replace("\r", "").strip()


def tail(text: str, limit: int = MAX_DETAILS_CHARS) -> str:
    if len(text) <= limit:
        return text
    return "[...truncated...]\n" + text[-limit:]


def first_int(pattern: str, text: str, default: int = 0) -> int:
    match = re.search(pattern, text)
    return int(match[1].replace(",", "")) if match else default


def plural(count: int, singular: str, plural_form: str) -> str:
    return f"{count} {singular if count == 1 else plural_form}"


def matching_lines(pattern: str, text: str) -> str:
    return "\n".join(line for line in text.splitlines() if re.search(pattern, line))


def analyze_ruff_check(fragment: Fragment) -> Finding:
    output = clean(fragment.output)
    if fragment.passed:
        return Finding(Status.OK, "No lint issues")
    count = first_int(r"Found (\d+) error", output)
    summary = plural(count, "lint issue", "lint issues") if count else "Lint failed"
    return Finding(Status.FAILED, summary, tail(output))


def analyze_ruff_format(fragment: Fragment) -> Finding:
    output = clean(fragment.output)
    if fragment.passed:
        return Finding(Status.OK, "Formatting is correct")
    count = len(re.findall(r"^Would reformat: ", output, re.MULTILINE))
    summary = (
        f"{plural(count, 'file', 'files')} need formatting (`make fmt`)"
        if count
        else "Format check failed"
    )
    return Finding(Status.FAILED, summary, tail(output))


def analyze_mypy(fragment: Fragment) -> Finding:
    output = clean(fragment.output)
    if fragment.passed:
        files = first_int(r"no issues found in (\d+) source file", output)
        return Finding(Status.OK, f"No type errors ({plural(files, 'file', 'files')})")
    errors = first_int(r"Found (\d+) error", output)
    summary = plural(errors, "type error", "type errors") if errors else "mypy failed"
    return Finding(Status.FAILED, summary, tail(output))


def analyze_bandit(fragment: Fragment) -> Finding:
    output = clean(fragment.output)
    if fragment.passed:
        return Finding(Status.OK, "No medium or high severity issues")
    issues = len(re.findall(r">> Issue:", output))
    summary = (
        plural(issues, "security issue", "security issues")
        if issues
        else "bandit failed"
    )
    return Finding(Status.FAILED, summary, tail(output))


def analyze_vulture(fragment: Fragment) -> Finding:
    output = clean(fragment.output)
    if fragment.passed and not output:
        return Finding(Status.OK, "No dead code detected")
    lines = [line for line in output.splitlines() if line.strip()]
    return Finding(
        Status.FAILED,
        plural(len(lines), "dead code item", "dead code items"),
        tail(output),
    )


def analyze_xenon(fragment: Fragment) -> Finding:
    output = clean(fragment.output)
    if fragment.passed:
        return Finding(Status.OK, "Complexity within the thresholds")
    blocks = len(re.findall(r"^ERROR:xenon:", output, re.MULTILINE))
    summary = (
        plural(blocks, "complexity violation", "complexity violations")
        if blocks
        else "Complexity above the thresholds"
    )
    return Finding(Status.FAILED, summary, tail(output))


CONTRACTS_BLOCK = re.compile(
    r"dependencies\.\n-+\n(?P<body>.*?)\n\s*Contracts: ", re.DOTALL
)
CONTRACT_ENTRY = re.compile(
    r"(?P<name>\S.*?) (?P<state>KEPT|BROKEN)"
    r"(?: \((?P<ignored>\d+) ignored imports?\))?(?=\s|$)"
)


def parse_contracts(output: str) -> list[tuple[str, str, int]]:
    block = CONTRACTS_BLOCK.search(output)
    if block is None:
        return []
    # import-linter wraps long contract names over several lines.
    flat = " ".join(line.strip() for line in block["body"].splitlines() if line.strip())
    return [
        (match["name"], match["state"], int(match["ignored"] or 0))
        for match in CONTRACT_ENTRY.finditer(flat)
    ]


def analyze_import_linter(fragment: Fragment) -> Finding:
    output = clean(fragment.output)
    contracts = parse_contracts(output)
    if not contracts:
        return Finding(
            Status.FAILED,
            "import-linter failed before evaluating the contracts",
            tail(output),
        )
    kept = sum(state == "KEPT" for _, state, _ in contracts)
    ignored = sum(count for _, _, count in contracts)
    baseline = (
        f" ({plural(ignored, 'ignored import', 'ignored imports')} in the baseline)"
    )
    listing = "\n".join(
        f"{state:6} {name}" + (f" ({count} ignored)" if count else "")
        for name, state, count in contracts
    )
    broken = [name for name, state, _ in contracts if state == "BROKEN"]
    if not broken and fragment.passed:
        summary = plural(kept, "contract kept", "contracts kept") + (
            baseline if ignored else ""
        )
        return Finding(Status.OK, summary, listing, "Contracts")
    marker = output.find("Broken contracts")
    violation = output[marker:] if marker >= 0 else output
    summary = f"{kept} kept, {len(broken)} broken: " + "; ".join(
        f"**{name}**" for name in broken
    )
    return Finding(
        Status.FAILED,
        summary,
        tail(violation),
        "Broken contracts and violating imports",
    )


def analyze_catalogue(fragment: Fragment) -> Finding:
    output = clean(fragment.output)
    messages = first_int(r"up to date \((\d+) messages?\)", output)
    if fragment.passed:
        return Finding(
            Status.OK,
            f"Catalogue matches the code ({plural(messages, 'message', 'messages')})",
        )
    return Finding(
        Status.FAILED, "The catalogue is behind the code (`make i18n`)", tail(output)
    )


COVERAGE_ROW = re.compile(
    r"^(?P<name>\S+)\s+\d+\s+\d+\s+(?:\d+\s+\d+\s+)?(?P<cover>\d+)%"
)


def least_covered(output: str) -> str:
    lines = output.splitlines()
    header = next((line for line in lines if line.startswith("Name ")), None)
    total = next((line for line in lines if line.startswith("TOTAL")), None)
    if header is None or total is None:
        return ""
    rows = [
        (int(match["cover"]), line)
        for line in lines
        if (match := COVERAGE_ROW.match(line)) and not line.startswith("TOTAL")
    ]
    lowest = [
        line for cover, line in sorted(rows, key=lambda row: row[0]) if cover < 100
    ]
    listed = lowest[:MAX_COVERAGE_ROWS]
    hidden = len(lowest) - len(listed)
    footer = [f"... and {hidden} more files below 100%"] if hidden > 0 else []
    return "\n".join([header, *listed, *footer, total])


def coverage_of(output: str) -> str:
    total = re.search(r"^TOTAL\s.*?(\d+(?:\.\d+)?)%\s*$", output, re.MULTILINE)
    return f"{float(total[1]):.0f}%" if total else "n/a"


def pytest_summary_line(output: str) -> str:
    return next(
        (
            line
            for line in reversed(output.splitlines())
            if re.search(r" in [\d.]+s", line)
            and re.search(r"\d+ (passed|failed|error)", line)
        ),
        "",
    )


def analyze_pytest(fragment: Fragment) -> Finding:
    output = clean(fragment.output)
    summary_line = pytest_summary_line(output)
    if not summary_line:
        return Finding(Status.FAILED, "pytest failed before finishing", tail(output))
    passed = first_int(r"(\d+) passed", summary_line)
    failed = first_int(r"(\d+) failed", summary_line) + first_int(
        r"(\d+) errors?", summary_line
    )
    skipped = first_int(r"(\d+) skipped", summary_line)
    counts = f"{passed} passed, {failed} failed" + (
        f", {skipped} skipped" if skipped else ""
    )
    coverage = coverage_of(output)
    if failed or not fragment.passed:
        failures = matching_lines(r"^(FAILED|ERROR) ", output) or output
        return Finding(
            Status.FAILED, f"{counts} · coverage {coverage}", tail(failures), "Failures"
        )
    return Finding(
        Status.OK,
        f"{counts} · coverage {coverage}",
        least_covered(output),
        "Least covered files",
    )


SMOKE_OK = re.compile(
    r"^\s+ok\s+(?P<name>[\w-]+): .*\((?P<seconds>[\d.]+) s\)$", re.MULTILINE
)
SMOKE_FAIL = re.compile(r"^FAIL (?P<name>[\w-]+): (?P<reason>.+)$", re.MULTILINE)
SMOKE_STACKED = re.compile(r"^\s+info\s+pipeline: (?P<text>.+)$", re.MULTILINE)
SMOKE_READY = "smoke: ready"


def analyze_smoke(fragment: Fragment) -> Finding:
    output = clean(fragment.output)
    steps = [(m["name"], float(m["seconds"])) for m in SMOKE_OK.finditer(output)]
    timings = " · ".join(f"{name} {seconds:.1f}s" for name, seconds in steps)
    stacked = SMOKE_STACKED.search(output)
    if fragment.passed and SMOKE_READY in output:
        parts = [plural(len(steps), "step ready", "steps ready"), timings]
        if stacked:
            parts.append(stacked["text"])
        return Finding(Status.OK, " · ".join(part for part in parts if part))
    failure = SMOKE_FAIL.search(output)
    where = (
        f"`{failure['name']}` failed: {failure['reason']}"
        if failure
        else "did not finish"
    )
    done = f" (after {timings})" if timings else ""
    logs = output[failure.start() :] if failure else output
    return Finding(Status.FAILED, f"{where}{done}", tail(logs), "Step output")


def analyze_wheel_build(fragment: Fragment) -> Finding:
    output = clean(fragment.output)
    built = re.search(r"Successfully built (\S+\.whl)", output)
    if fragment.passed:
        return Finding(Status.OK, f"Built `{built[1]}`" if built else "Built")
    return Finding(Status.FAILED, "The wheel did not build", tail(output))


def analyze_wheel_data(fragment: Fragment) -> Finding:
    output = clean(fragment.output)
    if fragment.passed:
        files = first_int(r"(\d+) files, package data present", output)
        return Finding(
            Status.OK, f"Package data present ({plural(files, 'file', 'files')})"
        )
    missing = matching_lines(r"missing from the wheel", output)
    return Finding(
        Status.FAILED, "Package data missing from the wheel", tail(missing or output)
    )


@dataclass(frozen=True)
class Survivor:
    file: str
    function: str
    status: str
    name: str
    diff: str = ""


@dataclass(frozen=True)
class FileScore:
    file: str
    total: int
    killed: int
    score: float


@dataclass(frozen=True)
class MutationOutcome:
    skipped: bool
    reason: str
    score: float
    min_score: float
    killed: int
    total: int
    files: list[FileScore]
    survivors: list[Survivor]

    @classmethod
    def parse(cls, output: str) -> MutationOutcome | None:
        line = next(
            (
                line
                for line in output.splitlines()
                if line.startswith(MUTATION_RESULT_PREFIX)
            ),
            None,
        )
        if line is None:
            return None
        data = json.loads(line.removeprefix(MUTATION_RESULT_PREFIX))
        return cls(
            skipped=bool(data["skipped"]),
            reason=str(data.get("reason", "")),
            score=float(data["score"]),
            min_score=float(data["min_score"]),
            killed=int(data["killed"]),
            total=int(data["total"]),
            files=[FileScore(**item) for item in data.get("files", [])],
            survivors=[Survivor(**item) for item in data["survivors"]],
        )


def mutation_details(outcome: MutationOutcome) -> str:
    lines = ["| File | Mutants | Killed | Score |", "|---|---:|---:|---:|"]
    lines += [
        f"| `{f.file}` | {f.total} | {f.killed} | {f.score:.1f}% |"
        for f in outcome.files
    ]
    if not outcome.survivors:
        return "\n".join(lines)
    listed = outcome.survivors[:MAX_SURVIVORS_LISTED]
    lines += [
        "",
        "**Survivors in the changed code**",
        "",
        "| File | Function | Status |",
    ]
    lines += ["|---|---|---|"]
    lines += [f"| `{s.file}` | `{s.function}` | {s.status} |" for s in listed]
    hidden = len(outcome.survivors) - len(listed)
    if hidden > 0:
        lines.append(f"\n... and {hidden} more: see the `mutation` job summary.")
    for survivor in (s for s in listed if s.diff):
        lines += [
            "",
            f"`{survivor.name}`",
            "```diff",
            survivor.diff.replace("```", "'''"),
            "```",
        ]
    lines += [
        "",
        "Reproduce locally with `make mutation-changed`, "
        "then `.venv/bin/mutmut show <name>`.",
    ]
    return "\n".join(lines)


def analyze_mutation(fragment: Fragment) -> Finding:
    output = clean(fragment.output)
    outcome = MutationOutcome.parse(output)
    if outcome is None:
        return Finding(
            Status.FAILED, "mutmut failed before producing a score", tail(output)
        )
    if outcome.skipped:
        return Finding(Status.SKIPPED, outcome.reason or "Nothing to mutate")
    survivors = len(outcome.survivors)
    summary = (
        f"score {outcome.score:.1f}% (ratchet {outcome.min_score:.0f}%) · "
        f"{outcome.killed} of {outcome.total} mutants killed · "
        f"{plural(survivors, 'survivor', 'survivors')} in "
        f"{plural(len(outcome.files), 'changed file', 'changed files')}"
    )
    status = Status.OK if outcome.score >= outcome.min_score else Status.FAILED
    return Finding(
        status,
        summary,
        mutation_details(outcome),
        "Changed files and survivors",
        DetailsFormat.MARKDOWN,
    )


ANALYZERS: dict[Analysis, Callable[[Fragment], Finding]] = {
    Analysis.RUFF_CHECK: analyze_ruff_check,
    Analysis.RUFF_FORMAT: analyze_ruff_format,
    Analysis.MYPY: analyze_mypy,
    Analysis.BANDIT: analyze_bandit,
    Analysis.VULTURE: analyze_vulture,
    Analysis.XENON: analyze_xenon,
    Analysis.IMPORT_LINTER: analyze_import_linter,
    Analysis.CATALOGUE: analyze_catalogue,
    Analysis.PYTEST: analyze_pytest,
    Analysis.SMOKE: analyze_smoke,
    Analysis.WHEEL_BUILD: analyze_wheel_build,
    Analysis.WHEEL_DATA: analyze_wheel_data,
    Analysis.MUTATION: analyze_mutation,
}


def missing_finding(job: str, job_results: dict[str, str]) -> Finding:
    result = job_results.get(job)
    if result is None:
        return Finding(Status.SKIPPED, f"Job `{job}` did not run: analysis not run")
    if result in ("cancelled", "skipped"):
        return Finding(Status.SKIPPED, f"Job `{job}` was {result}: analysis not run")
    if result == "success":
        return Finding(
            Status.WARNING, f"Job `{job}` passed but its result was not uploaded"
        )
    return Finding(
        Status.FAILED, f"Job `{job}` ended as `{result}` before running this analysis"
    )


def analyze(fragment: Fragment) -> Finding:
    try:
        return ANALYZERS[fragment.analysis](fragment)
    except (ValueError, KeyError, TypeError, IndexError) as error:
        logger.warning("could not analyse %s: %s", fragment.analysis, error)
        status = Status.OK if fragment.passed else Status.FAILED
        verdict = (
            "passed" if fragment.passed else f"failed (exit code {fragment.exit_code})"
        )
        return Finding(
            status,
            f"{verdict}; the output could not be summarised",
            tail(clean(fragment.output)),
        )


def platform_order(platform: str) -> tuple[bool, str]:
    return (platform != PRIMARY_PLATFORM, platform)


def combine(
    check: Check,
    findings: dict[str, tuple[Finding, float]],
    expected: list[str],
    job_results: dict[str, str],
) -> CheckResult:
    for platform in expected:
        if platform not in findings:
            findings[platform] = (missing_finding(check.job, job_results), 0.0)
    ordered = sorted(findings, key=platform_order)
    rows = tuple((p, findings[p][0].status, findings[p][1]) for p in ordered)
    status = worst([finding.status for finding, _ in findings.values()])
    shown = next(p for p in ordered if findings[p][0].status == status)
    finding, seconds = findings[shown]
    if len(ordered) < 2:
        return CheckResult(check, finding, seconds, rows)
    matching = [p for p in ordered if findings[p][0].status == status]
    if status in (Status.OK, Status.SKIPPED) or len(matching) == len(ordered):
        scope = f"{len(matching)}/{len(ordered)} platforms"
    else:
        scope = f"on {', '.join(matching)}"
    title = f"{finding.details_title} ({shown})"
    combined = Finding(
        status,
        f"{finding.summary} · {scope}",
        finding.details,
        title,
        finding.details_format,
    )
    return CheckResult(check, combined, seconds, rows)


JOB_OF = {check.analysis: check.job for section in SECTIONS for check in section.checks}


def platforms_of(fragments: list[Fragment], job: str) -> list[str]:
    return sorted({f.platform for f in fragments if JOB_OF[f.analysis] == job})


def evaluate(
    section: Section, fragments: list[Fragment], job_results: dict[str, str]
) -> SectionResult:
    results: list[CheckResult] = []
    for check in section.checks:
        mine = [f for f in fragments if f.analysis == check.analysis]
        if not mine:
            results.append(CheckResult(check, missing_finding(check.job, job_results)))
            continue
        findings = {f.platform: (analyze(f), f.seconds) for f in mine}
        expected = platforms_of(fragments, check.job)
        results.append(combine(check, findings, expected, job_results))
    return SectionResult(section, worst([r.finding.status for r in results]), results)


def fence(text: str) -> str:
    return "```text\n" + text.replace("```", "'''") + "\n```"


def render_details(finding: Finding, limit: int) -> list[str]:
    if not finding.details or limit <= 0:
        return []
    if (
        finding.details_format == DetailsFormat.MARKDOWN
        and len(finding.details) <= limit
    ):
        body = finding.details
    else:
        body = fence(tail(finding.details, limit))
    return [
        "",
        f"<details><summary>{finding.details_title}</summary>",
        "",
        body,
        "",
        "</details>",
    ]


def duration(seconds: float) -> str:
    return f" _({seconds:.1f}s)_" if seconds >= 0.1 else ""


def render_section(result: SectionResult, limit: int) -> list[str]:
    lines = ["", f"### {result.status} {result.section.title}", ""]
    multiple = len(result.results) > 1
    for item in result.results:
        name = item.check.label or item.check.analysis
        label = f"- {item.finding.status} **{name}**: " if multiple else ""
        lines.append(f"{label}{item.finding.summary}{duration(item.seconds)}")
        lines += render_details(item.finding, limit)
    return lines


def one_line(result: SectionResult) -> str:
    if len(result.results) == 1:
        return result.results[0].finding.summary.replace("|", "\\|")
    return " · ".join(
        f"{item.check.label or item.check.analysis}: {item.finding.status}"
        for item in result.results
    )


def platform_matrix(results: list[SectionResult]) -> list[str]:
    columns = [
        item for result in results for item in result.results if len(item.platforms) > 1
    ]
    if not columns:
        return []
    platforms = sorted({p for item in columns for p, _, _ in item.platforms})
    header = "| Platform | " + " | ".join(item.check.analysis.value for item in columns)
    rows = []
    for platform in platforms:
        cells = []
        for item in columns:
            cell = next(
                (f"{s} {t:.0f}s" for p, s, t in item.platforms if p == platform), "–"
            )
            cells.append(cell)
        rows.append(f"| {platform} | " + " | ".join(cells) + " |")
    return [
        "<details><summary>By platform (status and duration of each check)</summary>",
        "",
        header + " |",
        "|---|" + "---|" * len(columns),
        *rows,
        "",
        "</details>",
    ]


def verdict(results: list[SectionResult]) -> str:
    failed = sum(r.status == Status.FAILED for r in results)
    warnings = sum(r.status == Status.WARNING for r in results)
    skipped = sum(r.status == Status.SKIPPED for r in results)
    if failed:
        return f"❌ **Failed**: {plural(failed, 'section failed', 'sections failed')}"
    if warnings:
        return (
            "⚠️ **Passed with warnings**: "
            f"{plural(warnings, 'section', 'sections')} with warnings"
        )
    extra = f" ({plural(skipped, 'section', 'sections')} not run)" if skipped else ""
    return "✅ **All good**" + extra


def render_with_limit(results: list[SectionResult], footer: str, limit: int) -> str:
    lines = [MARKER, "## 🔍 Quality Report", "", verdict(results), ""]
    lines += ["| Analysis | Status | Result |", "| --- | --- | --- |"]
    lines += [f"| {r.section.title} | {r.status} | {one_line(r)} |" for r in results]
    matrix = platform_matrix(results)
    if matrix:
        lines += ["", *matrix]
    for result in results:
        lines += render_section(result, limit)
    if limit < MAX_DETAILS_CHARS:
        lines += [
            "",
            "_Details were trimmed to fit in a comment: see the summary of each job._",
        ]
    lines += [
        "",
        "---",
        "This report only informs: each CI job is still the gate for its own checks.",
    ]
    if footer:
        lines.append(footer)
    return "\n".join(lines) + "\n"


def render(
    fragments: list[Fragment], job_results: dict[str, str], footer: str = ""
) -> str:
    results = [evaluate(section, fragments, job_results) for section in SECTIONS]
    report = render_with_limit(results, footer, MAX_DETAILS_CHARS)
    for limit in REDUCED_DETAILS_CHARS:
        if len(report) <= MAX_REPORT_CHARS:
            break
        report = render_with_limit(results, footer, limit)
    return report


def load_fragments(directory: Path) -> list[Fragment]:
    fragments: list[Fragment] = []
    for path in sorted(directory.rglob("*.json")):
        try:
            fragments.append(Fragment.read(path))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as error:
            logger.warning("ignoring invalid fragment %s: %s", path, error)
    return fragments


def parse_job_results(needs_json: str) -> dict[str, str]:
    if not needs_json:
        return {}
    needs = json.loads(needs_json)
    return {job: str(data.get("result", "unknown")) for job, data in needs.items()}


def run_command(analysis: Analysis, command: list[str], directory: Path) -> int:
    platform = os.environ.get(PLATFORM_ENV, "")
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            env={**os.environ, **CHILD_ENV},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        message = f"could not run {command[0]}: {error}"
        logger.error(message)
        Fragment(analysis, 127, message, platform).write(directory)
        return 127
    lines: list[str] = []
    if process.stdout is not None:
        for line in process.stdout:
            sys.stdout.write(line)
            lines.append(line)
    exit_code = process.wait()
    # Absolute checkout paths only add noise, and differ between runners.
    output = "".join(lines).replace(f"{Path.cwd()}{os.sep}", "")
    elapsed = time.monotonic() - started
    Fragment(analysis, exit_code, output, platform, elapsed).write(directory)
    return exit_code


def footer_from_environment() -> str:
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not repository or not run_id:
        return ""
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    sha = os.environ.get("QUALITY_REPORT_SHA", os.environ.get("GITHUB_SHA", ""))[:7]
    commit = f"commit `{sha}` · " if sha else ""
    return f"{commit}[CI run]({server}/{repository}/actions/runs/{run_id})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consolidated CI Quality Report.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run a command and store its output")
    run.add_argument("analysis", type=Analysis)
    run.add_argument("--dir", type=Path, default=DEFAULT_DIRECTORY)
    render_parser = commands.add_parser("render", help="build the Markdown report")
    render_parser.add_argument("--dir", type=Path, default=DEFAULT_DIRECTORY)
    render_parser.add_argument("--needs-json", default="")
    render_parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # A Windows console is not UTF-8; a tool's output must not crash the relay.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    command: list[str] = []
    if "--" in argv:
        separator = argv.index("--")
        argv, command = argv[:separator], argv[separator + 1 :]
    args = build_parser().parse_args(argv)
    if args.command == "run":
        if not command:
            logger.error("missing command after --")
            return 2
        return run_command(args.analysis, command, args.dir)
    report = render(
        load_fragments(args.dir),
        parse_job_results(args.needs_json),
        footer_from_environment(),
    )
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
