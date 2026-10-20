from __future__ import annotations

import pytest

from scripts import risk


@pytest.mark.parametrize(
    ("labels", "problem"),
    [
        (["risk:high"], None),
        (["bug", "risk:critical"], None),
        (["risk:low"], None),
        (["risk:medium", "dependencies"], None),
        ([], "expected exactly one risk label"),
        (["bug"], "found: none"),
        (["risk:low", "risk:high"], "expected exactly one"),
        (["risk:urgent"], "unknown risk label risk:urgent"),
    ],
)
def test_a_pr_needs_exactly_one_known_risk_label(labels, problem):
    result = risk.label_problem(labels)

    if problem is None:
        assert result is None
    else:
        assert result is not None
        assert problem in result


def test_the_valid_labels_are_listed_when_the_label_is_wrong():
    assert risk.label_problem(["risk:urgent"]) == (
        "unknown risk label risk:urgent: use one of "
        "risk:low, risk:medium, risk:high, risk:critical"
    )
    assert risk.label_problem(["risk:low", "risk:high"]) == (
        "expected exactly one risk label "
        "(risk:low, risk:medium, risk:high, risk:critical), "
        "found: ['risk:low', 'risk:high']"
    )


def test_only_risk_labels_are_counted():
    assert risk.risk_labels(["bug", "risk:low", "riskless"]) == ["risk:low"]


def test_the_labels_of_the_event_are_parsed():
    assert risk.parse_labels('["risk:low", "bug"]') == ["risk:low", "bug"]
    assert risk.parse_labels("") == []


@pytest.mark.parametrize("labels_json", ['{"a": 1}', "[1]"])
def test_labels_that_are_not_a_list_of_strings_are_rejected(labels_json):
    with pytest.raises(risk.RiskLabelError, match="JSON list of strings"):
        risk.parse_labels(labels_json)


def test_each_level_has_its_label():
    assert [level.label for level in risk.RiskLevel] == [
        "risk:low",
        "risk:medium",
        "risk:high",
        "risk:critical",
    ]


@pytest.mark.parametrize(
    ("labels", "exit_code", "stream", "message"),
    [
        ('["risk:low"]', 0, "out", "Risk label: risk:low."),
        ('["bug", "risk:critical"]', 0, "out", "Risk label: risk:critical."),
        ("[]", 1, "err", "expected exactly one risk label"),
        ('["risk:urgent"]', 1, "err", "unknown risk label"),
        ("not json", 1, "err", "Risk label check failed"),
        ("[1]", 1, "err", "JSON list of strings"),
    ],
)
def test_check_gates_on_the_label(capsys, labels, exit_code, stream, message):
    assert risk.main(["check", "--labels-json", labels]) == exit_code
    assert message in getattr(capsys.readouterr(), stream)


def test_check_needs_the_labels():
    with pytest.raises(SystemExit):
        risk.main(["check"])
