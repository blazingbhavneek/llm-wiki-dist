"""Compatibility export; the writer lives in graph.workspace.writer."""

from .workspace.writer import *  # noqa: F401,F403
from .workspace.writer import (  # noqa: F401
    WriteResult,
    build_wiki_output,
    publish_output,
    run_linker,
    run_wiki,
    run_wiki_linker,
    up_to_date,
    wiki_config,
    write_index,
    write_source_stamp,
    write_wiki,
)
