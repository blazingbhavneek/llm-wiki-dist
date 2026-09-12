"""neo: overlapping source observation, seed planning, and CLI-agent rewrites.

The runtime never reads ``PROMPT.md``.  Python owns source ranges, numbered
filenames, provenance, retries, and publication. Planning calls are bounded;
each rewrite task plans and then edits one page body only.
"""

from __future__ import annotations

from .config import NeoConfig
from .pipeline import PipelineError, run_pipeline

__all__ = ["NeoConfig", "PipelineError", "run_pipeline"]
