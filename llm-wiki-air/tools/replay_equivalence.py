"""Standard-path equivalence: run THIS tree's wiki + linker on real raw docs with a scripted
fake model and dump every prompt (with kwargs) and every output, so two trees can be diffed.

    git worktree add --detach /tmp/base a9a7dcf
    (cd /tmp/base/llm-wiki-air && PYTHONPATH=. ../../<repo>/llm-wiki-air/.venv/bin/python \
        <repo>/llm-wiki-air/tools/replay_equivalence.py <repo>/llm-wiki-air/data_std/mountdocs/raw /tmp/rp-base /tmp/base.json <repo>/llm-wiki-air/data_std/mountdocs)
    (cd <repo>/llm-wiki-air && PYTHONPATH=. .venv/bin/python tools/replay_equivalence.py \
        data_std/mountdocs/raw /tmp/rp-now /tmp/now.json data_std/mountdocs)
    .venv/bin/python tools/replay_equivalence.py --compare /tmp/base.json /tmp/now.json

Expected: identical call SETS, identical pages, identical linker output (call ORDER may differ:
pages are scheduled concurrently). No GROWI, parser or real model is used.
"""

import asyncio
import hashlib
import json
import re
import shutil
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

if sys.argv[1] == "--compare":
    a, b = (json.loads(Path(name).read_text()) for name in sys.argv[2:4])
    same = True
    for name in sorted(set(a["wiki"]) | set(b["wiki"])):
        x, y = a["wiki"].get(name, {}), b["wiki"].get(name, {})
        calls = sorted(json.dumps(c, sort_keys=True, ensure_ascii=False) for c in x.get("calls", [])) == sorted(json.dumps(c, sort_keys=True, ensure_ascii=False) for c in y.get("calls", []))
        ok = calls and x.get("docs") == y.get("docs") and x.get("error") == y.get("error")
        same &= ok
        print(f"{'OK  ' if ok else 'DIFF'} {name}: calls={calls} pages={x.get('docs') == y.get('docs')} error={x.get('error') == y.get('error')}")
    la, lb = a["linker"], b["linker"]
    ok = (sorted(json.dumps(c, sort_keys=True, ensure_ascii=False) for c in la["calls"]) == sorted(json.dumps(c, sort_keys=True, ensure_ascii=False) for c in lb["calls"])
          and la["pages"] == lb["pages"] and la["markers"] == lb["markers"])
    print(f"{'OK  ' if ok else 'DIFF'} linker")
    raise SystemExit(0 if same and ok else 1)

RAW = Path(sys.argv[1])          # data_std/mountdocs/raw
WORK = Path(sys.argv[2])         # scratch dir for this tree
OUT = Path(sys.argv[3])          # json report
STD_WIKI = Path(sys.argv[4])     # data_std/mountdocs (for the linker run)

log: list[dict] = []


def content(messages):
    return [str(getattr(m, "content", m)) for m in messages]


class Fake:
    name = "fake"
    provider = "fake"

    async def structured(self, schema, messages, **kwargs):
        log.append({"kind": "structured", "schema": schema.__name__, "messages": content(messages),
                    "kwargs": {k: v for k, v in kwargs.items()}})
        try:
            return schema.model_validate({})
        except Exception:
            return schema.model_construct()

    async def text(self, messages, **kwargs):
        texts = content(messages)
        log.append({"kind": "text", "messages": texts, "kwargs": {k: v for k, v in kwargs.items()}})
        body = texts[-1]
        marker = "--- 行番号付き原文"
        if marker in body:
            source = body.split(marker, 1)[1].split("\n", 1)[1]
            return "\n".join(re.sub(r"^\s*\d+:\s?", "", line) for line in source.splitlines())
        if "JSON" in body or "json" in body:
            return '{"summary":"要約","keywords":["鍵"],"search_terms":["語"]}'
        return "要約。"


def settings(**extra):
    base = dict(chat_base_url="", chat_api_key="local", chat_model="fake", concurrency=1,
                ingest_concurrency=1, wiki_output_language="Japanese (日本語)", wiki_request_timeout=60,
                wiki_linker_mode="neo", wiki_linker_judge="llm", wiki_linker_enabled=True,
                wiki_linker_concurrency=1, wiki_rewrite_concurrency=1, wiki_planner_concurrency=1)
    base.update(extra)
    return SimpleNamespace(**base)


def tree_files(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            text = path.read_bytes()
            text = text.replace(str(WORK).encode(), b"<WORK>")
            out[path.relative_to(root).as_posix()] = hashlib.sha256(text).hexdigest()[:16]
    return out


def scrub(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str).replace(str(WORK), "<WORK>"))


report = {"wiki": {}, "linker": {}}
from graph.workspace.writer import build_wiki_output  # noqa: E402

for raw in sorted(RAW.glob("*.md")):
    out_dir = WORK / "out" / raw.stem
    state = WORK / "state" / raw.stem
    start = len(log)
    try:
        build_wiki_output(source_path=raw, document_name=raw.name, out_dir=out_dir, mode="wiki",
                          settings=settings(), llm=Fake(), embedder=None, state_dir=state)
        error = None
    except Exception as exc:  # noqa: BLE001 - compared, not raised
        error = f"{type(exc).__name__}: {exc}"
    docs = out_dir / "docs"
    report["wiki"][raw.name] = {
        "error": error,
        "calls": scrub(log[start:]),
        "docs": {p.name: p.read_text(encoding="utf-8").replace(str(WORK), "<WORK>") for p in sorted(docs.glob("*.md"))} if docs.exists() else {},
    }

# linker: the real standard wiki pages, linker state cleared
project_root = WORK / "linkproj" / "mountdocs"
shutil.copytree(STD_WIKI, project_root, symlinks=True)
for marker in ("linker.json", "chunks.json", "links.json"):
    for path in (project_root / "wiki").rglob(f"_planning/{marker}"):
        path.unlink()
(project_root / "metadata" / "wiki-linker.sqlite").unlink(missing_ok=True)
from graph.workspace.project import Project  # noqa: E402
from graph.linker import link_documents  # noqa: E402

project = Project(project_root, project_root / "mount")
rels = sorted(p.name for p in (project_root / "raw").glob("*.md"))
start = len(log)
try:
    result = asyncio.run(link_documents(project, rels, model=Fake(), embedder=None, settings=settings()))
    error = None
    summary = {k: getattr(result, k) for k in ("edges_added", "edges_removed", "meta_calls", "edge_calls", "meta_fallbacks")}
except Exception:  # noqa: BLE001
    error = traceback.format_exc()[-800:]
    summary = {}
pages = {}
for path in sorted((project_root / "wiki").rglob("*.md")):
    if "_planning" in path.parts:
        continue
    pages[path.relative_to(project_root / "wiki").as_posix()] = path.read_text(encoding="utf-8")
markers = {}
for path in sorted((project_root / "wiki").rglob("_planning/linker.json")):
    data = json.loads(path.read_text())
    markers[path.parent.parent.name] = {k: v for k, v in data.items() if k not in {"run_id", "finished_at"}}
report["linker"] = {"error": error, "summary": summary, "calls": scrub(log[start:]), "pages": pages, "markers": markers}
OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True))
print("calls", len(log), "wiki errors", {k: v["error"] for k, v in report["wiki"].items() if v["error"]}, "linker error", bool(error))
