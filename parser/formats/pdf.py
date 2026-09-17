"""PDF parsing through MinerU with parallel image descriptions."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from client.llm import LLMClient
from formats.base import BaseParser, ExtractedDocument, ParseOptions, ParseProfile
from utils.markdown_images import embed_markdown_images
from workers import Workers

logger = logging.getLogger("doc-parser.pdf")

_PDF_HEADER = b"%PDF-"

# Explicit page-boundary markers that a MinerU backend may emit into the
# Markdown when no content-list JSON is available. Match the ``N`` as either
# a plain page number or a page number prefixed by a leading ``page``; the
# marker can appear on any line and its span does not depend on Markdown
# structure so a backend can produce it from any source-of-truth.
_PAGE_MARKER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"<!--\s*page[-_ ]?(?:break|index)?\s*[:=]\s*(\d+)\s*-->", re.IGNORECASE),
    re.compile(r"^<!--\s*page\s+(\d+)\s*-->\s*$", re.IGNORECASE | re.MULTILINE),
)
_PDF_PAGE_HEADING_RE = re.compile(r"^##\s*PDF ページ\s+(\d+)\s*$", re.MULTILINE)

try:
    _LIBC = ctypes.CDLL(None)
except Exception:  # noqa: BLE001 - non-Linux or exotic libc
    _LIBC = None


def _parent_death_signal() -> None:
    """Terminate the MinerU child if its GPU worker is forcefully killed."""
    if _LIBC is not None:
        _LIBC.prctl(1, signal.SIGTERM, 0, 0, 0)


class MineruError(RuntimeError):
    """MinerU did not produce a usable Markdown document."""


def _find_markdown(output_dir: Path, input_stem: str) -> Path:
    candidates = sorted(output_dir.rglob("*.md"))
    if not candidates:
        raise MineruError("MinerU completed without producing a Markdown file")

    preferred = [path for path in candidates if path.stem == input_stem]
    if len(preferred) == 1:
        return preferred[0]
    if len(candidates) == 1:
        return candidates[0]

    relative = ", ".join(str(path.relative_to(output_dir)) for path in candidates[:8])
    raise MineruError(f"MinerU produced multiple Markdown files: {relative}")


def _render_content_list(blocks: list[dict]) -> list[str]:
    """Render each ``page_idx`` group as one raw Markdown string.

    Blocks keep their emitted order; block fields (``text``, ``img_path``,
    ``table_body``, captions) are the same fields already used by MinerU's
    own document Markdown, so a page's raw references match the raw references
    in the full document.
    """
    pages: dict[int, list[str]] = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        raw_index = block.get("page_idx", block.get("page"))
        try:
            page_index = int(raw_index)
        except (TypeError, ValueError):
            continue
        rendered: list[str] = []
        block_type = block.get("type")
        if block_type == "image":
            img_path = block.get("img_path") or ""
            caption_lines = [str(c) for c in (block.get("img_caption") or []) if str(c).strip()]
            alt = " / ".join(caption_lines) or "PDF ページ画像"
            if img_path:
                rendered.append(f"![{alt}]({img_path})")
            if caption_lines:
                rendered.append("\n".join(caption_lines))
            image_footnote = block.get("img_footnote") or []
            if image_footnote:
                rendered.append("\n".join(str(line) for line in image_footnote if str(line).strip()))
        elif block_type == "table":
            body = str(block.get("table_body") or "").strip()
            caption_lines = [str(c) for c in (block.get("table_caption") or []) if str(c).strip()]
            if caption_lines:
                rendered.append("\n".join(caption_lines))
            if body:
                rendered.append(body)
        else:
            text = str(block.get("text") or "").strip()
            level = block.get("text_level") or 0
            if text:
                if isinstance(level, int) and 1 <= level <= 6:
                    rendered.append(f"{'#' * level} {text}")
                else:
                    rendered.append(text)
        if rendered:
            pages.setdefault(page_index, []).append("\n\n".join(rendered))
    return ["\n\n".join(pages[index]) for index in sorted(pages)]


def _split_by_markers(markdown: str) -> list[str]:
    """Split Markdown into pages using an installed backend's explicit markers.

    Supports both HTML comment page boundaries and ``## PDF ページ N`` style
    headings. If no marker appears, returns an empty list so the caller can
    report a clear ``MineruError`` rather than produce false pages.
    """
    for pattern in _PAGE_MARKER_RES:
        splits = pattern.split(markdown)
        # ``re.split`` with a capture emits [pre, num, section, num, ...].
        if len(splits) > 2 and any(int(splits[i]) >= 1 for i in range(1, len(splits), 2)):
            sections: list[str] = []
            for index in range(2, len(splits), 2):
                section = splits[index].strip()
                if section:
                    sections.append(section)
            if sections:
                return sections
    splits = _PDF_PAGE_HEADING_RE.split(markdown)
    if len(splits) > 1 and any(splits[i] for i in range(1, len(splits), 2)):
        sections = []
        for pair_index in range(1, len(splits), 2):
            page_number = int(splits[pair_index])
            body = splits[pair_index + 1] if pair_index + 1 < len(splits) else ""
            section = f"## PDF ページ {page_number}\n\n{body.strip()}".strip()
            if section:
                sections.append(section)
        return sections
    return []


def split_mineru_pages(markdown_path: Path, output_dir: Path) -> list[str]:
    """Return MinerU's per-page Markdown strings for a parsed PDF.

    Prefers the ``content_list.json`` that the ``pipeline``/``vlm``/
    ``hybrid`` backends emit alongside the Markdown. Each block carries a
    ``page_idx`` and either a ``text``/``img_path``/``table_body`` field, so
    grouping by page reproduces the raw relative image references that appear
    in the document Markdown. When no content-list is available, explicit
    page-boundary markers within the Markdown are used instead. Never infers
    boundaries from headings or blank lines alone.
    """
    preferred: list[Path] = []
    fallback: list[Path] = []
    for candidate in output_dir.rglob("*.json"):
        name = candidate.name.casefold()
        if name.endswith("content_list.json") or name == "content_list.json":
            preferred.append(candidate)
        elif "content_list" in name:
            fallback.append(candidate)
    for content_path in [*sorted(preferred), *sorted(fallback)]:
        try:
            payload = json.loads(content_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload = payload.get("pdf_info") or payload.get("content_list") or []
        if not isinstance(payload, list):
            continue
        pages = _render_content_list(payload)
        if pages:
            return pages

    try:
        markdown_text = markdown_path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - markdown path is created earlier
        raise MineruError(f"MinerU Markdown is unreadable at {markdown_path}: {exc}") from exc

    marker_pages = _split_by_markers(markdown_text)
    if marker_pages:
        return marker_pages

    raise MineruError(
        "MinerU produced no per-page artifact: neither a content-list JSON "
        f"(looked under {output_dir} for content_list.json) nor explicit page "
        f"markers were found in {markdown_path.name}. "
        "Enable MinerU's content_list output or add backend page markers."
    )


def discover_mineru_bin(command: list[str]) -> str | None:
    """Return a ``bin`` directory to prepend to PATH so ``command[0]`` resolves.

    Order: explicit MINERU_VENV_BIN, the current PATH, venvs in the project
    root (``.venv``, ``venv``, ...), this interpreter's own bin, then common
    venv/conda roots under the user's home and /opt. Returns None when nothing
    is found, which leaves PATH untouched so the original error message still
    applies.
    """
    explicit = os.getenv("MINERU_VENV_BIN", "").strip()
    if explicit:
        return explicit

    executable = shutil.which(command[0])
    if executable:
        return str(Path(executable).parent)

    exe_name = command[0] + (".exe" if os.name == "nt" else "")
    # This package lives in <project root>/formats, so parent.parent is the root.
    project_root = Path(__file__).resolve().parent.parent
    candidates = [
        *(project_root / name / "bin"
          for name in (".venv", "venv", "env", ".env")),
        Path(sys.executable).parent,
    ]
    home = Path.home()
    candidates.extend(
        bin_dir
        for bin_dir in (
            *sorted(home.glob("*venv*/bin")),
            *sorted(home.glob("*/.venv/bin")),
            *sorted(home.glob(".venvs/*/bin")),
            *sorted(home.glob("*conda*/envs/*/bin")),
            *sorted(Path("/opt").glob("*venv*/bin")),
        )
    )
    for bin_dir in candidates:
        if (bin_dir / exe_name).is_file():
            return str(bin_dir)
    return None


def run_mineru(pdf_path: str, output_dir: str) -> str:
    """Run MinerU in a GPU worker and return its generated Markdown path.

    This function is module-level because spawned process workers require a
    picklable callable. MinerU itself is invoked without a shell.
    """
    source = Path(pdf_path)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)

    command = shlex.split(os.getenv("MINERU_COMMAND", "mineru"))
    if not command:
        raise MineruError("MINERU_COMMAND is empty")

    args = [*command, "-p", str(source), "-o", str(destination)]

    # ``pipeline`` is the general-purpose backend; it cold-loads in ~1 minute and
    # ignores --gpu-memory-utilization. The vlm/hybrid backends default otherwise
    # and pull in vLLM, whose init on a shared GPU takes several minutes.
    backend = os.getenv("MINERU_BACKEND", "pipeline").strip()
    if backend:
        args.extend(["-b", backend])

    # A warm mineru-api service (started by the server lifespan with the VLM
    # preloaded) avoids the per-request vLLM cold start. When it is not up,
    # MINERU_API_URL is absent and the CLI cold-starts locally as before.
    api_url = os.getenv("MINERU_API_URL", "").strip()
    if api_url:
        args.extend(["--api-url", api_url])

    # --gpu-memory-utilization is a vLLM knob: only meaningful for vlm/hybrid.
    gpu_memory = os.getenv("MINERU_GPU_MEMORY_UTILIZATION", "").strip()
    if gpu_memory and backend not in {"", "pipeline"}:
        args.extend(["--gpu-memory-utilization", gpu_memory])

    extra_args = os.getenv("MINERU_EXTRA_ARGS", "").strip()
    if extra_args:
        args.extend(shlex.split(extra_args))

    environment = os.environ.copy()
    environment["CUDA_VISIBLE_DEVICES"] = os.getenv(
        "MINERU_CUDA_VISIBLE_DEVICES",
        "1",
    )
    environment["MINERU_PROCESSING_WINDOW_SIZE"] = os.getenv(
        "MINERU_PROCESSING_WINDOW_SIZE",
        "4",
    )
    environment["MINERU_DISABLE_CUDNN_SDPA"] = os.getenv(
        "MINERU_DISABLE_CUDNN_SDPA",
        "true",
    )
    bootstrap_dir = (
        Path(__file__).resolve().parent.parent / "workers" / "mineru_bootstrap"
    )
    environment["PYTHONPATH"] = (
        f"{bootstrap_dir}{os.pathsep}{environment.get('PYTHONPATH', '')}"
    )
    mineru_bin = discover_mineru_bin(command)
    if mineru_bin:
        environment["PATH"] = (
            f"{mineru_bin}{os.pathsep}{environment.get('PATH', '')}"
        )

    timeout_s = float(os.getenv("MINERU_TIMEOUT_SECONDS", "1800"))
    try:
        # Own session so a kill on timeout takes the whole process group:
        # without an API URL the CLI starts a temporary local mineru-api
        # whose vLLM children would otherwise survive and hold GPU memory.
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=True,
            preexec_fn=_parent_death_signal if _LIBC else None,
        )
    except FileNotFoundError as exc:
        raise MineruError(f"MinerU executable was not found: {command[0]}") from exc

    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(process)
        process.communicate()
        raise MineruError(f"MinerU timed out after {timeout_s:g} seconds") from exc

    if process.returncode:
        details = ((stderr or "") or (stdout or "")).strip()[-4000:]
        raise MineruError(
            f"MinerU exited with status {process.returncode}: {details}"
        )

    return str(_find_markdown(destination, source.stem))


def _kill_process_group(process: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the CLI's whole process group."""
    try:
        group = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


class PdfParser(BaseParser):
    name = "pdf"
    stream_response = True

    @classmethod
    def detect(cls, data: bytes) -> bool:
        return _PDF_HEADER in data[:1024]

    async def _extract(
        self,
        data: bytes,
        image_dir: str,
        options: ParseOptions,
        workers: Workers,
    ) -> ExtractedDocument:
        work_dir = Path(image_dir)
        pdf_path = work_dir / "document.pdf"
        output_dir = work_dir / "mineru-output"
        pdf_path.write_bytes(data)

        markdown_path = Path(
            await workers.run_gpu(
                run_mineru,
                str(pdf_path),
                str(output_dir),
            )
        )
        markdown = markdown_path.read_text(encoding="utf-8")

        if options.profile == ParseProfile.GENERIC:
            # Return raw references plus resolved paths; BaseParser.parse embeds
            # Markdown data URLs into the full document and every page. Page
            # extraction is generic-only: llm-wiki keeps its historical
            # behavior and must not newly depend on a page artifact existing.
            raw_pages = split_mineru_pages(markdown_path, output_dir)
            return ExtractedDocument(
                markdown=markdown,
                pages=raw_pages,
                markdown_path=markdown_path,
                asset_root=output_dir,
            )

        client = (
            LLMClient(
                base_url=options.llm_base_url,
                api_key=options.llm_api_key,
                model=options.llm_model,
            )
            if options.describe_images
            else None
        )
        try:
            # Embed the full document and its page views in one pass. This
            # keeps descriptions identical while scheduling only one LLM call
            # per unique image asset.
            try:
                raw_pages = split_mineru_pages(markdown_path, output_dir)
            except MineruError as exc:
                logger.warning("llm-wiki PDF pages unavailable: %s", exc)
                raw_pages = []
            boundary = "\n<!-- doc-parser-pdf-page-boundary -->\n"
            parts = [markdown, *raw_pages]
            if any(boundary in part for part in parts):
                raise MineruError("reserved PDF page boundary appeared in MinerU output")
            embedded_parts = (
                await embed_markdown_images(
                    boundary.join(parts),
                    markdown_path,
                    output_dir,
                    workers,
                    client.describe_image if client is not None else None,
                )
            ).split(boundary)
            embedded, *embedded_pages = embedded_parts
            return ExtractedDocument(
                markdown=embedded,
                pages=embedded_pages or [embedded],
            )
        finally:
            if client is not None:
                await client.close()
