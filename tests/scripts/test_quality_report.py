from __future__ import annotations

import json
import sys

import pytest

from scripts import quality_report as qr

qr.PRIMARY = qr.PRIMARY_PLATFORM
WINDOWS = "windows-latest · py3.12"

PYTEST_OK = """........................................                  [100%]

Name                              Stmts   Miss  Cover
-----------------------------------------------------
src/astrodoro/core/a.py              10      0   100%
src/astrodoro/core/b.py              10      5    50%
src/astrodoro/core/c.py              10      2    80%
-----------------------------------------------------
TOTAL                                30      7    77%
326 passed in 506.16s (0:08:26)
"""
PYTEST_FAILED = """FAILED tests/core/test_a.py::test_x - assert 1 == 2
1 failed, 325 passed in 500.00s (0:08:20)
"""
IMPORT_LINTER_OK = """Analyzed 98 files, 416 dependencies.
------------------------------------

the CLI stays headless KEPT
one-way layering KEPT (2 ignored imports)

Contracts: 2 kept, 0 broken.
"""
IMPORT_LINTER_BROKEN = """Analyzed 98 files, 416 dependencies.
------------------------------------

the CLI stays headless BROKEN
one-way layering KEPT

Contracts: 1 kept, 1 broken.


----------------
Broken contracts
----------------

the CLI stays headless
----------------------

astrodoro.cli is not allowed to import PySide6:

- astrodoro.cli.stack -> PySide6.QtCore (l.12)
"""
SMOKE_OK = """smoke: astrodoro 0.1.0 on /x/python
  ok  liveness: astrodoro --version (3.3 s)
  ok  config: astrodoro settings --set ... (1.5 s)
  ok  pipeline: astrodoro replay <recorded session> (5.2 s)
  info  pipeline: 8 of 8 frames stacked
  ok  gui: astrodoro-gui entry point, offscreen (5.7 s)
smoke: ready
"""
SMOKE_FAILED = """smoke: astrodoro 0.1.0 on /x/python
  ok  liveness: astrodoro --version (3.3 s)
FAIL config: exit code 2
--- stdout

--- stderr
usage: astrodoro settings
"""


def _fragment(analysis, output, exit_code=0, platform=qr.PRIMARY, seconds=1.0):
    return qr.Fragment(analysis, exit_code, output, platform, seconds)


def test_a_fragment_survives_the_round_trip_through_disk(tmp_path):
    fragment = _fragment(qr.Analysis.MYPY, "ok", 1, WINDOWS, 2.5)
    path = fragment.write(tmp_path)
    assert path.name == "mypy@windows-latest-py3.12.json"
    assert qr.Fragment.read(path) == fragment


def test_fragments_of_every_platform_load_from_nested_folders(tmp_path):
    _fragment(qr.Analysis.MYPY, "a").write(tmp_path / "one")
    _fragment(qr.Analysis.MYPY, "b", platform=WINDOWS).write(tmp_path / "two")
    (tmp_path / "broken.json").write_text("{")
    assert {f.platform for f in qr.load_fragments(tmp_path)} == {qr.PRIMARY, WINDOWS}


@pytest.mark.parametrize(
    ("analyze", "output", "exit_code", "status", "summary"),
    [
        (qr.analyze_ruff_check, "", 0, qr.Status.OK, "No lint issues"),
        (
            qr.analyze_ruff_check,
            "Found 3 errors.",
            1,
            qr.Status.FAILED,
            "3 lint issues",
        ),
        (qr.analyze_ruff_format, "", 0, qr.Status.OK, "Formatting is correct"),
        (
            qr.analyze_ruff_format,
            "Would reformat: a.py\nWould reformat: b.py",
            1,
            qr.Status.FAILED,
            "2 files need formatting (`make fmt`)",
        ),
        (
            qr.analyze_mypy,
            "Success: no issues found in 61 source files",
            0,
            qr.Status.OK,
            "No type errors (61 files)",
        ),
        (
            qr.analyze_mypy,
            "Found 1 error in 1 file",
            1,
            qr.Status.FAILED,
            "1 type error",
        ),
        (qr.analyze_bandit, "", 0, qr.Status.OK, "No medium or high severity issues"),
        (qr.analyze_bandit, ">> Issue: x", 1, qr.Status.FAILED, "1 security issue"),
        (qr.analyze_vulture, "", 0, qr.Status.OK, "No dead code detected"),
        (qr.analyze_vulture, "a.py:1: unused", 3, qr.Status.FAILED, "1 dead code item"),
        (qr.analyze_xenon, "", 0, qr.Status.OK, "Complexity within the thresholds"),
        (
            qr.analyze_xenon,
            "ERROR:xenon:block 'x' has a rank of F",
            1,
            qr.Status.FAILED,
            "1 complexity violation",
        ),
        (
            qr.analyze_catalogue,
            "astrodoro.pot is up to date (682 messages)",
            0,
            qr.Status.OK,
            "Catalogue matches the code (682 messages)",
        ),
        (
            qr.analyze_catalogue,
            "astrodoro.pot is behind",
            1,
            qr.Status.FAILED,
            "The catalogue is behind the code (`make i18n`)",
        ),
        (
            qr.analyze_wheel_build,
            "Successfully built astrodoro-0.1.0-py3-none-any.whl",
            0,
            qr.Status.OK,
            "Built `astrodoro-0.1.0-py3-none-any.whl`",
        ),
        (
            qr.analyze_wheel_build,
            "error",
            1,
            qr.Status.FAILED,
            "The wheel did not build",
        ),
        (
            qr.analyze_wheel_data,
            "412 files, package data present",
            0,
            qr.Status.OK,
            "Package data present (412 files)",
        ),
        (
            qr.analyze_wheel_data,
            "AssertionError: x missing from the wheel",
            1,
            qr.Status.FAILED,
            "Package data missing from the wheel",
        ),
    ],
)
def test_each_tool_is_summarised_in_one_line(
    analyze, output, exit_code, status, summary
):
    finding = analyze(_fragment(qr.Analysis.MYPY, output, exit_code))
    assert (finding.status, finding.summary) == (status, summary)


def test_pytest_reports_counts_coverage_and_the_least_covered_files():
    finding = qr.analyze_pytest(_fragment(qr.Analysis.PYTEST, PYTEST_OK))
    assert finding.status == qr.Status.OK
    assert finding.summary == "326 passed, 0 failed · coverage 77%"
    assert finding.details.splitlines()[1].startswith("src/astrodoro/core/b.py")
    assert "a.py" not in finding.details


def test_pytest_failures_list_the_failing_tests():
    finding = qr.analyze_pytest(_fragment(qr.Analysis.PYTEST, PYTEST_FAILED, 1))
    assert finding.status == qr.Status.FAILED
    assert finding.summary.startswith("325 passed, 1 failed")
    assert finding.details.startswith("FAILED tests/core/test_a.py::test_x")


def test_pytest_that_never_finished_says_so():
    finding = qr.analyze_pytest(_fragment(qr.Analysis.PYTEST, "ImportError", 4))
    assert finding.summary == "pytest failed before finishing"


def test_import_linter_lists_the_contracts_and_the_baseline():
    finding = qr.analyze_import_linter(
        _fragment(qr.Analysis.IMPORT_LINTER, IMPORT_LINTER_OK)
    )
    assert finding.status == qr.Status.OK
    assert finding.summary == "2 contracts kept (2 ignored imports in the baseline)"
    assert "KEPT   one-way layering (2 ignored)" in finding.details


def test_import_linter_names_the_broken_contract_and_the_import():
    finding = qr.analyze_import_linter(
        _fragment(qr.Analysis.IMPORT_LINTER, IMPORT_LINTER_BROKEN, 1)
    )
    assert finding.status == qr.Status.FAILED
    assert finding.summary == "1 kept, 1 broken: **the CLI stays headless**"
    assert "astrodoro.cli.stack -> PySide6.QtCore" in finding.details


def test_import_linter_that_crashed_is_a_failure():
    finding = qr.analyze_import_linter(_fragment(qr.Analysis.IMPORT_LINTER, "boom", 1))
    assert finding.summary == "import-linter failed before evaluating the contracts"


def test_smoke_reports_each_step_and_the_stack():
    finding = qr.analyze_smoke(_fragment(qr.Analysis.SMOKE, SMOKE_OK))
    assert finding.status == qr.Status.OK
    assert finding.summary == (
        "4 steps ready · liveness 3.3s · config 1.5s · pipeline 5.2s · gui 5.7s"
        " · 8 of 8 frames stacked"
    )


def test_smoke_names_the_step_that_failed_and_quotes_its_output():
    finding = qr.analyze_smoke(_fragment(qr.Analysis.SMOKE, SMOKE_FAILED, 1))
    assert finding.status == qr.Status.FAILED
    assert finding.summary == "`config` failed: exit code 2 (after liveness 3.3s)"
    assert finding.details.startswith("FAIL config")
    assert "usage: astrodoro settings" in finding.details


def test_a_smoke_that_exits_0_without_the_ready_line_is_a_failure():
    finding = qr.analyze_smoke(_fragment(qr.Analysis.SMOKE, "smoke: astrodoro 0.1.0"))
    assert finding.status == qr.Status.FAILED


def test_the_mutation_output_without_a_result_line_is_a_failure():
    finding = qr.analyze_mutation(_fragment(qr.Analysis.MUTATION, "Traceback", 1))
    assert finding.summary == "mutmut failed before producing a score"


def test_an_output_no_analyser_understands_still_reports_the_exit_code(monkeypatch):
    def explode(_):
        raise ValueError("unexpected")

    monkeypatch.setitem(qr.ANALYZERS, qr.Analysis.MYPY, explode)
    finding = qr.analyze(_fragment(qr.Analysis.MYPY, "x", 2))
    assert finding.status == qr.Status.FAILED
    assert finding.summary.startswith("failed (exit code 2)")


def test_ansi_colours_spinners_and_carriage_returns_are_cleaned():
    assert qr.clean("\x1b[31mred\x1b[0m\r\n⠋ running\nend") == "red\nend"


def _checks(*fragments):
    section = next(s for s in qr.SECTIONS if s.checks[0].analysis == qr.Analysis.MYPY)
    return qr.evaluate(section, list(fragments), {"checks": "failure"})


def test_every_platform_agreeing_quotes_the_primary_one():
    result = _checks(
        _fragment(
            qr.Analysis.MYPY, "no issues found in 61 source files", platform=WINDOWS
        ),
        _fragment(qr.Analysis.MYPY, "no issues found in 60 source files"),
    )
    item = result.results[0]
    assert result.status == qr.Status.OK
    assert item.finding.summary == "No type errors (60 files) · 2/2 platforms"
    assert [p for p, _, _ in item.platforms] == [qr.PRIMARY, WINDOWS]


def test_a_failure_on_one_platform_names_that_platform():
    result = _checks(
        _fragment(qr.Analysis.MYPY, "no issues found in 60 source files"),
        _fragment(qr.Analysis.MYPY, "Found 2 errors", 1, WINDOWS),
    )
    finding = result.results[0].finding
    assert result.status == qr.Status.FAILED
    assert finding.summary == f"2 type errors · on {WINDOWS}"
    assert finding.details_title == f"Output ({WINDOWS})"


def test_a_platform_that_left_no_fragment_counts_against_the_check():
    result = _checks(
        _fragment(qr.Analysis.MYPY, "no issues found in 60 source files"),
        _fragment(qr.Analysis.RUFF_CHECK, "", platform=WINDOWS),
    )
    assert result.status == qr.Status.FAILED
    assert result.results[0].finding.summary.endswith(f"· on {WINDOWS}")


def test_a_check_with_no_fragment_explains_why():
    assert qr.missing_finding("mutation", {}).status == qr.Status.SKIPPED
    assert qr.missing_finding("mutation", {"mutation": "skipped"}).status == (
        qr.Status.SKIPPED
    )
    assert qr.missing_finding("wheel", {"wheel": "success"}).status == qr.Status.WARNING
    assert qr.missing_finding("wheel", {"wheel": "failure"}).status == qr.Status.FAILED


def _all_ok():
    outputs = {
        qr.Analysis.MYPY: "no issues found in 60 source files",
        qr.Analysis.CATALOGUE: "up to date (682 messages)",
        qr.Analysis.PYTEST: PYTEST_OK,
        qr.Analysis.IMPORT_LINTER: IMPORT_LINTER_OK,
        qr.Analysis.SMOKE: SMOKE_OK,
        qr.Analysis.WHEEL_BUILD: "Successfully built a.whl",
        qr.Analysis.WHEEL_DATA: "3 files, package data present",
    }
    fragments = []
    for analysis in qr.Analysis:
        if analysis == qr.Analysis.MUTATION:
            continue
        platforms = (qr.PRIMARY, WINDOWS) if qr.JOB_OF[analysis] == "checks" else ("",)
        fragments += [
            _fragment(analysis, outputs.get(analysis, ""), platform=p)
            for p in platforms
        ]
    return fragments


def test_the_report_carries_the_marker_every_section_and_the_matrix():
    report = qr.render(_all_ok(), {"mutation": "skipped"}, "commit `abc1234`")
    assert report.startswith(qr.MARKER + "\n## 🔍 Quality Report")
    assert "✅ **All good** (1 section not run)" in report
    for section in qr.SECTIONS:
        assert f"| {section.title} |" in report
    assert f"| {WINDOWS} | " in report
    assert "By platform" in report
    assert report.rstrip().endswith("commit `abc1234`")
    assert "This report only informs" in report


def test_a_failure_anywhere_turns_the_verdict():
    fragments = [*_all_ok(), _fragment(qr.Analysis.MUTATION, "Traceback", 1)]
    report = qr.render(fragments, {"mutation": "failure"})
    assert "❌ **Failed**: 1 section failed" in report


def test_a_report_too_long_for_a_comment_is_trimmed(monkeypatch):
    fragments = [
        f
        if f.analysis != qr.Analysis.RUFF_CHECK
        else _fragment(f.analysis, "x" * 9000, 1)
        for f in _all_ok()
    ]
    monkeypatch.setattr(qr, "MAX_REPORT_CHARS", 3000)
    report = qr.render(fragments, {})
    assert "Details were trimmed" in report
    assert "x" * 1600 not in report


def test_job_results_come_from_the_needs_context():
    needs = json.dumps({"checks": {"result": "success", "outputs": {}}})
    assert qr.parse_job_results(needs) == {"checks": "success"}
    assert qr.parse_job_results("") == {}


def test_the_footer_links_the_run(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "mathmed/astrodoro")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("QUALITY_REPORT_SHA", "abcdef123456")
    assert qr.footer_from_environment() == (
        "commit `abcdef1` · "
        "[CI run](https://github.com/mathmed/astrodoro/actions/runs/42)"
    )


def test_the_footer_is_empty_outside_ci(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    assert qr.footer_from_environment() == ""


def test_run_keeps_the_exit_code_and_stores_the_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(qr.PLATFORM_ENV, WINDOWS)
    code = "import sys; print('Found 2 errors'); sys.exit(3)"
    argv = [
        "run",
        "ruff-check",
        "--dir",
        str(tmp_path),
        "--",
        sys.executable,
        "-c",
        code,
    ]
    assert qr.main(argv) == 3
    assert "Found 2 errors" in capsys.readouterr().out
    (fragment,) = qr.load_fragments(tmp_path)
    assert (fragment.exit_code, fragment.platform) == (3, WINDOWS)
    assert fragment.output.strip() == "Found 2 errors"


def test_run_of_a_missing_program_is_a_failed_fragment(tmp_path):
    argv = ["run", "mypy", "--dir", str(tmp_path), "--", "no-such-program-here"]
    assert qr.main(argv) == 127
    assert qr.load_fragments(tmp_path)[0].exit_code == 127


def test_run_without_a_command_is_a_usage_error(tmp_path):
    assert qr.main(["run", "mypy", "--dir", str(tmp_path)]) == 2


def test_render_writes_the_report(tmp_path):
    for fragment in _all_ok():
        fragment.write(tmp_path / "fragments")
    output = tmp_path / "report.md"
    argv = ["render", "--dir", str(tmp_path / "fragments"), "--output", str(output)]
    assert qr.main(argv) == 0
    assert output.read_text(encoding="utf-8").startswith(qr.MARKER)
