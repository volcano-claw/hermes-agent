from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from hermes_constants import get_hermes_home

from .engine import LearningEngine, Observation
from .eval import built_in_eval_suite, run_eval_suite


def default_root() -> Path:
    configured = os.getenv("HERMES_LEARNING_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path(get_hermes_home()) / "learning"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes Learning System primitives")
    parser.add_argument("--root", type=Path, default=default_root(), help="Learning store root")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Ingest one observation and emit the derived instinct")
    ingest.add_argument("text", help="Observation text")
    ingest.add_argument("--kind", default="workflow")
    ingest.add_argument("--source", default="cli")
    ingest.add_argument("--project-id")
    ingest.add_argument("--project-name")
    ingest.add_argument("--tag", action="append", default=[])

    sub.add_parser("status", help="Show instinct counts and IDs without raw observation text")

    promote = sub.add_parser("promote", help="Promote repeated high-confidence project instincts")
    promote.add_argument("--min-projects", type=int, default=2)
    promote.add_argument("--min-average-confidence", type=float, default=0.8)

    sub.add_parser("eval", help="Run the built-in offline HLS evaluation probes")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    engine = LearningEngine(args.root)

    if args.command == "ingest":
        observation = Observation(
            kind=args.kind,
            text=args.text,
            source=args.source,
            project_id=args.project_id,
            project_name=args.project_name,
            tags=list(args.tag),
        )
        instinct = engine.ingest_observation(observation)
        print(json.dumps(instinct.to_json(), ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "status":
        print(json.dumps(engine.status(), ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "promote":
        promoted = engine.promote_cross_project_instincts(
            min_projects=args.min_projects,
            min_average_confidence=args.min_average_confidence,
        )
        print(json.dumps([item.to_json() for item in promoted], ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "eval":
        report = run_eval_suite(built_in_eval_suite(), root=args.root)
        print(json.dumps(report.to_json(), ensure_ascii=False, sort_keys=True))
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
