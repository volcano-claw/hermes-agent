"""Hermes Learning System plugin package.

This package intentionally contains only generic learning primitives. Private
operator doctrine, project secrets, and business-specific rules belong in a
private overlay or external memory store, not in this public fork package.
"""

from .engine import Instinct, LearningEngine, Observation, Scope

__all__ = ["Instinct", "LearningEngine", "Observation", "Scope"]
