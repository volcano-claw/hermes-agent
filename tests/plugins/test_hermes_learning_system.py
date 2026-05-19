from __future__ import annotations

import json
from pathlib import Path

from plugins.hermes_learning_system.engine import (
    LearningEngine,
    Observation,
    Scope,
)


def test_user_correction_becomes_global_high_confidence_instinct(tmp_path: Path) -> None:
    engine = LearningEngine(tmp_path)

    observation = Observation.from_user_correction(
        text="Do not learn Hyperframes as DBM-only; learn every capability as transferable.",
        source="telegram",
        project_id="dbm",
        tags=["learning", "doctrine"],
    )

    instinct = engine.ingest_observation(observation)

    assert instinct.id == "learn-capabilities-as-transferable-domains"
    assert instinct.scope == Scope.GLOBAL
    assert instinct.confidence >= 0.9
    assert "transferable" in instinct.action
    assert instinct.evidence_count == 1
    assert engine.load_instinct(instinct.id, Scope.GLOBAL) == instinct


def test_project_specific_observation_stays_project_scoped(tmp_path: Path) -> None:
    engine = LearningEngine(tmp_path)

    observation = Observation(
        kind="workflow",
        text="In repo alpha, tests must be run with pytest -n 4 before committing.",
        source="cli",
        project_id="repo-alpha",
        project_name="Repo Alpha",
        tags=["testing"],
    )

    instinct = engine.ingest_observation(observation)

    assert instinct.scope == Scope.PROJECT
    assert instinct.project_id == "repo-alpha"
    assert "repo-alpha" in str(instinct.path)
    assert instinct.confidence == 0.5


def test_repeated_project_instinct_can_be_promoted_to_global(tmp_path: Path) -> None:
    engine = LearningEngine(tmp_path)

    obs_a = Observation(
        kind="workflow",
        text="Always verify generated videos with a visual snapshot before reporting.",
        source="telegram",
        project_id="project-a",
        project_name="Project A",
        tags=["verification"],
    )
    obs_b = Observation(
        kind="workflow",
        text="Always verify generated videos with a visual snapshot before reporting.",
        source="telegram",
        project_id="project-b",
        project_name="Project B",
        tags=["verification"],
    )

    first = engine.ingest_observation(obs_a)
    second = engine.ingest_observation(obs_b)
    engine.update_confidence(first.id, Scope.PROJECT, project_id="project-a", confidence=0.85)
    engine.update_confidence(second.id, Scope.PROJECT, project_id="project-b", confidence=0.9)

    promoted = engine.promote_cross_project_instincts(min_projects=2, min_average_confidence=0.8)

    assert [item.id for item in promoted] == [first.id]
    global_instinct = engine.load_instinct(first.id, Scope.GLOBAL)
    assert global_instinct is not None
    assert global_instinct.scope == Scope.GLOBAL
    assert global_instinct.confidence == 0.88
    assert global_instinct.evidence_count == 2


def test_export_status_contains_counts_without_raw_observation_text(tmp_path: Path) -> None:
    engine = LearningEngine(tmp_path)
    engine.ingest_observation(
        Observation.from_user_correction(
            text="Never expose private business data while learning.",
            source="telegram",
            project_id=None,
            tags=["privacy"],
        )
    )

    status = engine.status()
    payload = json.dumps(status, ensure_ascii=False)

    assert status["global_instincts"] == 1
    assert status["project_instincts"] == 0
    assert "Never expose private business data" not in payload
    assert "private-by-default-learning" in payload
