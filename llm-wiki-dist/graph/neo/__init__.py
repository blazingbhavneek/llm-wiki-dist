"""neo: overlapping observation, seed planning, and section-wise lossless rewriting."""

from __future__ import annotations

from .config import NeoConfig
from .pipeline import PipelineError, run_pipeline

__all__ = ["NeoConfig", "PipelineError", "run_pipeline"]
