from __future__ import annotations

import json
import subprocess

import pytest

from scripts import mutation, quality_report

SOURCE = """import math

from astrodoro.drivers.base import Bayer

LIMIT = 3


class Solver:
    def __init__(self, scale: float) -> None:
        self.scale = scale

    def solve(self, value: float) -> float:
        if value > LIMIT:
            return math.inf
        return value * self.scale


@cache
def helper(value: int) -> int:
    return value + 1
"""
MODULE = "astrodoro.core.solver"
FILE = "src/astrodoro/core/solver.py"
SCOPE = mutation.Scope(
    ("src/",), ("src/astrodoro/core/solver.py", "src/astrodoro/pointing/*")
)


def _write_meta(root, file, exit_codes):
    path = root / f"{file}.meta"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"exit_code_by_key": exit_codes}))


def test_changed_lines_come_from_the_new_side_of_each_hunk():
    diff = "@@ -10,2 +12,3 @@ def x\n+a\n@@ -40 +44 @@\n-b\n+c\n"
    assert mutation.parse_changed_lines(diff) == {12, 13, 14, 44}


def test_a_deletion_marks_both_of_its_neighbours():
    assert mutation.parse_changed_lines("@@ -5,2 +4,0 @@\n-a\n-b\n") == {4, 5}


def test_a_changed_method_targets_that_method_alone():
    assert mutation.targets_for_file(MODULE, SOURCE, {14}) == [
        f"{MODULE}.xǁSolverǁsolve__mutmut_*"
    ]


def test_a_decorator_belongs_to_its_function():
    assert mutation.targets_for_file(MODULE, SOURCE, {18}) == [
        f"{MODULE}.x_helper__mutmut_*"
    ]


def test_a_changed_constant_targets_the_whole_module():
    assert mutation.targets_for_file(MODULE, SOURCE, {5, 14}) == [f"{MODULE}.*"]


def test_every_function_changed_targets_the_whole_module():
    assert mutation.targets_for_file(MODULE, SOURCE, {10, 13, 20}) == [f"{MODULE}.*"]


def test_imports_blank_lines_and_comments_target_nothing():
    assert mutation.targets_for_file(MODULE, SOURCE, {1, 2, 3, 4}) == []


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        ("src/astrodoro/core/solver.py", True),
        ("src/astrodoro/pointing/pushto.py", True),
        ("src/astrodoro/pointing/__init__.py", False),
        ("src/astrodoro/core/stacker.py", False),
        ("src/astrodoro/ui/main.py", False),
        ("src/astrodoro/pointing/README.md", False),
    ],
)
def test_the_scope_is_the_python_files_only_mutate_names(file, expected):
    assert SCOPE.contains(file) is expected


def test_without_only_mutate_the_scope_is_every_source_path():
    assert mutation.Scope(("src/",), ()).contains("src/astrodoro/ui/main.py")


def test_module_names_drop_the_source_path_like_mutmut_does():
    assert SCOPE.module_name(FILE) == MODULE


def test_the_scope_is_read_from_pyproject(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.mutmut]\nsource_paths = ["src/"]\nonly_mutate = ["src/x/*"]\n'
    )
    assert mutation.load_scope(pyproject) == mutation.Scope(("src/",), ("src/x/*",))


def test_the_project_scope_leaves_the_front_ends_and_drivers_out():
    scope = mutation.load_scope()
    assert scope.contains("src/astrodoro/core/calibration.py")
    for file in (
        "src/astrodoro/ui/main.py",
        "src/astrodoro/cli/stack.py",
        "src/astrodoro/drivers/svbony/camera.py",
    ):
        assert not scope.contains(file)


@pytest.fixture
def mutants(tmp_path):
    _write_meta(
        tmp_path,
        FILE,
        {
            f"{MODULE}.xǁSolverǁsolve__mutmut_1": 1,
            f"{MODULE}.xǁSolverǁsolve__mutmut_2": 0,
            f"{MODULE}.xǁSolverǁ__init____mutmut_1": 33,
            f"{MODULE}.x_helper__mutmut_1": 36,
        },
    )
    _write_meta(
        tmp_path,
        "src/astrodoro/pointing/pushto.py",
        {"astrodoro.pointing.pushto.x_guide__mutmut_1": None},
    )
    return tmp_path


def test_timeouts_count_as_killed_and_untested_mutants_as_survivors(mutants):
    tally = mutation.collect(mutants)[FILE]
    assert (tally.total, tally.detected, tally.score) == (4, 2, 50.0)
    assert tally.counts[mutation.Outcome.NO_TESTS] == 1
    assert len(tally.survivors) == 2


def test_collect_keeps_only_the_targeted_mutants(mutants):
    tallies = mutation.collect(mutants, [f"{MODULE}.xǁSolverǁsolve__mutmut_*"])
    assert list(tallies) == [FILE]
    assert tallies[FILE].total == 2


def test_an_unchecked_mutant_is_not_killed(mutants):
    tally = mutation.collect(mutants)["src/astrodoro/pointing/pushto.py"]
    assert (tally.counts[mutation.Outcome.OTHER], tally.score) == (1, 0.0)


def test_files_are_grouped_by_package(mutants):
    packages = mutation.merge(mutation.collect(mutants), "")
    assert set(packages) == {"src/astrodoro/core", "src/astrodoro/pointing"}
    assert mutation.overall(mutation.collect(mutants)).total == 5


def test_the_full_report_lists_scores_and_survivors(mutants, monkeypatch):
    monkeypatch.setattr(mutation, "diff_of", lambda _: "-a\n+b")
    report = mutation.full_report(mutation.collect(mutants), diffs_per_module=1)
    assert report.startswith("## Mutation testing\n\n**Score: 40.0%**")
    assert "### By package" in report
    assert "... and 1 more" in report


def test_mutant_names_read_back_as_functions():
    assert mutation.function_of(f"{MODULE}.xǁSolverǁsolve__mutmut_3") == "Solver.solve"
    assert mutation.function_of(f"{MODULE}.x_helper__mutmut_1") == "helper"
    assert mutation.function_of("weird") == "weird"


@pytest.fixture
def changed_report(monkeypatch):
    monkeypatch.setattr(mutation, "diff_of", lambda _: "-    a\n+    b")
    tally = mutation.Tally()
    tally.add(f"{MODULE}.xǁSolverǁsolve__mutmut_1", mutation.Outcome.KILLED)
    tally.add(f"{MODULE}.xǁSolverǁsolve__mutmut_2", mutation.Outcome.SURVIVED)
    return mutation.ChangedReport({FILE: tally}, 60.0, [f"{MODULE}.*"])


def test_a_score_below_the_ratchet_fails(changed_report):
    assert (changed_report.total.score, changed_report.passed) == (50.0, False)


def test_the_changed_report_lists_its_survivors(changed_report):
    markdown = changed_report.to_markdown()
    assert "**Score: 50.0%** (ratchet 60.0%, ❌ below the ratchet)" in markdown
    assert f"- `{MODULE}.xǁSolverǁsolve__mutmut_2`" in markdown


def test_the_result_line_is_what_the_quality_report_reads(changed_report):
    line = changed_report.to_result_line()
    fragment = quality_report.Fragment(
        quality_report.Analysis.MUTATION, 1, f"x\n{line}\n"
    )
    finding = quality_report.analyze_mutation(fragment)
    assert finding.status == quality_report.Status.FAILED
    assert finding.summary.startswith(
        "score 50.0% (ratchet 60%) · 1 of 2 mutants killed"
    )
    assert f"| `{FILE}` | `Solver.solve` | 1 survived |" in finding.details
    assert "```diff\n-    a\n+    b\n```" in finding.details


def test_the_skipped_line_is_what_the_quality_report_reads():
    line = mutation.skipped_result_line(60.0, "Nothing changed.")
    fragment = quality_report.Fragment(quality_report.Analysis.MUTATION, 0, line)
    finding = quality_report.analyze_mutation(fragment)
    assert (finding.status, finding.summary) == (
        quality_report.Status.SKIPPED,
        "Nothing changed.",
    )


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=60,
    )


def test_changed_targets_follow_a_real_branch(tmp_path, monkeypatch):
    source = tmp_path / FILE
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE)
    (tmp_path / "src/astrodoro/ui").mkdir(parents=True)
    (tmp_path / "src/astrodoro/ui/main.py").write_text("X = 1\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "topic")
    source.write_text(SOURCE.replace("value + 1", "value + 2"))
    (tmp_path / "src/astrodoro/ui/main.py").write_text("X = 2\n")
    _git(tmp_path, "commit", "-q", "-am", "change")
    monkeypatch.chdir(tmp_path)
    assert mutation.changed_targets("main", SCOPE) == [f"{MODULE}.x_helper__mutmut_*"]


@pytest.fixture
def summary(monkeypatch, tmp_path):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    monkeypatch.setenv("MUTATION_MIN_SCORE", "60")
    monkeypatch.setattr(
        mutation, "changed_targets", lambda base, scope: [f"{MODULE}.*"]
    )
    monkeypatch.setattr(mutation, "diff_of", lambda _: None)
    return path


def test_nothing_in_scope_changed_is_a_skip(summary, monkeypatch, capsys):
    monkeypatch.setattr(mutation, "changed_targets", lambda base, scope: [])
    assert mutation.changed_command("origin/main") == 0
    assert '"skipped": true' in capsys.readouterr().out
    assert "No function in the mutation scope changed" in summary.read_text(
        encoding="utf-8"
    )


def test_changed_functions_without_mutants_are_a_skip(summary, monkeypatch, capsys):
    monkeypatch.setattr(mutation, "run_mutmut", lambda _: (1, mutation.NOTHING_MATCHED))
    assert mutation.changed_command("origin/main") == 0
    assert "produce no mutants" in capsys.readouterr().out


def test_a_mutmut_failure_is_passed_on(summary, monkeypatch):
    monkeypatch.setattr(mutation, "run_mutmut", lambda _: (2, "clean tests failed"))
    assert mutation.changed_command("origin/main") == 2


@pytest.mark.parametrize(
    ("outcome", "exit_code"),
    [(mutation.Outcome.KILLED, 0), (mutation.Outcome.SURVIVED, 1)],
)
def test_the_pr_run_gates_on_the_ratchet(
    summary, monkeypatch, capsys, outcome, exit_code
):
    tally = mutation.Tally()
    tally.add(f"{MODULE}.x_helper__mutmut_1", outcome)
    monkeypatch.setattr(mutation, "run_mutmut", lambda _: (0, ""))
    monkeypatch.setattr(mutation, "collect", lambda _, patterns: {FILE: tally})
    assert mutation.changed_command("origin/main") == exit_code
    assert mutation.RESULT_PREFIX in capsys.readouterr().out
    assert "Mutation testing (changed code in scope)" in summary.read_text(
        encoding="utf-8"
    )


def test_the_full_report_needs_results(monkeypatch, tmp_path):
    monkeypatch.setattr(mutation, "MUTANTS_DIR", tmp_path)
    assert mutation.report_command(10, 0.0) == 1


def test_the_full_report_fails_below_its_minimum(monkeypatch, tmp_path):
    _write_meta(tmp_path, FILE, {f"{MODULE}.x_helper__mutmut_1": 0})
    monkeypatch.setattr(mutation, "MUTANTS_DIR", tmp_path)
    monkeypatch.setattr(mutation, "diff_of", lambda _: None)
    assert mutation.report_command(10, 50.0) == 2
    assert mutation.report_command(10, 0.0) == 0


def test_the_ratchet_defaults_to_the_constant(monkeypatch):
    monkeypatch.delenv("MUTATION_MIN_SCORE", raising=False)
    assert mutation.min_score() == mutation.DEFAULT_MIN_SCORE


def test_the_ratchet_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("MUTATION_MIN_SCORE", "72.5")
    assert mutation.min_score() == 72.5
