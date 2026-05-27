from __future__ import annotations

import json
from pathlib import Path

from tools.hermes_learning import hls_eval, hls_ingest, hls_promote, hls_status


def test_hls_tool_ingest_status_and_eval(tmp_path: Path) -> None:
    root = str(tmp_path / "learning")

    ingest = json.loads(
        hls_ingest(
            text="Do not learn Hyperframes as DBM-only; learn every capability as transferable.",
            kind="user_correction",
            source="pytest",
            project_id="dbm",
            tags=["learning"],
            root=root,
        )
    )
    assert ingest["success"] is True
    assert ingest["instinct"]["id"] == "learn-capabilities-as-transferable-domains"
    assert ingest["instinct"]["scope"] == "global"

    status = json.loads(hls_status(root=root))
    assert status["success"] is True
    assert status["status"]["global_instincts"] == 1
    assert "Do not learn Hyperframes" not in json.dumps(status)

    eval_report = json.loads(hls_eval(root=str(tmp_path / "eval")))
    assert eval_report["success"] is True
    assert eval_report["report"]["passed"] is True


def test_hls_tool_promote(tmp_path: Path) -> None:
    root = str(tmp_path / "learning")
    for project_id in ("alpha", "beta"):
        result = json.loads(
            hls_ingest(
                text="Run pytest before committing in this repo.",
                kind="workflow",
                source="pytest",
                project_id=project_id,
                tags=["testing"],
                root=root,
            )
        )
        assert result["success"] is True

    promoted = json.loads(hls_promote(min_projects=2, min_average_confidence=0.5, root=root))
    assert promoted["success"] is True
    assert promoted["promoted"]
    assert promoted["promoted"][0]["scope"] == "global"
