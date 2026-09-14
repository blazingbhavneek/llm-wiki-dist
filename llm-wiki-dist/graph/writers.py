"""One writer entry point for chunks, pages, and wiki output."""

from __future__ import annotations

import shutil
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

StopCheck = Callable[[], bool] | None
Progress = Callable[[dict[str, Any]], None] | None


def wiki_config(settings: Any, *, run_dir: Path, resume: bool = True, source_kind: str = "md"):
    from .wiki.config import WikiConfig

    return WikiConfig(
        chat_base_url=settings.chat_base_url,
        chat_api_key=settings.chat_api_key,
        chat_model=settings.chat_model,
        temperature=0.0,
        output_language=getattr(settings, "wiki_output_language", "Japanese (日本語)"),
        section_target_lines=int(getattr(settings, "wiki_section_target_lines", 80)),
        write_attempts=int(getattr(settings, "wiki_write_attempts", 3)),
        rewrite_concurrency=int(getattr(settings, "wiki_rewrite_concurrency", 4)),
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
    from .chunk import _run_async_blocking
    from .wiki.pipeline import run_pipeline
    from .wiki.model import ChatModelPort

    config = wiki_config(settings, run_dir=run_dir, resume=resume, source_kind=source_kind)
    model = llm if hasattr(llm, "structured") and hasattr(llm, "text") else ChatModelPort(config, llm=llm) if llm is not None else None
    return _run_async_blocking(
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
) -> SimpleNamespace:
    source_path = Path(source_path)
    out_dir = Path(out_dir)
    from .formats import is_tabular, kind_of

    kind = source_kind or kind_of(document_name)
    if is_tabular(kind):
        from .chunk import _run_async_blocking
        from .formats import csv as csv_format, xlsx as xlsx_format
        from .wiki.model import ChatModelPort

        config = wiki_config(settings, run_dir=state_dir or (out_dir / "wiki-state"), source_kind=kind)
        runner = xlsx_format.run if kind == "xlsx" else csv_format.run
        model = llm if hasattr(llm, "structured") and hasattr(llm, "text") else ChatModelPort(config, llm=llm)
        _run_async_blocking(runner(source_path, run_dir=out_dir, model=model, config=config, on_progress=on_progress, stop_check=stop_check))
        return SimpleNamespace(out_dir=out_dir, file_count=len(list((out_dir / "docs").glob("*.md"))))
    if mode == "wiki":
        from .wiki.export import export_ingest_layout

        run_root = run_wiki(
            source_path,
            run_dir=state_dir or (out_dir / "wiki-state"),
            settings=settings,
            llm=llm,
            on_progress=on_progress,
            stop_check=stop_check,
            source_kind=kind,
        )
        export_ingest_layout(run_root, out_dir, document_name=document_name)
        return SimpleNamespace(
            out_dir=out_dir, file_count=len(list((out_dir / "docs").glob("*.md")))
        )

    body = source_path.read_text(encoding="utf-8")
    if mode == "pages":
        from .pages import run_pages_pipeline

        return run_pages_pipeline(
            source_text=body,
            document_name=document_name,
            out_dir=out_dir,
            llm=llm,
            embedder=embedder,
            settings=settings,
            on_progress=on_progress,
            stop_check=stop_check,
        )

    from .chunk import run_chunk_pipeline

    return run_chunk_pipeline(
        source_text=body,
        document_name=document_name,
        out_dir=out_dir,
        llm=llm,
        concurrency=max(1, int(getattr(settings, "ingest_concurrency", 4))),
        on_progress=on_progress,
        stop_check=stop_check,
    )


def publish_output(staged: Path, target: Path) -> int:
    """Publish ``docs/*.md`` flat beside ``_planning/``."""

    staged = Path(staged)
    target = Path(target)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    count = 0
    for page in sorted((staged / "docs").glob("*.md")):
        shutil.copyfile(page, target / page.name)
        count += 1
    if (staged / "_planning").exists():
        shutil.copytree(staged / "_planning", target / "_planning")
    return count


def write_wiki(
    project: Any,
    rel: str,
    *,
    mode: str,
    settings: Any,
    llm: Any,
    embedder: Any,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
) -> Path:
    from .formats import kind_of

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
        )
        target = project.wiki_dir(rel)
        publish_output(result.out_dir, target)
        if mode == "wiki":
            run_wiki_linker(
                project,
                rel,
                settings=settings,
                llm=llm,
                embedder=embedder,
                on_progress=on_progress,
                stop_check=stop_check,
            )
        write_source_stamp(target, project.raw_file(rel), rel)
        return target
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_wiki_linker(
    project: Any,
    rel: str,
    *,
    settings: Any,
    llm: Any,
    embedder: Any,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
) -> None:
    """Pre-ingestion cross-document linker; stamp only after it succeeds."""

    import os

    from .wiki.linker import link_generated_document
    from .wiki.storage import write_json_atomic

    marker_path = project.wiki_dir(rel) / "_planning" / "linker.json"
    if not getattr(settings, "wiki_linker_enabled", True):
        # disabled mode constructs no services and never opens the catalog
        write_json_atomic(
            marker_path,
            {"schema_version": 1, "status": "disabled", "links_added": 0, "links_removed": 0},
        )
        return

    write_json_atomic(
        marker_path,
        {"schema_version": 1, "status": "pending", "links_added": 0, "links_removed": 0},
    )

    from .chunk import _run_async_blocking
    from .wiki.model import ChatModelPort

    if hasattr(llm, "structured") and hasattr(llm, "text"):
        model = llm  # already a model port; no services to construct
    else:
        config = wiki_config(settings, run_dir=project.state_dir(rel))
        model = ChatModelPort(config, llm=llm)
    if embedder is None:
        try:
            from .gateway import Embedder

            embedder = Embedder(settings)
        except Exception:
            embedder = None
    reranker = None
    try:
        from .gateway import Reranker

        reranker = Reranker(settings)
    except Exception:
        reranker = None
    # Linker concurrency mirrors the wiki maker's rewrite concurrency so one
    # WIKI_REWRITE_CONCURRENCY knob governs both phases; an explicit linker
    # environment variable still wins.
    rewrite = int(getattr(settings, "wiki_rewrite_concurrency", 4) or 4)
    linker_settings = SimpleNamespace(
        wiki_output_language=getattr(
            settings, "wiki_output_language", "Japanese (日本語)"
        ),
        wiki_linker_map_concurrency=int(
            os.environ.get("WIKI_LINKER_MAP_CONCURRENCY", rewrite)
        ),
        wiki_linker_research_concurrency=int(
            os.environ.get("WIKI_LINKER_RESEARCH_CONCURRENCY", rewrite)
        ),
    )
    _run_async_blocking(
        link_generated_document(
            project,
            rel,
            model=model,
            settings=linker_settings,
            embedder=embedder,
            reranker=reranker,
            on_progress=on_progress,
            stop_check=stop_check,
        )
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_source_stamp(target: Path, raw_file: Path, rel: str) -> None:
    planning = Path(target) / "_planning"
    planning.mkdir(exist_ok=True)
    tmp = planning / "source.json.tmp"
    tmp.write_text(json.dumps({"raw": rel, "sha256": _sha256_file(raw_file)}), encoding="utf-8")
    tmp.replace(planning / "source.json")


def up_to_date(project: Any, rel: str) -> bool:
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
        # A present pending/failed linker marker means the cross-document
        # phase never finished; legacy output without any marker stays valid.
        marker = planning / "linker.json"
        if marker.exists():
            try:
                status = json.loads(marker.read_text(encoding="utf-8")).get("status")
            except (OSError, ValueError):
                return False
            return status in ("complete", "disabled")
        return True
    return False


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
