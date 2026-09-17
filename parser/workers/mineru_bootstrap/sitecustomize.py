"""Process-local PyTorch compatibility settings for the MinerU CLI.

Python imports ``sitecustomize`` during startup when its directory is present
on ``PYTHONPATH``. Keeping this shim in a dedicated directory ensures the
setting applies only to MinerU subprocesses, not to the parser server itself.
"""

from __future__ import annotations

import os


def _enabled(value: str) -> bool:
    return value.strip().lower() not in {"0", "false", "no", "off"}


if _enabled(os.getenv("MINERU_DISABLE_CUDNN_SDPA", "true")):
    try:
        import torch

        # UniMERNet's decoder uses scaled_dot_product_attention. On some
        # PyTorch/cuDNN/GPU combinations cuDNN accepts the operation but cannot
        # build an execution plan for a particular formula shape. Disabling
        # only this backend lets PyTorch select Flash, memory-efficient, or math
        # SDPA instead.
        torch.backends.cuda.enable_cudnn_sdp(False)
    except (AttributeError, ImportError):
        pass
