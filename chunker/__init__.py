"""
Signal-fused markdown chunker (HBLF: hierarchical boundary-lattice fusion).

Public API:
    run_chunk_pipeline(source_text, document_name, out_dir, config, llm, on_progress)
    ChunkConfig  - all ablation switches (see config.py docstring)
    ChunkResult

Consumed by graph.librarian (job type "chunk_and_ingest") and by the
standalone CLI (../chunk.py). The output directory is directly ingestable by
Librarian.ingest_md_output.
"""

from .config import ChunkConfig
from .pipeline import ChunkResult, run_chunk_pipeline

__all__ = ["ChunkConfig", "ChunkResult", "run_chunk_pipeline"]
