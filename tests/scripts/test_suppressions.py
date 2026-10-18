from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from scripts import suppressions as sp

ROOT = Path(__file__).resolve().parents[2]
WORKER = "src/astrodoro/ui/worker.py"


def _kinds_in(source):
    return [occurrence.kind for occurrence in sp.scan_source("x.py", source)]


def _write(root, relative, source):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("comment", "kind"),
    [
        ("# noqa", sp.Kind.NOQA),
        ("# noqa: E501", sp.Kind.NOQA),
        ("# ruff: noqa", sp.Kind.NOQA),
        ("# type: ignore", sp.Kind.TYPE_IGNORE),
        ("# type: ignore[arg-type]", sp.Kind.TYPE_IGNORE),
        ("# nosec B101", sp.Kind.NOSEC),
        ("# pragma: no cover", sp.Kind.NO_COVER),
        ("# pragma: no branch", sp.Kind.NO_COVER),
        ("# pylint: disable=invalid-name", sp.Kind.PYLINT),
        ("# mypy: ignore-errors", sp.Kind.MYPY),
        ("# pyright: ignore[reportGeneralTypeIssues]", sp.Kind.PYRIGHT),
        ("# fmt: off", sp.Kind.FORMATTER),
        ("# fmt: skip", sp.Kind.FORMATTER),
        ("# isort: skip_file", sp.Kind.FORMATTER),
    ],
)
def test_every_comment_suppression_is_counted(comment, kind):
    assert _kinds_in(f"value = 1  {comment}\n") == [kind]


def test_two_suppressions_in_one_comment_count_twice():
    assert _kinds_in("value = 1  # type: ignore[misc]  # noqa: E501\n") == [
        sp.Kind.NOQA,
        sp.Kind.TYPE_IGNORE,
    ]


def test_a_suppression_is_reported_on_the_line_of_its_comment():
    occurrences = sp.scan_source(WORKER, "a = 1\n\nb = 2  # nosec\n")

    assert occurrences == [sp.Occurrence(WORKER, 3, sp.Kind.NOSEC)]


@pytest.mark.parametrize(
    "source",
    [
        'text = "# noqa"\n',
        'text = """\n# type: ignore\n"""\n',
        "# a regular comment about types\n",
        "skip = True\nif skip:\n    pass\n",
        "outcome.skipped = True\n",
    ],
)
def test_the_same_words_outside_a_comment_or_a_marker_do_not_count(source):
    assert _kinds_in(source) == []


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("import pytest\n@pytest.mark.skip\ndef f(): ...\n", sp.Kind.TEST_SKIP),
        (
            "import pytest\n@pytest.mark.skipif(True, reason='x')\ndef f(): ...\n",
            sp.Kind.TEST_SKIP,
        ),
        ("import pytest\n@pytest.mark.xfail\ndef f(): ...\n", sp.Kind.TEST_XFAIL),
        ("from pytest import mark\n@mark.skip\ndef f(): ...\n", sp.Kind.TEST_SKIP),
        ("import pytest\npytestmark = pytest.mark.skip\n", sp.Kind.TEST_SKIP),
        ("import pytest\ndef f():\n    pytest.skip('x')\n", sp.Kind.TEST_SKIP),
        ("import pytest\nyaml = pytest.importorskip('yaml')\n", sp.Kind.TEST_SKIP),
        ("import pytest\ndef f():\n    pytest.xfail('x')\n", sp.Kind.TEST_XFAIL),
        (
            "import pytest\np = pytest.param(1, marks=pytest.mark.xfail)\n",
            sp.Kind.TEST_XFAIL,
        ),
        ("from pytest import skip as s\ndef f():\n    s('x')\n", sp.Kind.TEST_SKIP),
        (
            "import unittest\n@unittest.skipIf(True, 'x')\ndef f(): ...\n",
            sp.Kind.TEST_SKIP,
        ),
        (
            "import unittest\n@unittest.expectedFailure\ndef f(): ...\n",
            sp.Kind.TEST_XFAIL,
        ),
        ("def test(self):\n    self.skipTest('x')\n", sp.Kind.TEST_SKIP),
    ],
)
def test_skipped_and_expected_failure_tests_are_counted(source, kind):
    assert _kinds_in(source) == [kind]


def test_other_pytest_marks_are_not_suppressions():
    source = "import pytest\n@pytest.mark.parametrize('a', [1])\ndef f(a): ...\n"

    assert _kinds_in(source) == []


def test_a_file_that_does_not_parse_fails_loudly():
    with pytest.raises(SyntaxError):
        sp.scan_source("x.py", "def broken(:\n")


def test_the_scan_reads_python_files_under_every_root_and_single_files(tmp_path):
    _write(tmp_path, "src/a.py", "a = 1  # noqa\n")
    _write(tmp_path, "tests/b.py", "b = 1  # type: ignore\n")
    _write(tmp_path, "src/notes.md", "# noqa\n")
    single = _write(tmp_path, "build.py", "c = 1  # nosec\n")

    occurrences = sp.scan([tmp_path / "src", tmp_path / "tests", single])

    assert [(Path(item.path).name, item.kind) for item in occurrences] == [
        ("build.py", sp.Kind.NOSEC),
        ("a.py", sp.Kind.NOQA),
        ("b.py", sp.Kind.TYPE_IGNORE),
    ]


def test_the_scan_paths_are_posix_so_every_platform_shares_one_baseline(tmp_path):
    _write(tmp_path, "src/pkg/a.py", "a = 1  # noqa\n")

    (occurrence,) = sp.scan([tmp_path / "src"])

    assert occurrence.path == (tmp_path / "src" / "pkg" / "a.py").as_posix()
    assert "\\" not in occurrence.path


@pytest.mark.parametrize("missing", ["missing", "notes.md"])
def test_a_root_that_is_not_python_fails_instead_of_scanning_nothing(tmp_path, missing):
    _write(tmp_path, "notes.md", "# noqa\n")

    with pytest.raises(FileNotFoundError, match="root not found"):
        sp.scan([tmp_path / missing])


@pytest.fixture
def comparison():
    occurrences = (
        sp.Occurrence(WORKER, 3, sp.Kind.NOSEC),
        sp.Occurrence(WORKER, 9, sp.Kind.NOSEC),
        sp.Occurrence("tests/t.py", 1, sp.Kind.TEST_SKIP),
    )
    baseline = Counter({(WORKER, sp.Kind.NOSEC): 1, ("src/gone.py", sp.Kind.NOQA): 2})
    return sp.Comparison(occurrences, baseline)


def test_an_increase_lists_every_line_of_that_kind_in_the_file(comparison):
    assert comparison.increases == (
        sp.Change(WORKER, sp.Kind.NOSEC, 1, 2, (3, 9)),
        sp.Change("tests/t.py", sp.Kind.TEST_SKIP, 0, 1, (1,)),
    )


def test_a_decrease_includes_files_that_no_longer_exist(comparison):
    assert comparison.decreases == (sp.Change("src/gone.py", sp.Kind.NOQA, 2, 0, ()),)


def test_matching_counts_are_no_change():
    occurrences = (sp.Occurrence(WORKER, 3, sp.Kind.NOSEC),)

    comparison = sp.Comparison(occurrences, Counter({(WORKER, sp.Kind.NOSEC): 1}))

    assert comparison.changes == ()


def test_a_suppression_moved_to_another_file_is_an_increase_there():
    occurrences = (sp.Occurrence("src/other.py", 1, sp.Kind.NOSEC),)

    comparison = sp.Comparison(occurrences, Counter({(WORKER, sp.Kind.NOSEC): 1}))

    assert [change.path for change in comparison.increases] == ["src/other.py"]
    assert [change.path for change in comparison.decreases] == [WORKER]


def test_a_suppression_swapped_for_another_kind_is_an_increase_of_that_kind():
    occurrences = (sp.Occurrence(WORKER, 1, sp.Kind.NOQA),)

    comparison = sp.Comparison(occurrences, Counter({(WORKER, sp.Kind.NOSEC): 1}))

    assert comparison.increases == (sp.Change(WORKER, sp.Kind.NOQA, 0, 1, (1,)),)


def test_the_result_line_carries_totals_and_every_change(comparison):
    line = comparison.to_result_line()

    payload = json.loads(line.removeprefix(sp.RESULT_PREFIX))
    assert line.startswith(sp.RESULT_PREFIX)
    assert payload["total"] == 3
    assert payload["baseline_total"] == 3
    assert payload["totals"] == {"nosec": 2, "test-skip": 1}
    assert len(payload["changes"]) == 3
    assert payload["changes"][1] == {
        "path": "src/gone.py",
        "kind": "noqa",
        "baseline": 2,
        "current": 0,
        "lines": [],
    }


def test_a_missing_baseline_counts_as_empty(tmp_path):
    assert sp.load_baseline(tmp_path / "missing.json") == Counter()


def test_the_baseline_round_trips_sorted_by_file_and_kind(tmp_path):
    path = tmp_path / "baseline.json"
    counts = Counter({(WORKER, sp.Kind.NOSEC): 1, (WORKER, sp.Kind.NOQA): 2})

    sp.write_baseline(path, counts)

    assert sp.load_baseline(path) == counts
    assert json.loads(path.read_text()) == {WORKER: {"noqa": 2, "nosec": 1}}


def test_a_baseline_with_an_unknown_kind_is_rejected(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({WORKER: {"unknown": 1}}))

    with pytest.raises(ValueError, match="unknown"):
        sp.load_baseline(path)


def test_the_committed_baseline_names_only_known_kinds_and_scanned_files():
    baseline = sp.load_baseline(ROOT / sp.DEFAULT_BASELINE)

    roots = [root.as_posix() for root in sp.DEFAULT_ROOTS]
    assert baseline
    for file, _ in baseline:
        assert any(file == root or file.startswith(f"{root}/") for root in roots)
        assert (ROOT / file).is_file()


@pytest.fixture
def source_root(tmp_path):
    _write(tmp_path, "src/a.py", "a = 1  # noqa\nb = 2  # noqa\n")
    return tmp_path / "src"


def _run(baseline, root, *command):
    return sp.main(["--baseline", str(baseline), "--root", str(root), *command])


def _lock(baseline, root, count):
    file = (root / "a.py").as_posix()
    sp.write_baseline(baseline, Counter({(file, sp.Kind.NOQA): count}))


def test_check_passes_when_the_suppressions_match_the_baseline(
    tmp_path, source_root, capsys
):
    baseline = tmp_path / "baseline.json"
    _lock(baseline, source_root, 2)

    assert _run(baseline, source_root, "check") == 0
    output = capsys.readouterr().out
    assert "Suppressions: 2 (baseline 2)" in output
    assert "Suppressions match the baseline." in output


def test_check_fails_and_names_the_lines_when_a_suppression_is_added(
    tmp_path, source_root, capsys
):
    baseline = tmp_path / "baseline.json"
    _lock(baseline, source_root, 1)

    assert _run(baseline, source_root, "check") == 1
    output = capsys.readouterr().out
    assert "New suppressions" in output
    assert "noqa 1 -> 2 (lines: 1, 2)" in output
    assert sp.RESULT_PREFIX in output


def test_check_fails_without_a_baseline(tmp_path, source_root):
    assert _run(tmp_path / "missing.json", source_root, "check") == 1


def test_check_fails_until_a_lower_count_is_locked_in(tmp_path, source_root, capsys):
    baseline = tmp_path / "baseline.json"
    _lock(baseline, source_root, 3)

    assert _run(baseline, source_root, "check") == 1
    assert "make suppressions-update" in capsys.readouterr().out


def test_update_lowers_the_baseline(tmp_path, source_root):
    baseline = tmp_path / "baseline.json"
    _lock(baseline, source_root, 3)

    assert _run(baseline, source_root, "update") == 0
    assert _run(baseline, source_root, "check") == 0


def test_update_refuses_to_raise_the_baseline_without_the_flag(
    tmp_path, source_root, capsys
):
    baseline = tmp_path / "baseline.json"
    _lock(baseline, source_root, 1)

    assert _run(baseline, source_root, "update") == 1
    assert "Refusing to raise the baseline" in capsys.readouterr().out
    assert _run(baseline, source_root, "check") == 1


def test_update_raises_the_baseline_only_with_the_flag(tmp_path, source_root):
    baseline = tmp_path / "baseline.json"
    _lock(baseline, source_root, 1)

    assert _run(baseline, source_root, "update", "--allow-increase") == 0
    assert _run(baseline, source_root, "check") == 0
