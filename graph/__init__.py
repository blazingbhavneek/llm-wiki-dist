"""Expose the application package when commands run from the repository root."""

from pathlib import Path

# The deployable Python application is kept in the historical nested directory.
# Extending this package path makes `python -m graph.neo ...` work from either
# the repository root or that application directory without duplicating code.
__path__ = [str(Path(__file__).resolve().parent.parent / "llm-wiki-dist" / "graph")]
