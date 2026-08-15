"""Benchmark package.

The maintained command line lives in :mod:`benchmark.runner`.  The original
single-file harness is executed into this package namespace for backwards
compatibility with existing imports and its worker subprocess protocol while
the dataset, client, and orchestration code is split into focused modules.
"""

from __future__ import annotations

from pathlib import Path as _Path


_PACKAGE_DIR = _Path(__path__[0]).resolve()  # type: ignore[name-defined]
_LEGACY_SOURCE = _PACKAGE_DIR.parent / "benchmark.py"

# Keep ``__file__`` pointed at the original worker entry point.  A few of the
# compatibility functions launch that exact file in an isolated environment.
__file__ = str(_LEGACY_SOURCE)
exec(compile(_LEGACY_SOURCE.read_bytes(), str(_LEGACY_SOURCE), "exec"), globals())

