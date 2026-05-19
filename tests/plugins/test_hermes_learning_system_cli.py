from __future__ import annotations

import json
from pathlib import Path

from plugins.hermes_learning_system.cli import main


def test_cli_ingest_status_and_promote(tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "ingest",
            "--kind",
            "user_correction",
            "--source",
            "test",
            "--project-id",
            "alpha",
            "--tag",
            "learning",
            "Do not learn a capability as project-only; make it transferable.",
        ]
    )
    assert exit_code == 0
    created = json.loads(capsys.readouterr().out)
    assert created["id"] == "learn-capabilities-as-transferable-domains"
    assert created["scope"] == "global"

    exit_code = main(["--root", str(tmp_path), "status"])
    assert exit_code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["global_instincts"] == 1
    assert "learn-capabilities-as-transferable-domains" in status["global_ids"]
