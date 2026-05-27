#!/usr/bin/env python3
"""Hermes Learning System tools.

Small tool wrappers around ``plugins.hermes_learning_system`` so gateway/CLI
agents can use the generic learning primitive without shelling out.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from plugins.hermes_learning_system.engine import LearningEngine, Observation
from plugins.hermes_learning_system.eval import built_in_eval_suite, run_eval_suite
from tools.registry import registry


def _default_root() -> Path:
    configured = os.getenv("HERMES_LEARNING_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path(get_hermes_home()) / "learning"


def _engine(root: str | None = None) -> LearningEngine:
    return LearningEngine(Path(root).expanduser() if root else _default_root())


def check_hermes_learning_requirements() -> bool:
    try:
        _default_root().mkdir(parents=True, exist_ok=True)
        return True
    except Exception:
        return False


def hls_ingest(
    text: str,
    kind: str = "workflow",
    source: str = "tool",
    project_id: str | None = None,
    project_name: str | None = None,
    tags: list[str] | None = None,
    root: str | None = None,
) -> str:
    """Ingest one learning observation and return the derived instinct."""
    if not text or not str(text).strip():
        return json.dumps({"success": False, "error": "text is required"})
    observation = Observation(
        kind=str(kind or "workflow"),
        text=str(text),
        source=str(source or "tool"),
        project_id=project_id,
        project_name=project_name,
        tags=list(tags or []),
    )
    instinct = _engine(root).ingest_observation(observation)
    return json.dumps({"success": True, "instinct": instinct.to_json()}, ensure_ascii=False, sort_keys=True)


def hls_status(root: str | None = None) -> str:
    """Return counts and IDs only; raw observation text is intentionally omitted."""
    return json.dumps({"success": True, "status": _engine(root).status()}, ensure_ascii=False, sort_keys=True)


def hls_promote(
    min_projects: int = 2,
    min_average_confidence: float = 0.8,
    root: str | None = None,
) -> str:
    """Promote repeated high-confidence project instincts to global scope."""
    promoted = _engine(root).promote_cross_project_instincts(
        min_projects=int(min_projects),
        min_average_confidence=float(min_average_confidence),
    )
    return json.dumps(
        {"success": True, "promoted": [item.to_json() for item in promoted]},
        ensure_ascii=False,
        sort_keys=True,
    )


def hls_eval(root: str | None = None) -> str:
    """Run the built-in offline HLS regression/evaluation probes."""
    report = run_eval_suite(built_in_eval_suite(), root=Path(root).expanduser() if root else _default_root())
    return json.dumps({"success": True, "report": report.to_json()}, ensure_ascii=False, sort_keys=True)


HLS_INGEST_SCHEMA: dict[str, Any] = {
    "name": "hls_ingest",
    "description": (
        "Ingest a learning observation/correction into Hermes Learning System and return "
        "the derived scoped instinct. Use for durable generic learning signals after user "
        "corrections, repeated workflow lessons, or validated post-task insights. Do not "
        "store secrets or raw private business data."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Observation or correction text to learn from."},
            "kind": {"type": "string", "description": "Signal kind, e.g. user_correction, workflow, tool_outcome.", "default": "workflow"},
            "source": {"type": "string", "description": "Where the signal came from, e.g. telegram, cli, post_task.", "default": "tool"},
            "project_id": {"type": "string", "description": "Optional project scope id. Omit for global observations."},
            "project_name": {"type": "string", "description": "Optional human-readable project name."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional learning/domain tags."},
            "root": {"type": "string", "description": "Optional learning store root; defaults to $HERMES_HOME/learning."},
        },
        "required": ["text"],
    },
}

HLS_STATUS_SCHEMA: dict[str, Any] = {
    "name": "hls_status",
    "description": "Show Hermes Learning System counts and instinct IDs without raw observation text.",
    "parameters": {"type": "object", "properties": {"root": {"type": "string"}}, "required": []},
}

HLS_PROMOTE_SCHEMA: dict[str, Any] = {
    "name": "hls_promote",
    "description": "Promote repeated high-confidence project instincts into global instincts.",
    "parameters": {
        "type": "object",
        "properties": {
            "min_projects": {"type": "integer", "default": 2},
            "min_average_confidence": {"type": "number", "default": 0.8},
            "root": {"type": "string"},
        },
        "required": [],
    },
}

HLS_EVAL_SCHEMA: dict[str, Any] = {
    "name": "hls_eval",
    "description": "Run offline Hermes Learning System evaluation probes for scope/confidence/privacy behavior.",
    "parameters": {"type": "object", "properties": {"root": {"type": "string"}}, "required": []},
}


registry.register(
    name="hls_ingest",
    toolset="hermes_learning",
    schema=HLS_INGEST_SCHEMA,
    handler=lambda args, **kw: hls_ingest(
        text=args.get("text", ""),
        kind=args.get("kind", "workflow"),
        source=args.get("source", "tool"),
        project_id=args.get("project_id"),
        project_name=args.get("project_name"),
        tags=args.get("tags") or [],
        root=args.get("root"),
    ),
    check_fn=check_hermes_learning_requirements,
    emoji="🧠",
)

registry.register(
    name="hls_status",
    toolset="hermes_learning",
    schema=HLS_STATUS_SCHEMA,
    handler=lambda args, **kw: hls_status(root=args.get("root")),
    check_fn=check_hermes_learning_requirements,
    emoji="🧠",
)

registry.register(
    name="hls_promote",
    toolset="hermes_learning",
    schema=HLS_PROMOTE_SCHEMA,
    handler=lambda args, **kw: hls_promote(
        min_projects=args.get("min_projects", 2),
        min_average_confidence=args.get("min_average_confidence", 0.8),
        root=args.get("root"),
    ),
    check_fn=check_hermes_learning_requirements,
    emoji="🧠",
)

registry.register(
    name="hls_eval",
    toolset="hermes_learning",
    schema=HLS_EVAL_SCHEMA,
    handler=lambda args, **kw: hls_eval(root=args.get("root")),
    check_fn=check_hermes_learning_requirements,
    emoji="🧠",
)
