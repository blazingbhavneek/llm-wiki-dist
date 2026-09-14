"""The wiki factory must import without the knowledge engine (ORG_AND_PORT.md C9)."""

from __future__ import annotations

import subprocess
import sys
import unittest

FACTORY = (
    "graph.config",
    "graph.common",
    "graph.clients.chat",
    "graph.clients.embeddings",
    "graph.formats",
    "graph.wiki.pipeline",
    "graph.wiki.legacy",
    "graph.linker",
    "graph.workspace.project",
    "graph.workspace.writer",
    "graph.growi.client",
    "graph.growi.publisher",
)
ENGINE_PREFIXES = (
    "app",
    "graph.librarian",
    "graph.researcher",
    "graph.realtime",
    "graph.store",
    "graph.vectors",
    "graph.neighborhood",
    "graph.knowledge",
    "graph.gateway",
    "torch",
    "sentence_transformers",
)

_PROBE = """
import sys
for name in {factory!r}:
    __import__(name)
loaded = sorted(m for m in sys.modules if m.split(".")[0] in ("app", "torch", "sentence_transformers") or any(m == p or m.startswith(p + ".") for p in {engine!r}))
print("\\n".join(loaded))
"""


class BoundaryTests(unittest.TestCase):
    def test_factory_packages_never_import_the_engine(self) -> None:
        probe = _PROBE.format(factory=FACTORY, engine=ENGINE_PREFIXES)
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "", f"engine modules imported by the factory:\n{result.stdout}")


if __name__ == "__main__":
    unittest.main()
