"""Explicit generation/linking policies.

Fast-path behaviour is selected by ``settings.policy == "fast"`` at each
branch (see fast-path-proposal.md); this object carries only what is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Policy:
    name: Literal["standard", "fast"] = "standard"
    version: str = "standard-v1"
    repair_attempts: int = 3


STANDARD = Policy()
FAST = Policy(name="fast", version="fast-v1", repair_attempts=1)


def resolve_policy(value: str | None) -> Policy:
    normalized = str(value or "standard").strip().lower()
    if normalized in {"", "standard", "standard-v1"}:
        return STANDARD
    if normalized in {"fast", "fast-v1"}:
        return FAST
    raise ValueError("policy must be standard or fast")


__all__ = ["FAST", "STANDARD", "Policy", "resolve_policy"]
