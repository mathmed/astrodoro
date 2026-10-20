"""The check that a PR carries exactly one known risk label.

    python scripts/risk.py check --labels-json '["risk:medium"]'

`check` fails unless the labels hold exactly one of risk:low, risk:medium,
risk:high or risk:critical. Which one is a judgement, made by the pr-writer
agent (.claude/agents/pr-writer.md) or by whoever opens the PR; no file decides
it. The `risk-label` workflow runs it on every push and every label change.
"""

from __future__ import annotations

import argparse
import json
import sys
from enum import StrEnum

LABEL_PREFIX = "risk:"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def label(self) -> str:
        return f"{LABEL_PREFIX}{self.value}"


class RiskLabelError(Exception):
    pass


def risk_labels(labels: list[str]) -> list[str]:
    return [label for label in labels if label.startswith(LABEL_PREFIX)]


def label_problem(labels: list[str]) -> str | None:
    found = risk_labels(labels)
    valid = [level.label for level in RiskLevel]
    if len(found) != 1:
        return (
            f"expected exactly one risk label ({', '.join(valid)}), "
            f"found: {found or 'none'}"
        )
    if found[0] not in valid:
        return f"unknown risk label {found[0]}: use one of {', '.join(valid)}"
    return None


def parse_labels(labels_json: str) -> list[str]:
    labels = json.loads(labels_json or "[]")
    if not isinstance(labels, list) or not all(
        isinstance(label, str) for label in labels
    ):
        raise RiskLabelError(
            f"--labels-json must be a JSON list of strings, got: {labels_json}"
        )
    return labels


def check_command(labels_json: str) -> int:
    labels = parse_labels(labels_json)
    problem = label_problem(labels)
    if problem:
        print(f"Risk label check failed: {problem}.", file=sys.stderr)
        return 1
    print(f"Risk label: {risk_labels(labels)[0]}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Risk label check.")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "check", help="fail unless the PR has exactly one known risk label"
    )
    check.add_argument("--labels-json", required=True)
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        return check_command(args.labels_json)
    except (RiskLabelError, json.JSONDecodeError) as error:
        print(f"Risk label check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
