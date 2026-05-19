from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable


class Scope(StrEnum):
    """Where an instinct is allowed to apply."""

    GLOBAL = "global"
    PROJECT = "project"


@dataclass(frozen=True)
class Observation:
    """A raw learning signal captured from a session or an explicit correction."""

    kind: str
    text: str
    source: str
    project_id: str | None = None
    project_name: str | None = None
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def from_user_correction(
        cls,
        *,
        text: str,
        source: str,
        project_id: str | None = None,
        project_name: str | None = None,
        tags: list[str] | None = None,
    ) -> "Observation":
        return cls(
            kind="user_correction",
            text=text,
            source=source,
            project_id=project_id,
            project_name=project_name,
            tags=list(tags or []),
        )


@dataclass(frozen=True)
class Instinct:
    """An atomic learned behavior with confidence and evidence metadata."""

    id: str
    trigger: str
    action: str
    scope: Scope
    confidence: float
    domain: str
    project_id: str | None = None
    project_name: str | None = None
    evidence_count: int = 1
    evidence_sources: tuple[str, ...] = ()
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    path: str | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["scope"] = self.scope.value
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Instinct":
        payload = dict(data)
        payload["scope"] = Scope(payload["scope"])
        payload["evidence_sources"] = tuple(payload.get("evidence_sources", ()))
        return cls(**payload)


class LearningEngine:
    """Small generic engine for turning observations into scoped instincts.

    The engine is deliberately deterministic and offline-first. It stores no raw
    observation text in status exports, and all paths are rooted in the caller's
    chosen data directory so private overlays can own sensitive content.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.global_dir = self.root / "instincts" / "global"
        self.projects_dir = self.root / "projects"
        self.observations_dir = self.root / "observations"
        for path in (self.global_dir, self.projects_dir, self.observations_dir):
            path.mkdir(parents=True, exist_ok=True)

    def ingest_observation(self, observation: Observation) -> Instinct:
        self._record_observation_metadata(observation)
        draft = self._derive_instinct(observation)
        existing = self.load_instinct(draft.id, draft.scope, project_id=draft.project_id)
        if existing:
            instinct = self._merge_instinct(existing, draft)
        else:
            instinct = draft
        self._write_instinct(instinct)
        return instinct

    def load_instinct(
        self, instinct_id: str, scope: Scope, *, project_id: str | None = None
    ) -> Instinct | None:
        path = self._instinct_path(instinct_id, scope, project_id=project_id)
        if not path.exists():
            return None
        return Instinct.from_json(json.loads(path.read_text(encoding="utf-8")))

    def update_confidence(
        self, instinct_id: str, scope: Scope, *, confidence: float, project_id: str | None = None
    ) -> Instinct:
        instinct = self.load_instinct(instinct_id, scope, project_id=project_id)
        if instinct is None:
            raise KeyError(instinct_id)
        updated = Instinct(
            **{
                **instinct.to_json(),
                "scope": instinct.scope,
                "confidence": round(confidence, 2),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        self._write_instinct(updated)
        return updated

    def promote_cross_project_instincts(
        self, *, min_projects: int = 2, min_average_confidence: float = 0.8
    ) -> list[Instinct]:
        grouped: dict[str, list[Instinct]] = {}
        for instinct in self._iter_project_instincts():
            grouped.setdefault(instinct.id, []).append(instinct)

        promoted: list[Instinct] = []
        for instinct_id, instincts in sorted(grouped.items()):
            project_ids = {item.project_id for item in instincts if item.project_id}
            if len(project_ids) < min_projects:
                continue
            average = sum(item.confidence for item in instincts) / len(instincts)
            if average < min_average_confidence:
                continue
            first = instincts[0]
            global_instinct = Instinct(
                id=instinct_id,
                trigger=first.trigger,
                action=first.action,
                scope=Scope.GLOBAL,
                confidence=round(average, 2),
                domain=first.domain,
                evidence_count=sum(item.evidence_count for item in instincts),
                evidence_sources=tuple(sorted({src for item in instincts for src in item.evidence_sources})),
                created_at=first.created_at,
                updated_at=datetime.now(UTC).isoformat(),
            )
            self._write_instinct(global_instinct)
            promoted.append(global_instinct)
        return promoted

    def status(self) -> dict[str, Any]:
        global_instincts = list(self._iter_global_instincts())
        project_instincts = list(self._iter_project_instincts())
        return {
            "global_instincts": len(global_instincts),
            "project_instincts": len(project_instincts),
            "global_ids": sorted(item.id for item in global_instincts),
            "project_ids": sorted(item.id for item in project_instincts),
            "projects": sorted({item.project_id for item in project_instincts if item.project_id}),
        }

    def _derive_instinct(self, observation: Observation) -> Instinct:
        text = observation.text.lower()
        tags = {tag.lower() for tag in observation.tags}
        if "transferable" in text or "general" in text or "hyperframes" in text and "dbm" in text:
            return self._make_instinct(
                observation,
                instinct_id="learn-capabilities-as-transferable-domains",
                trigger="when learning any new tool, framework, workflow, or method",
                action=(
                    "Map the full professional capability surface, then generalize the "
                    "capability as transferable across projects instead of binding it to "
                    "the first triggering use case."
                ),
                scope=Scope.GLOBAL,
                confidence=0.9,
                domain="learning-doctrine",
            )
        if "private" in text or "secret" in text or "privacy" in tags:
            return self._make_instinct(
                observation,
                instinct_id="private-by-default-learning",
                trigger="when storing or promoting learned behavior",
                action=(
                    "Keep private operator data, secrets, and business-specific doctrine "
                    "out of public code; store only generic mechanisms in the fork."
                ),
                scope=Scope.GLOBAL,
                confidence=0.85,
                domain="privacy",
            )

        return self._make_instinct(
            observation,
            instinct_id=self._slug(observation.text),
            trigger=f"when similar {observation.kind} signals appear",
            action=self._summarize_action(observation.text),
            scope=Scope.PROJECT if observation.project_id else Scope.GLOBAL,
            confidence=0.5,
            domain=next(iter(tags), observation.kind),
        )

    def _make_instinct(
        self,
        observation: Observation,
        *,
        instinct_id: str,
        trigger: str,
        action: str,
        scope: Scope,
        confidence: float,
        domain: str,
    ) -> Instinct:
        project_id = observation.project_id if scope == Scope.PROJECT else None
        project_name = observation.project_name if scope == Scope.PROJECT else None
        path = self._instinct_path(instinct_id, scope, project_id=project_id)
        return Instinct(
            id=instinct_id,
            trigger=trigger,
            action=action,
            scope=scope,
            confidence=confidence,
            domain=domain,
            project_id=project_id,
            project_name=project_name,
            evidence_sources=(observation.source,),
            path=str(path),
        )

    def _merge_instinct(self, existing: Instinct, draft: Instinct) -> Instinct:
        confidence = min(0.95, max(existing.confidence, draft.confidence) + 0.05)
        return Instinct(
            id=existing.id,
            trigger=existing.trigger,
            action=existing.action,
            scope=existing.scope,
            confidence=round(confidence, 2),
            domain=existing.domain,
            project_id=existing.project_id,
            project_name=existing.project_name,
            evidence_count=existing.evidence_count + 1,
            evidence_sources=tuple(sorted(set(existing.evidence_sources + draft.evidence_sources))),
            created_at=existing.created_at,
            updated_at=datetime.now(UTC).isoformat(),
            path=existing.path,
        )

    def _write_instinct(self, instinct: Instinct) -> None:
        path = self._instinct_path(instinct.id, instinct.scope, project_id=instinct.project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**instinct.to_json(), "path": str(path)}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _record_observation_metadata(self, observation: Observation) -> None:
        # Store metadata only by default; raw text can be kept by a private overlay if desired.
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        path = self.observations_dir / f"{day}.jsonl"
        record = {
            "kind": observation.kind,
            "source": observation.source,
            "project_id": observation.project_id,
            "project_name": observation.project_name,
            "tags": observation.tags,
            "created_at": observation.created_at,
            "text_sha256_hint": self._stable_hint(observation.text),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def _instinct_path(self, instinct_id: str, scope: Scope, *, project_id: str | None = None) -> Path:
        filename = f"{instinct_id}.json"
        if scope == Scope.GLOBAL:
            return self.global_dir / filename
        if not project_id:
            raise ValueError("project_id is required for project-scoped instincts")
        return self.projects_dir / self._slug(project_id) / "instincts" / filename

    def _iter_global_instincts(self) -> Iterable[Instinct]:
        yield from self._iter_instinct_files(self.global_dir.glob("*.json"))

    def _iter_project_instincts(self) -> Iterable[Instinct]:
        yield from self._iter_instinct_files(self.projects_dir.glob("*/instincts/*.json"))

    def _iter_instinct_files(self, paths: Iterable[Path]) -> Iterable[Instinct]:
        for path in sorted(paths):
            yield Instinct.from_json(json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    def _summarize_action(text: str) -> str:
        cleaned = " ".join(text.strip().split())
        return cleaned[:220]

    @staticmethod
    def _slug(text: str) -> str:
        words = re.findall(r"[a-z0-9]+", text.lower())[:7]
        return "-".join(words) or "untitled-instinct"

    @staticmethod
    def _stable_hint(text: str) -> str:
        import hashlib

        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
