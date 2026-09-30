"""Mutation testing around mutmut, over the scope in [tool.mutmut].

    python scripts/mutation.py changed --base origin/main   # what a PR runs
    python scripts/mutation.py report                       # after `mutmut run`

`changed` mutates only the functions of the in-scope modules that the branch
touched and fails below MUTATION_MIN_SCORE, the ratchet. `report` turns a full
run into Markdown: the score by package, by file, and the survivors. Needs
mutmut, which needs `fork`: Linux and macOS, not Windows.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import io
import json
import logging
import os
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

MUTANTS_DIR = Path("mutants")
PYPROJECT = Path("pyproject.toml")
# The ratchet for pull requests: a floor below the measured baseline, raised by
# the owner over time. The MUTATION_MIN_SCORE environment variable (a
# repository variable in CI) overrides it. Baseline measured in September 2026:
# 62.9% over the whole scope, 47.1% for the weakest file (platform_align.py).
DEFAULT_MIN_SCORE = 45.0
MIN_SCORE_ENV = "MUTATION_MIN_SCORE"
# Read by scripts/quality_report.py: keep the prefix in sync.
RESULT_PREFIX = "MUTATION_RESULT: "
# Printed by `mutmut run` when no mutant matches the names it was given.
NOTHING_MATCHED = "Filtered for specific mutants, but nothing matches"
MAX_REPORTED_DIFFS = 20
HUNK_HEADER = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@", re.MULTILINE
)
MUTANT_NAME = re.compile(
    r"^(?P<module>.+?)\.(?P<function>xǁ[^.]+|x_[^.]+)__mutmut_\d+$"
)


class Outcome(StrEnum):
    KILLED = "killed"
    SURVIVED = "survived"
    NO_TESTS = "no tests"
    TIMEOUT = "timeout"
    OTHER = "other"


# The meaning mutmut gives these exit codes (mutmut/stats.py). A timeout or a
# type-check failure means the tests noticed the mutant: both count as killed.
OUTCOME_BY_EXIT_CODE: dict[int | None, Outcome] = {
    0: Outcome.SURVIVED,
    1: Outcome.KILLED,
    3: Outcome.KILLED,
    37: Outcome.KILLED,
    5: Outcome.NO_TESTS,
    33: Outcome.NO_TESTS,
    24: Outcome.TIMEOUT,
    -24: Outcome.TIMEOUT,
    36: Outcome.TIMEOUT,
    152: Outcome.TIMEOUT,
    255: Outcome.TIMEOUT,
}
UNDETECTED = (Outcome.SURVIVED, Outcome.NO_TESTS, Outcome.OTHER)


@dataclass
class Tally:
    counts: dict[Outcome, int] = field(default_factory=lambda: defaultdict(int))
    survivors: list[str] = field(default_factory=list)
    outcomes: dict[str, Outcome] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def detected(self) -> int:
        return self.counts[Outcome.KILLED] + self.counts[Outcome.TIMEOUT]

    # A mutant no test runs is a survivor: nothing would catch that change.
    @property
    def score(self) -> float:
        return 100 * self.detected / self.total if self.total else 100.0

    def add(self, name: str, outcome: Outcome) -> None:
        self.counts[outcome] += 1
        self.outcomes[name] = outcome
        if outcome in UNDETECTED:
            self.survivors.append(name)


@dataclass(frozen=True)
class Scope:
    source_paths: tuple[str, ...]
    only_mutate: tuple[str, ...]

    def contains(self, file: str) -> bool:
        if not file.endswith(".py") or file.endswith("__init__.py"):
            return False
        patterns = self.only_mutate or tuple(
            f"{p.rstrip('/')}/*" for p in self.source_paths
        )
        return any(fnmatch.fnmatch(file, pattern) for pattern in patterns)

    def module_name(self, file: str) -> str:
        for path in self.source_paths:
            prefix = path.rstrip("/") + "/"
            if file.startswith(prefix):
                file = file.removeprefix(prefix)
                break
        return file.removesuffix(".py").replace("/", ".")


def load_scope(pyproject: Path = PYPROJECT) -> Scope:
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["mutmut"]
    return Scope(
        source_paths=tuple(config["source_paths"]),
        only_mutate=tuple(config.get("only_mutate", ())),
    )


def file_of(meta: Path, mutants_dir: Path) -> str:
    return meta.relative_to(mutants_dir).as_posix().removesuffix(".meta")


def package_of(file: str) -> str:
    return file.rpartition("/")[0] or file


def matches(name: str, patterns: list[str] | None) -> bool:
    return patterns is None or any(fnmatch.fnmatchcase(name, p) for p in patterns)


def collect(mutants_dir: Path, patterns: list[str] | None = None) -> dict[str, Tally]:
    tallies: dict[str, Tally] = defaultdict(Tally)
    for meta in sorted(mutants_dir.rglob("*.py.meta")):
        data = json.loads(meta.read_text(encoding="utf-8"))
        exit_codes: dict[str, int | None] = data["exit_code_by_key"]
        for name, code in exit_codes.items():
            if not matches(name, patterns):
                continue
            outcome = OUTCOME_BY_EXIT_CODE.get(code, Outcome.OTHER)
            tallies[file_of(meta, mutants_dir)].add(name, outcome)
    return dict(tallies)


def merge(tallies: dict[str, Tally], key: str) -> dict[str, Tally]:
    merged: dict[str, Tally] = defaultdict(Tally)
    for file, tally in tallies.items():
        target = merged[key or package_of(file)]
        for name, outcome in tally.outcomes.items():
            target.add(name, outcome)
    return dict(merged)


def overall(tallies: dict[str, Tally]) -> Tally:
    return merge(tallies, "all").get("all", Tally())


def table(title: str, tallies: dict[str, Tally]) -> list[str]:
    rows = [
        f"| `{name}` | {t.total} | {t.detected} | {t.counts[Outcome.SURVIVED]} "
        f"| {t.counts[Outcome.NO_TESTS]} | {t.score:.1f}% |"
        for name, t in sorted(
            tallies.items(), key=lambda item: (item[1].score, item[0])
        )
    ]
    header = "| Module | Mutants | Killed | Survived | No tests | Score |"
    return [f"### {title}", "", header, "|---|---:|---:|---:|---:|---:|", *rows, ""]


def diff_of(name: str) -> str | None:
    # Imported here so that the rest of this module, and its tests, run where
    # mutmut is not installed (Windows).
    try:
        from mutmut.mutation.diff_apply import get_diff_for_mutant
    except ImportError:
        return None
    try:
        return str(get_diff_for_mutant(name)).strip()
    except (OSError, ValueError, KeyError, AssertionError) as error:
        logger.warning("could not diff %s: %s", name, error)
        return None


def survivors_section(tallies: dict[str, Tally], limit: int) -> list[str]:
    lines = ["### Surviving mutants", ""]
    for file, tally in sorted(tallies.items()):
        if not tally.survivors:
            continue
        count = len(tally.survivors)
        lines += [f"<details><summary><code>{file}</code>: {count}</summary>", ""]
        for name in tally.survivors[:limit]:
            diff = diff_of(name)
            lines.append(f"`{name}`")
            if diff:
                lines += ["```diff", diff, "```"]
        if count > limit:
            lines.append(f"... and {count - limit} more (`mutmut results`)")
        lines += ["", "</details>", ""]
    return lines


def full_report(tallies: dict[str, Tally], diffs_per_module: int) -> str:
    total = overall(tallies)
    lines = [
        "## Mutation testing",
        "",
        f"**Score: {total.score:.1f}%** ({total.detected} of {total.total} mutants "
        f"killed; {total.counts[Outcome.SURVIVED]} survived, "
        f"{total.counts[Outcome.NO_TESTS]} without any test).",
        "",
        *table("By package", merge(tallies, "")),
        *table("By file", tallies),
        *survivors_section(tallies, diffs_per_module),
    ]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class FunctionSpan:
    mangled: str
    start: int
    end: int

    def contains(self, line: int) -> bool:
        return self.start <= line <= self.end


def span_of(node: ast.FunctionDef | ast.AsyncFunctionDef, mangled: str) -> FunctionSpan:
    start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    return FunctionSpan(mangled, start, node.end_lineno or node.lineno)


# Mirrors how mutmut names what it mutates: top-level functions (x_name) and
# methods of top-level classes (xǁClassǁmethod). A nested function belongs to
# the function around it.
def function_spans(tree: ast.Module) -> list[FunctionSpan]:
    spans: list[FunctionSpan] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            spans.append(span_of(node, f"x_{node.name}"))
        if not isinstance(node, ast.ClassDef):
            continue
        spans += [
            span_of(item, f"xǁ{node.name}ǁ{item.name}")
            for item in node.body
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
    return spans


# Lines whose change cannot alter what a function does.
def neutral_lines(tree: ast.Module, source: str) -> set[int]:
    neutral = {
        number
        for number, text in enumerate(source.splitlines(), start=1)
        if not text.strip() or text.strip().startswith("#")
    }
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            neutral.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return neutral


def targets_for_file(module: str, source: str, lines: set[int]) -> list[str]:
    tree = ast.parse(source)
    spans = function_spans(tree)
    relevant = lines - neutral_lines(tree, source)
    if not relevant:
        return []
    touched = [span for span in spans if any(span.contains(n) for n in relevant)]
    outside = any(not any(span.contains(n) for span in spans) for n in relevant)
    # A change outside every function (a constant, a class attribute) can
    # change any of them.
    if outside or len(touched) == len(spans):
        return [f"{module}.*"]
    return [f"{module}.{span.mangled}__mutmut_*" for span in touched]


def parse_changed_lines(diff: str) -> set[int]:
    lines: set[int] = set()
    for hunk in HUNK_HEADER.finditer(diff):
        start = int(hunk["start"])
        count = int(hunk["count"]) if hunk["count"] is not None else 1
        # A pure deletion (count 0) sits between line `start` and the next.
        lines.update(range(start, start + count) if count else (start, start + 1))
    return lines


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, timeout=60
    ).stdout


def changed_targets(base_ref: str, scope: Scope) -> list[str]:
    diff_range = f"{base_ref}...HEAD"
    files = git("diff", "--name-only", "--diff-filter=AMR", diff_range).splitlines()
    targets: list[str] = []
    for file in sorted(f for f in files if scope.contains(f)):
        lines = parse_changed_lines(git("diff", "-U0", diff_range, "--", file))
        source = Path(file).read_text(encoding="utf-8")
        targets += targets_for_file(scope.module_name(file), source, lines)
    return targets


def function_of(name: str) -> str:
    match = MUTANT_NAME.match(name)
    if not match:
        return name
    function = match["function"]
    if "ǁ" in function:
        return function.removeprefix("xǁ").replace("ǁ", ".")
    return function.removeprefix("x_")


@dataclass(frozen=True)
class Survivor:
    file: str
    function: str
    status: str
    name: str
    diff: str


@dataclass(frozen=True)
class FileScore:
    file: str
    total: int
    killed: int
    score: float


@dataclass(frozen=True)
class ChangedReport:
    tallies: dict[str, Tally]
    min_score: float
    targets: list[str]

    @property
    def total(self) -> Tally:
        return overall(self.tallies)

    @property
    def passed(self) -> bool:
        return self.total.score >= self.min_score

    def survivors(self) -> list[Survivor]:
        items: list[Survivor] = []
        for file, tally in sorted(self.tallies.items()):
            for name in tally.survivors:
                diff = diff_of(name) if len(items) < MAX_REPORTED_DIFFS else None
                status = tally.outcomes[name].value
                items.append(
                    Survivor(file, function_of(name), status, name, diff or "")
                )
        return items

    def to_result_line(self) -> str:
        total = self.total
        return RESULT_PREFIX + json.dumps(
            {
                "skipped": False,
                "reason": "",
                "score": total.score,
                "min_score": self.min_score,
                "killed": total.detected,
                "total": total.total,
                "targets": self.targets,
                "files": [
                    asdict(FileScore(file, t.total, t.detected, t.score))
                    for file, t in sorted(self.tallies.items())
                ],
                "survivors": [asdict(survivor) for survivor in self.survivors()],
            }
        )

    def to_markdown(self) -> str:
        total = self.total
        verdict = "✅ passed" if self.passed else "❌ below the ratchet"
        lines = [
            "## Mutation testing (changed code in scope)",
            "",
            f"**Score: {total.score:.1f}%** (ratchet {self.min_score:.1f}%, "
            f"{verdict}): {total.detected} of {total.total} mutants killed.",
            "",
            *table("By file", self.tallies),
        ]
        names = [name for tally in self.tallies.values() for name in tally.survivors]
        if names:
            lines += ["### Survivors", "", *(f"- `{name}`" for name in names)]
            lines += ["", "Inspect one with `.venv/bin/mutmut show <name>`."]
        return "\n".join(lines) + "\n"


def skipped_result_line(min_score: float, reason: str) -> str:
    return RESULT_PREFIX + json.dumps(
        {
            "skipped": True,
            "reason": reason,
            "score": 100.0,
            "min_score": min_score,
            "killed": 0,
            "total": 0,
            "targets": [],
            "files": [],
            "survivors": [],
        }
    )


def min_score() -> float:
    return float(os.environ.get(MIN_SCORE_ENV) or DEFAULT_MIN_SCORE)


def publish(markdown: str) -> None:
    sys.stdout.write(markdown)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(summary, "a", encoding="utf-8") as handle:
        handle.write(markdown)


def run_mutmut(targets: list[str]) -> tuple[int, str]:
    process = subprocess.Popen(
        [sys.executable, "-m", "mutmut", "run", *targets],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines: list[str] = []
    if process.stdout is not None:
        for line in process.stdout:
            sys.stdout.write(line)
            lines.append(line)
    return process.wait(), "".join(lines)


def skip(reason: str, threshold: float) -> int:
    publish(f"## Mutation testing (changed code in scope)\n\n{reason}\n")
    sys.stdout.write(skipped_result_line(threshold, reason) + "\n")
    return 0


def changed_command(base_ref: str) -> int:
    threshold = min_score()
    targets = changed_targets(base_ref, load_scope())
    if not targets:
        return skip("No function in the mutation scope changed.", threshold)
    sys.stdout.write("Mutating:\n" + "".join(f"  {t}\n" for t in targets))
    sys.stdout.flush()
    exit_code, output = run_mutmut(targets)
    if exit_code != 0 and NOTHING_MATCHED in output:
        return skip("The changed functions produce no mutants.", threshold)
    if exit_code != 0:
        logger.error("mutmut run failed with exit code %s", exit_code)
        return exit_code
    report = ChangedReport(collect(MUTANTS_DIR, targets), threshold, targets)
    publish(report.to_markdown())
    sys.stdout.write(report.to_result_line() + "\n")
    return 0 if report.passed else 1


def report_command(diffs_per_module: int, threshold: float) -> int:
    tallies = collect(MUTANTS_DIR)
    if not tallies:
        logger.error(
            "No mutmut results in %s/: run `make mutation` first.", MUTANTS_DIR
        )
        return 1
    sys.stdout.write(full_report(tallies, diffs_per_module))
    return 0 if overall(tallies).score >= threshold else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mutation testing around mutmut.")
    commands = parser.add_subparsers(dest="command", required=True)
    changed = commands.add_parser("changed", help="mutate what changed against BASE")
    changed.add_argument("--base", default="origin/main")
    report = commands.add_parser("report", help="summarise a full run as Markdown")
    report.add_argument("--diffs-per-module", type=int, default=10)
    report.add_argument("--min-score", type=float, default=0.0)
    return parser


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    if args.command == "changed":
        return changed_command(args.base)
    return report_command(args.diffs_per_module, args.min_score)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
