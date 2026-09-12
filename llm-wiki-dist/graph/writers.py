"""One writer entry point for chunks, pages, and wiki output."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

StopCheck = Callable[[], bool] | None
Progress = Callable[[dict[str, Any]], None] | None


def wiki_config(settings: Any, *, run_dir: Path, resume: bool = True):
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
    )


def run_wiki(
    source_path: Path,
    *,
    run_dir: Path,
    settings: Any,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
    resume: bool = True,
) -> Path:
    from .chunk import _run_async_blocking
    from .wiki.pipeline import run_pipeline

    return _run_async_blocking(
        run_pipeline(
            source_path,
            config=wiki_config(settings, run_dir=run_dir, resume=resume),
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
) -> SimpleNamespace:
    source_path = Path(source_path)
    out_dir = Path(out_dir)
    if mode == "wiki":
        from .wiki.export import export_ingest_layout

        run_root = run_wiki(
            source_path,
            run_dir=state_dir or (out_dir / "wiki-state"),
            settings=settings,
            on_progress=on_progress,
            stop_check=stop_check,
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
        )
        target = project.wiki_dir(rel)
        publish_output(result.out_dir, target)
        return target
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
