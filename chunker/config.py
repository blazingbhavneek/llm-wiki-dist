"""
ChunkConfig: every knob of the chunking pipeline in one dataclass.

Ablation model: each pipeline feature is an independent boolean. Turning a
feature off can only remove its score contribution (or skip its LLM calls);
the DP assembler always runs and always emits a valid exact partition, so any
combination of switches still produces correct output — just cheaper/rougher.

Speed ladder (fastest -> best):
    use_skeleton_llm=False, use_adjudicator=False   pure deterministic signals
    use_skeleton_llm=True,  use_adjudicator=False   + few selection-only calls
    use_skeleton_llm=True,  use_adjudicator=True    + bounded yes/no votes
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw not in {"0", "false", "False", ""}


@dataclass
class ChunkConfig:
    # --- Stage 1 signals (ablation switches) ---
    use_structure: bool = True  # regex heading/numbering/blank-run prior
    use_texttiling: bool = True  # lexical-cohesion depth score
    use_sat: bool = False  # wtpsplit paragraph probability (optional dep)
    use_ppl: bool = False  # local-model perplexity spike (optional endpoint)

    # --- Stage 2/4 LLM features (ablation switches) ---
    use_skeleton_llm: bool = True  # pattern-class labeling + top-level ID picks
    use_adjudicator: bool = True  # yes/no majority vote on ambiguous cuts

    # --- signal fusion weights (applied to per-doc z-scores) ---
    weight_structure: float = 1.0
    weight_texttiling: float = 0.6
    weight_sat: float = 0.8
    weight_ppl: float = 0.6

    # --- size band (lines) enforced by the DP cost, not by prompt prose ---
    size_min: int = 100
    size_target: int = 250
    size_max: int = 400
    size_hard_max: int = 700

    # --- skeleton / candidate extraction ---
    skeleton_max_candidates: int = 200  # cap on lattice peaks shown to the LLM
    skeleton_min_gap: int = 3  # min lines between two candidates
    skeleton_recursion_depth: int = 2  # top-level pass + one refinement pass

    # --- adjudication bounds (hard cost ceiling) ---
    adjudicate_votes: int = 3  # k samples, majority
    adjudicate_max_cuts: int = 20  # never more than this many LLM cut reviews
    adjudicate_zscore_band: float = 1.0  # cuts with fused z below this are "ambiguous"

    # --- misc ---
    detect_repeated_lines: bool = True  # page header/footer suppression
    ppl_base_url: str = ""  # OpenAI-compatible /completions endpoint for logprobs
    ppl_model: str = ""

    @classmethod
    def from_env(cls) -> "ChunkConfig":
        cfg = cls(
            use_structure=_env_bool("CHUNK_USE_STRUCTURE", cls.use_structure),
            use_texttiling=_env_bool("CHUNK_USE_TEXTTILING", cls.use_texttiling),
            use_sat=_env_bool("CHUNK_USE_SAT", cls.use_sat),
            use_ppl=_env_bool("CHUNK_USE_PPL", cls.use_ppl),
            use_skeleton_llm=_env_bool("CHUNK_USE_SKELETON_LLM", cls.use_skeleton_llm),
            use_adjudicator=_env_bool("CHUNK_USE_ADJUDICATOR", cls.use_adjudicator),
            size_min=int(os.environ.get("CHUNK_SIZE_MIN", cls.size_min)),
            size_target=int(os.environ.get("CHUNK_SIZE_TARGET", cls.size_target)),
            size_max=int(os.environ.get("CHUNK_SIZE_MAX", cls.size_max)),
            size_hard_max=int(os.environ.get("CHUNK_SIZE_HARD_MAX", cls.size_hard_max)),
            ppl_base_url=os.environ.get("CHUNK_PPL_BASE_URL", cls.ppl_base_url),
            ppl_model=os.environ.get("CHUNK_PPL_MODEL", cls.ppl_model),
        )
        return cfg

    def apply_overrides(self, overrides: dict | None) -> "ChunkConfig":
        """Per-request overrides (API payload `chunk_options`). Unknown keys
        are ignored so old clients never break the pipeline."""
        if not overrides:
            return self
        valid = {f.name: f.type for f in fields(self)}
        for key, value in overrides.items():
            if key not in valid or value is None:
                continue
            current = getattr(self, key)
            if isinstance(current, bool):
                setattr(self, key, bool(value))
            elif isinstance(current, int):
                setattr(self, key, int(value))
            elif isinstance(current, float):
                setattr(self, key, float(value))
            else:
                setattr(self, key, str(value))
        return self

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}
