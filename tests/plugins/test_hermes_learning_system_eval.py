from __future__ import annotations

from pathlib import Path

from plugins.hermes_learning_system.eval import EvalCase, EvalSuite, run_eval_suite


def test_eval_suite_scores_scope_confidence_and_privacy(tmp_path: Path) -> None:
    suite = EvalSuite(
        cases=[
            EvalCase(
                id="transferable-learning",
                text="Do not learn Hyperframes as DBM-only; learn every capability as transferable.",
                source="telegram",
                project_id="dbm",
                tags=["learning"],
                expected_instinct_id="learn-capabilities-as-transferable-domains",
                expected_scope="global",
                min_confidence=0.9,
                forbidden_status_fragments=["Do not learn Hyperframes"],
            ),
            EvalCase(
                id="project-specific-testing",
                text="In repo alpha, tests must be run with pytest -n 4 before committing.",
                source="cli",
                project_id="repo-alpha",
                tags=["testing"],
                expected_scope="project",
                min_confidence=0.5,
            ),
        ]
    )

    report = run_eval_suite(suite, root=tmp_path)

    assert report.passed is True
    assert report.score == 1.0
    assert [result.id for result in report.results] == ["transferable-learning", "project-specific-testing"]
    assert all(result.passed for result in report.results)
    markdown = report.to_markdown()
    assert "# Hermes Learning System Eval Report" in markdown
    assert "transferable-learning" in markdown


def test_eval_suite_reports_failed_expectations(tmp_path: Path) -> None:
    suite = EvalSuite(
        cases=[
            EvalCase(
                id="bad-expectation",
                text="In repo alpha, tests must be run with pytest -n 4 before committing.",
                source="cli",
                project_id="repo-alpha",
                tags=["testing"],
                expected_scope="global",
                min_confidence=0.9,
            )
        ]
    )

    report = run_eval_suite(suite, root=tmp_path)

    assert report.passed is False
    assert report.score == 0.0
    assert report.results[0].failures == [
        "expected scope global, got project",
        "expected confidence >= 0.9, got 0.5",
    ]
