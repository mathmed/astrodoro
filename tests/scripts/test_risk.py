from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts import risk

ROOT = Path(__file__).resolve().parents[2]
GIT = (
    "git",
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@t",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=no-hooks",
)


@pytest.fixture(scope="module")
def floors():
    return risk.load_floors(ROOT / risk.DEFAULT_FLOORS)


@pytest.mark.parametrize(
    ("file", "category"),
    [
        ("pyproject.toml", "pipeline config"),
        ("uv.lock", "pipeline config"),
        (".pre-commit-config.yaml", "pipeline config"),
        ("Makefile", "pipeline config"),
        ("suppressions-baseline.json", "pipeline config"),
        ("hatch_build.py", "pipeline config"),
        ("packaging/astrodoro.spec", "pipeline config"),
        ("scripts/quality_report.py", "pipeline config"),
        (".github/workflows/checks.yml", "pipeline config"),
        (".github/risk-floors.yaml", "pipeline config"),
        (".github/pull_request_template.md", "pipeline config"),
        (".claude/agents/pr-writer.md", "pipeline config"),
        ("CONTRIBUTING.md", "pipeline config"),
        ("src/astrodoro/settings.py", "migrations and persistent formats"),
        ("src/astrodoro/migrations/0001.py", "migrations and persistent formats"),
        ("src/astrodoro/pointing/handset.py", "auth and secrets"),
        ("src/astrodoro/ui/auth/login.py", "auth and secrets"),
        ("src/astrodoro/secrets.py", "auth and secrets"),
        ("vendor/lib/libSVBCameraSDK.dylib", "camera SDK bindings"),
    ],
)
def test_sensitive_files_are_never_below_high(floors, file, category):
    assessment = risk.assess([file], floors)

    assert assessment.floor == risk.RiskLevel.HIGH
    assert [hit.category for hit in assessment.hits] == [category]


@pytest.mark.parametrize(
    "file",
    [
        "src/astrodoro/core/recorder.py",
        "src/astrodoro/core/source.py",
        "src/astrodoro/core/masters.py",
        "src/astrodoro/drivers/svbony/sdk.py",
    ],
)
def test_persistent_formats_and_sdk_bindings_inside_the_core_are_high(floors, file):
    assert risk.assess([file], floors).floor == risk.RiskLevel.HIGH


@pytest.mark.parametrize(
    "file",
    [
        "src/astrodoro/core/stacker.py",
        "src/astrodoro/drivers/base.py",
        "src/astrodoro/ui/worker.py",
    ],
)
def test_the_capture_and_stacking_core_is_at_least_medium(floors, file):
    assessment = risk.assess([file], floors)

    assert assessment.floor == risk.RiskLevel.MEDIUM
    assert [hit.category for hit in assessment.hits] == ["capture and stacking core"]


@pytest.mark.parametrize(
    "file",
    [
        "src/astrodoro/ui/main.py",
        "src/astrodoro/pointing/pushto.py",
        "src/astrodoro/cli/stack.py",
        "tests/core/test_calibration.py",
        "README.md",
        "docs/third-party.md",
        "assets/logo.png",
    ],
)
def test_ordinary_files_stay_low(floors, file):
    assert risk.assess([file], floors).floor == risk.RiskLevel.LOW


@pytest.mark.parametrize(
    ("file", "pattern", "expected"),
    [
        ("Makefile", "Makefile", True),
        ("src/Makefile", "Makefile", False),
        (".github/workflows/ci.yml", ".github/**", True),
        ("a/b/migrations/c/d.py", "**/migrations/**", True),
        ("migrations/d.py", "**/migrations/**", True),
        ("a/migrationsx/d.py", "**/migrations/**", False),
        ("src/a/oauth_client.py", "**/*auth*", True),
        ("src/core/x.py", "src/*/x.py", True),
        ("src/core/deep/x.py", "src/*/x.py", False),
        ("SRC/core/x.py", "src/*/x.py", False),
    ],
)
def test_star_stays_in_one_segment_and_double_star_crosses_any_number(
    file, pattern, expected
):
    assert risk.glob_match(file, pattern) is expected


@pytest.fixture
def assessment():
    floors = (
        risk.Floor("docs", risk.RiskLevel.MEDIUM, ("docs/**",)),
        risk.Floor("pipeline", risk.RiskLevel.HIGH, ("Makefile",)),
        risk.Floor("unused", risk.RiskLevel.CRITICAL, ("never/**",)),
    )
    return risk.assess(["docs/a.md", "Makefile", "docs/b.md", "src/x.py"], floors)


def test_matching_files_are_grouped_by_category(assessment):
    assert assessment.hits == (
        risk.FloorHit("docs", risk.RiskLevel.MEDIUM, ("docs/a.md", "docs/b.md")),
        risk.FloorHit("pipeline", risk.RiskLevel.HIGH, ("Makefile",)),
    )


def test_the_floor_is_the_highest_level_reached(assessment):
    assert assessment.floor == risk.RiskLevel.HIGH


def test_the_description_names_the_floor_and_the_files_behind_it(assessment):
    assert assessment.describe() == (
        "Risk floor: high\n"
        "- docs (medium): docs/a.md, docs/b.md\n"
        "- pipeline (high): Makefile"
    )


def test_no_hit_is_a_low_floor():
    assessment = risk.Assessment(())

    assert assessment.floor == risk.RiskLevel.LOW
    assert assessment.describe().endswith("no file matches a floor category")


def _floors_file(tmp_path, content):
    path = tmp_path / "floors.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_a_floors_file_loads_into_floors(tmp_path):
    path = _floors_file(
        tmp_path, "floors:\n  - {category: x, level: medium, paths: [a/**]}\n"
    )

    assert risk.load_floors(path) == (
        risk.Floor("x", risk.RiskLevel.MEDIUM, ("a/**",)),
    )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[]", "top-level `floors` list"),
        ("floors: [x]", "must be a mapping"),
        ("floors:\n  - {category: x, level: high, paths: []}", "non-empty list"),
        ("floors:\n  - {level: high, paths: [a]}", "needs a category"),
        ("floors:\n  - {category: x, level: urgent, paths: [a]}", "unknown level"),
    ],
)
def test_an_invalid_floors_file_is_rejected(tmp_path, content, message):
    with pytest.raises(risk.RiskFloorError, match=message):
        risk.load_floors(_floors_file(tmp_path, content))


@pytest.mark.parametrize(
    ("labels", "floor", "problem"),
    [
        (["risk:high"], risk.RiskLevel.HIGH, None),
        (["bug", "risk:critical"], risk.RiskLevel.HIGH, None),
        (["risk:low"], risk.RiskLevel.LOW, None),
        (
            ["risk:medium"],
            risk.RiskLevel.HIGH,
            "risk:medium is below the floor of this PR (risk:high)",
        ),
        ([], risk.RiskLevel.LOW, "expected exactly one risk label"),
        (["risk:low", "risk:high"], risk.RiskLevel.LOW, "expected exactly one"),
        (["risk:urgent"], risk.RiskLevel.LOW, "unknown risk label risk:urgent"),
    ],
)
def test_a_pr_needs_one_known_risk_label_at_or_above_the_floor(labels, floor, problem):
    result = risk.label_problem(labels, floor)

    if problem is None:
        assert result is None
    else:
        assert result is not None
        assert problem in result


def test_the_labels_of_the_event_are_parsed():
    assert risk.parse_labels('["risk:low", "bug"]') == ["risk:low", "bug"]
    assert risk.parse_labels("") == []


@pytest.mark.parametrize("labels_json", ['{"a": 1}', "[1]"])
def test_labels_that_are_not_a_list_of_strings_are_rejected(labels_json):
    with pytest.raises(risk.RiskFloorError, match="JSON list of strings"):
        risk.parse_labels(labels_json)


def test_each_level_has_its_label_and_rank():
    assert [level.label for level in risk.RiskLevel] == [
        "risk:low",
        "risk:medium",
        "risk:high",
        "risk:critical",
    ]
    assert [level.rank for level in risk.RiskLevel] == [0, 1, 2, 3]


def _git(repo, *args):
    subprocess.run([*GIT, *args], cwd=repo, check=True, capture_output=True, timeout=30)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # A git hook running this suite exports GIT_DIR and friends, which would
    # point every command below at the real repository.
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_both_sides_of_a_rename_count_against_the_merge_base(repo):
    _git(repo, "mv", "Makefile", "build.mk")
    _git(repo, "commit", "-q", "-m", "rename")
    _git(repo, "checkout", "-q", "main")
    (repo / "later.txt").write_text("on main only\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "main moves on")
    _git(repo, "checkout", "-q", "feature")

    assert risk.changed_files("main") == ["Makefile", "build.mk"]


def test_floor_prints_the_floor_of_the_branch(repo, capsys):
    (repo / "Makefile").write_text("all:\n\ttrue\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "pipeline")
    floors = _floors_file(
        repo, "floors:\n  - {category: pipeline, level: high, paths: [Makefile]}\n"
    )

    assert risk.main(["--floors", str(floors), "--base", "main", "floor"]) == 0
    assert capsys.readouterr().out == "Risk floor: high\n- pipeline (high): Makefile\n"


@pytest.mark.parametrize(
    ("labels", "exit_code", "message"),
    [
        ('["risk:high"]', 0, "at or above the floor"),
        ('["risk:low"]', 1, "risk:low is below the floor"),
        ("[]", 1, "expected exactly one risk label"),
        ("not json", 1, "Risk label check failed"),
    ],
)
def test_check_gates_on_the_label(repo, capsys, labels, exit_code, message):
    (repo / "Makefile").write_text("all:\n\ttrue\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "pipeline")
    floors = _floors_file(
        repo, "floors:\n  - {category: pipeline, level: high, paths: [Makefile]}\n"
    )
    argv = ["--floors", str(floors), "--base", "main", "check", "--labels-json"]

    assert risk.main([*argv, labels]) == exit_code
    captured = capsys.readouterr()
    assert message in captured.out + captured.err


def test_a_base_that_cannot_be_diffed_fails_the_check(repo, capsys):
    assert risk.main(["--base", "origin/nope", "floor"]) == 1
    assert "could not diff against origin/nope" in capsys.readouterr().err
