"""One writer entry point for chunks and wiki output."""

from __future__ import annotations

import shutil
import hashlib
import json
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from graph.config import app_concurrency

StopCheck = Callable[[], bool] | None
Progress = Callable[[dict[str, Any]], None] | None


@dataclass(frozen=True)
class WriteResult:
    target: Path
    touched: list[str]


def wiki_config(settings: Any, *, run_dir: Path, resume: bool = True, source_kind: str = "md"):
    from graph.wiki.config import WikiConfig

    concurrency = max(1, int(getattr(settings, "concurrency", app_concurrency())))
    return WikiConfig(
        chat_base_url=settings.chat_base_url,
        chat_api_key=settings.chat_api_key,
        chat_model=settings.chat_model,
        temperature=0.0,
        output_language=getattr(settings, "wiki_output_language", "Japanese (日本語)"),
        section_target_lines=int(getattr(settings, "wiki_section_target_lines", 80)),
        write_attempts=int(getattr(settings, "wiki_write_attempts", 3)),
        planner_concurrency=concurrency,
        rewrite_concurrency=int(
            getattr(settings, "wiki_rewrite_concurrency", concurrency)
        ),
        request_timeout=int(getattr(settings, "wiki_request_timeout", 300)),
        run_dir=str(run_dir),
        resume=resume,
        source_kind=source_kind,
        structure_target_lines=int(getattr(settings, "structure_target_lines", 250)),
        structure_min_lines=int(getattr(settings, "structure_min_lines", 40)),
        slide_delimiter=getattr(settings, "slide_delimiter", r"^## Slide (\d+)\s*$"),
        slide_title=getattr(settings, "slide_title", r"^### (.+?)\s*$"),
        pdf_use_headings=bool(getattr(settings, "pdf_use_headings", False)),
        tabular_slice_records=int(getattr(settings, "tabular_slice_records", 40)),
        tabular_preview_rows=int(getattr(settings, "tabular_preview_rows", 12)),
        tabular_preview_cols=int(getattr(settings, "tabular_preview_cols", 12)),
    )


def run_wiki(
    source_path: Path,
    *,
    run_dir: Path,
    settings: Any,
    llm: Any | None = None,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
    resume: bool = True,
    source_kind: str = "md",
) -> Path:
    from graph.common.async_tools import run_async_blocking
    from graph.wiki.pipeline import run_pipeline
    from graph.wiki.model import ChatModelPort

    config = wiki_config(settings, run_dir=run_dir, resume=resume, source_kind=source_kind)
    model = llm if hasattr(llm, "structured") and hasattr(llm, "text") else ChatModelPort(config, llm=llm) if llm is not None else None
    return run_async_blocking(
        run_pipeline(
            source_path,
            config=config,
            model=model,
            on_progress=on_progress,
            stop_check=stop_check,
        )
    )


def build_wiki_output(
    *,
    source_path: Path,
    document_name: str,
    out_dir: Path,
    mode: str,
    settings: Any,
    llm: Any,
    embedder: Any,
    state_dir: Path | None = None,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
    source_kind: str | None = None,
    resume: bool = True,
) -> SimpleNamespace:
    source_path = Path(source_path)
    out_dir = Path(out_dir)
    from graph.formats import is_tabular, kind_of

    if mode not in {"wiki", "chunks"}:
        raise ValueError("mode must be wiki or chunks")
    kind = source_kind or kind_of(document_name)
    if is_tabular(kind):
        from graph.common.async_tools import run_async_blocking
        from graph.formats import csv as csv_format, xlsx as xlsx_format
        from graph.wiki.model import ChatModelPort

        config = wiki_config(settings, run_dir=state_dir or (out_dir / "wiki-state"), resume=resume, source_kind=kind)
        runner = xlsx_format.run if kind == "xlsx" else csv_format.run
        model = llm if hasattr(llm, "structured") and hasattr(llm, "text") else ChatModelPort(config, llm=llm)
        run_async_blocking(runner(source_path, run_dir=out_dir, model=model, config=config, on_progress=on_progress, stop_check=stop_check))
        return SimpleNamespace(out_dir=out_dir, file_count=len(list((out_dir / "docs").glob("*.md"))))
    if mode == "wiki":
        from graph.wiki.export import export_ingest_layout

        run_root = run_wiki(
            source_path,
            run_dir=state_dir or (out_dir / "wiki-state"),
            settings=settings,
            llm=llm,
            on_progress=on_progress,
            stop_check=stop_check,
            source_kind=kind,
            resume=resume,
        )
        export_ingest_layout(run_root, out_dir, document_name=document_name)
        return SimpleNamespace(
            out_dir=out_dir, file_count=len(list((out_dir / "docs").glob("*.md")))
        )

    body = source_path.read_text(encoding="utf-8")
    from graph.wiki.legacy import run_chunk_pipeline

    return run_chunk_pipeline(
        source_text=body,
        document_name=document_name,
        out_dir=out_dir,
        llm=getattr(llm, "llm", llm),
        concurrency=max(
            1,
            int(
                getattr(
                    settings,
                    "ingest_concurrency",
                    getattr(settings, "concurrency", app_concurrency()),
                )
            ),
        ),
        on_progress=on_progress,
        stop_check=stop_check,
    )


def publish_output(staged: Path, target: Path) -> int:
    """Publish ``docs/*.md`` flat beside ``_planning/``."""

    staged = Path(staged)
    target = Path(target)
    preserved: dict[str, bytes] = {}
    old_planning = target / "_planning"
    for name in ("chunks.json", "links.json", "linker.json", "navigation.json"):
        path = old_planning / name
        if path.exists():
            preserved[name] = path.read_bytes()
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    count = 0
    for page in sorted((staged / "docs").glob("*.md")):
        shutil.copyfile(page, target / page.name)
        count += 1
    if (staged / "_planning").exists():
        shutil.copytree(staged / "_planning", target / "_planning")
    if preserved:
        (target / "_planning").mkdir(parents=True, exist_ok=True)
        for name, payload in preserved.items():
            (target / "_planning" / name).write_bytes(payload)
    return count


def write_wiki_pages(
    project: Any,
    rel: str,
    *,
    mode: str,
    settings: Any,
    llm: Any,
    embedder: Any,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
    resume: bool = True,
) -> WriteResult:
    from graph.formats import kind_of

    if resume:
        old_source = project.state_dir(rel) / "source" / "original.md"
        if old_source.exists():
            old_lines = old_source.read_text(encoding="utf-8").splitlines()
            new_text = project.raw_file(rel).read_text(encoding="utf-8")
            new_lines = new_text.splitlines()
            # ponytail: stdlib line diff; replace only if multi-MB changed Markdown proves slow.
            hunks = [
                (old_start + 1, old_end - old_start, new_start + 1, new_end - new_start)
                for tag, old_start, old_end, new_start, new_end in SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()
                if tag != "equal"
            ]
            if hunks:
                from graph.wiki.incremental import invalidate_pages
                invalidate_pages(project.state_dir(rel), hunks, new_text)
    work = project.work_dir(rel)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    try:
        result = build_wiki_output(
            source_path=project.raw_file(rel),
            document_name=rel,
            out_dir=work / "out",
            mode=mode,
            settings=settings,
            llm=llm,
            embedder=embedder,
            state_dir=project.state_dir(rel),
            on_progress=on_progress,
            stop_check=stop_check,
            source_kind=kind_of(rel),
            resume=resume,
        )
        target = project.wiki_dir(rel)
        publish_output(result.out_dir, target)
        write_source_stamp(target, project.raw_file(rel), rel)
        marker = target / "_planning" / "linker.json"
        status = "pending" if getattr(settings, "wiki_linker_enabled", True) else "disabled"
        from graph.wiki.storage import write_json_atomic
        write_json_atomic(marker, {"schema_version": 2, "status": status, "mode": str(getattr(settings, "wiki_linker_mode", "legacy"))})
        return WriteResult(target=target, touched=[])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def write_wiki(
    project: Any, rel: str, *, mode: str, settings: Any, llm: Any, embedder: Any,
    on_progress: Progress = None, stop_check: StopCheck = None,
) -> WriteResult:
    """Compatibility wrapper: generate one wiki and immediately link it."""
    result = write_wiki_pages(
        project, rel, mode=mode, settings=settings, llm=llm, embedder=embedder,
        on_progress=on_progress, stop_check=stop_check,
    )
    touched = run_linkers(
        project, [rel], settings=settings, llm=llm, embedder=embedder,
        on_progress=on_progress, stop_check=stop_check,
    )
    return WriteResult(target=result.target, touched=touched)


def run_linker(
    project: Any,
    rel: str,
    *,
    settings: Any,
    llm: Any,
    embedder: Any,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
) -> list[str]:
    """Run the sole cross-document linker after pristine pages are published."""

    from graph.wiki.storage import write_json_atomic

    marker_path = project.wiki_dir(rel) / "_planning" / "linker.json"
    if not getattr(settings, "wiki_linker_enabled", True):
        # disabled mode constructs no services and never opens the catalog
        write_json_atomic(marker_path, {"schema_version": 2, "status": "disabled"})
        return []

    from graph.common.async_tools import run_async_blocking
    from graph.wiki.model import ChatModelPort

    if hasattr(llm, "structured"):
        model = llm  # already a model port (the linker only needs structured()); no services to construct
    else:
        config = wiki_config(settings, run_dir=project.state_dir(rel))
        model = ChatModelPort(config, llm=llm)
    if embedder is None:
        try:
            from graph.clients.embeddings import Embedder

            embedder = Embedder(settings)
        except Exception as exc:
            embedder = None
            if on_progress:
                on_progress({"stage": "linker", "step": "embedder_unavailable", "error": str(exc)[:200]})
    from graph.linker import link_document
    result = run_async_blocking(link_document(project, rel, model=model, embedder=embedder, settings=settings, on_progress=on_progress, stop_check=stop_check))
    return result.touched_documents


run_wiki_linker = run_linker


def run_linkers(
    project: Any, rels: list[str], *, settings: Any, llm: Any, embedder: Any,
    on_progress: Progress = None, stop_check: StopCheck = None,
) -> list[str]:
    """Link a completed wiki batch and render affected pages once."""
    if not rels:
        return []
    from graph.wiki.storage import write_json_atomic
    if not getattr(settings, "wiki_linker_enabled", True):
        for rel in rels:
            write_json_atomic(project.wiki_dir(rel) / "_planning" / "linker.json", {"schema_version": 2, "status": "disabled"})
        return []
    from graph.common.async_tools import run_async_blocking
    from graph.linker import link_documents
    from graph.wiki.model import ChatModelPort
    model = llm if hasattr(llm, "structured") else ChatModelPort(wiki_config(settings, run_dir=project.metadata / "state" / "linker"), llm=llm) if llm is not None else None
    return run_async_blocking(link_documents(project, rels, model=model, embedder=embedder, settings=settings, on_progress=on_progress, stop_check=stop_check)).touched_documents


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_source_stamp(target: Path, raw_file: Path, rel: str) -> None:
    planning = Path(target) / "_planning"
    planning.mkdir(exist_ok=True)
    tmp = planning / "source.json.tmp"
    tmp.write_text(json.dumps({"raw": rel, "sha256": _sha256_file(raw_file)}), encoding="utf-8")
    tmp.replace(planning / "source.json")


def wiki_up_to_date(project: Any, rel: str) -> bool:
    planning = project.wiki_dir(rel) / "_planning"
    raw = project.raw_file(rel)
    current = _sha256_file(raw) if raw.exists() else ""
    for name, key in (("source.json", "sha256"), ("manifest.json", "source_sha256")):
        path = planning / name
        if not path.exists():
            continue
        try:
            if json.loads(path.read_text(encoding="utf-8")).get(key) != current:
                return False
        except (OSError, ValueError):
            return False
        return True
    return False


def links_up_to_date(project: Any, rel: str, *, mode: str | None = None) -> bool:
    marker = project.wiki_dir(rel) / "_planning" / "linker.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if data.get("status") not in ("complete", "disabled"):
        return False
    return mode is None or (data.get("status") == "complete" and data.get("mode") == mode)


def up_to_date(project: Any, rel: str) -> bool:
    return wiki_up_to_date(project, rel) and links_up_to_date(project, rel)


def write_index(project: Any) -> None:
    lines = ["# Wiki", ""]
    for planning in sorted(project.wiki.rglob("_planning/metadata.json")):
        folder = planning.parent.parent
        rel = folder.relative_to(project.wiki).as_posix()
        pages = sorted(page.name for page in folder.glob("*.md"))
        lines.append(f"## {rel}")
        lines.extend(f"- [{page[:-3]}]({rel}/{page})" for page in pages)
        lines.append("")
    project.wiki.mkdir(parents=True, exist_ok=True)
    (project.wiki / "index.md").write_text(
        "\n".join(lines).rstrip() + "\n", encoding="utf-8"
    )
