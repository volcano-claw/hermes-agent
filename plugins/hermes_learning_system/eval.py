from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plugins.hermes_learning_system.engine import LearningEngine, Observation


@dataclass(frozen=True)
class EvalCase:
    """One deterministic probe for HLS behavior."""

    id: str
    text: str
    source: str
    project_id: str | None = None
    project_name: str | None = None
    tags: list[str] = field(default_factory=list)
    expected_instinct_id: str | None = None
    expected_scope: str | None = None
    min_confidence: float | None = None
    forbidden_status_fragments: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvalSuite:
    """A small offline fixture/probe suite for HLS."""

    cases: list[EvalCase]
    name: str = "hls-eval"


@dataclass(frozen=True)
class EvalResult:
    id: str
    passed: bool
    failures: list[str]
    instinct_id: str
    scope: str
    confidence: float

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "passed": self.passed,
            "failures": self.failures,
            "instinct_id": self.instinct_id,
            "scope": self.scope,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class EvalReport:
    suite: str
    results: list[EvalResult]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def score(self) -> float:
        if not self.results:
            return 0.0
        return round(sum(1 for result in self.results if result.passed) / len(self.results), 2)

    def to_json(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "passed": self.passed,
            "score": self.score,
            "results": [result.to_json() for result in self.results],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Hermes Learning System Eval Report",
            "",
            f"Suite: `{self.suite}`",
            f"Passed: `{self.passed}`",
            f"Score: `{self.score}`",
            "",
            "## Results",
        ]
        for result in self.results:
            status = "PASS" if result.passed else "FAIL"
            lines.extend(
                [
                    "",
                    f"- `{result.id}`: **{status}**",
                    f"  - instinct: `{result.instinct_id}`",
                    f"  - scope: `{result.scope}`",
                    f"  - confidence: `{result.confidence}`",
                ]
            )
            for failure in result.failures:
                lines.append(f"  - failure: {failure}")
        return "\n".join(lines) + "\n"


def built_in_eval_suite() -> EvalSuite:
    """Return the default offline regression probes for HLS."""

    return EvalSuite(
        cases=[
            EvalCase(
                id="transferable-learning",
                text="Do not learn Hyperframes as DBM-only; learn every capability as transferable.",
                source="builtin-eval",
                project_id="dbm",
                tags=["learning"],
                expected_instinct_id="learn-capabilities-as-transferable-domains",
                expected_scope="global",
                min_confidence=0.9,
                forbidden_status_fragments=["Do not learn Hyperframes"],
            ),
            EvalCase(
                id="privacy-boundary",
                text="Never expose private operator data while learning.",
                source="builtin-eval",
                tags=["privacy"],
                expected_instinct_id="private-by-default-learning",
                expected_scope="global",
                min_confidence=0.85,
                forbidden_status_fragments=["Never expose private operator data"],
            ),
            EvalCase(
                id="project-specific-workflow",
                text="In repo alpha, tests must be run with pytest -n 4 before committing.",
                source="builtin-eval",
                project_id="repo-alpha",
                tags=["testing"],
                expected_scope="project",
                min_confidence=0.5,
            ),
        ]
    )


def run_eval_suite(suite: EvalSuite, *, root: str | Path) -> EvalReport:
    """Run deterministic HLS probes against an isolated LearningEngine root."""

    engine = LearningEngine(root)
    results = []
    for case in suite.cases:
        observation = Observation(
            kind="eval_probe",
            text=case.text,
            source=case.source,
            project_id=case.project_id,
            project_name=case.project_name,
            tags=list(case.tags),
        )
        instinct = engine.ingest_observation(observation)
        failures = _check_case(case, instinct, engine.status())
        results.append(
            EvalResult(
                id=case.id,
                passed=not failures,
                failures=failures,
                instinct_id=instinct.id,
                scope=instinct.scope.value,
                confidence=instinct.confidence,
            )
        )
    return EvalReport(suite=suite.name, results=results)


def _check_case(case: EvalCase, instinct, status: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if case.expected_instinct_id and instinct.id != case.expected_instinct_id:
        failures.append(f"expected instinct {case.expected_instinct_id}, got {instinct.id}")
    if case.expected_scope and instinct.scope.value != case.expected_scope:
        failures.append(f"expected scope {case.expected_scope}, got {instinct.scope.value}")
    if case.min_confidence is not None and instinct.confidence < case.min_confidence:
        failures.append(
            f"expected confidence >= {case.min_confidence}, got {instinct.confidence}"
        )
    payload = json.dumps(status, ensure_ascii=False)
    for fragment in case.forbidden_status_fragments:
        if fragment in payload:
            failures.append(f"forbidden status fragment leaked: {fragment}")
    return failures
