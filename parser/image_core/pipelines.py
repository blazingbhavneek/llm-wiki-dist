#!/usr/bin/env python3

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from image_core.llm_judges import (
    DESCRIPTION_COVERAGE_ATTEMPTS,
    DESCRIPTION_COVERAGE_GOOD_ENOUGH_SCORE,
    ENABLE_DESCRIPTION_COVERAGE_LOOP,
    ENABLE_SHALLOW_RETRY,
    INCLUDE_COVERAGE_JUDGE_NOTE,
    LLM_FALLBACK_TIMEOUT_SECONDS,
    LLM_THINKING_TIMEOUT_SECONDS,
    OPENAI_MODEL,
    TEMPERATURE,
    ensure_reconstruction_wrapper,
    improve_description_coverage_loop,
    improve_mermaid_visual_match_loop,
    llm_ainvoke_text,
    looks_too_shallow,
    make_chat_client,
    repair_mermaid_if_needed,
)
from image_core.mermaid_media import (
    ENABLE_MERMAID_DIAGRAMS,
    ENABLE_MERMAID_VISUAL_MATCH_LOOP,
    MERMAID_CLI_BIN,
    MERMAID_CLI_REQUIRED,
    MERMAID_PUPPETEER_CONFIG_FILE,
    MERMAID_REPAIR_ATTEMPTS,
    MERMAID_VISUAL_MATCH_ATTEMPTS,
    MERMAID_VISUAL_MATCH_GOOD_ENOUGH_SCORE,
    VALIDATE_MERMAID,
    build_original_image_blocks_for_compare,
    extract_images_from_line,
    find_image_line_indices,
    image_file_to_data_url,
    is_remote_url,
)

load_dotenv()

MARKDOWN_FOLDER = os.environ.get("MARKDOWN_FOLDER", "./mineru")

# Number of Markdown files to process at the same time.
FILE_CONCURRENCY = 5

OUTPUT_FILE = None
# If OUTPUT_FILE is None, output will be:
# input_filename.described.md

CONCURRENCY = 1


def build_reconstruction_prompt() -> str:
    if ENABLE_MERMAID_DIAGRAMS:
        diagram_guidance = (
            "For flowcharts, architecture diagrams, block diagrams, dependency graphs, sequence diagrams, or data-flow diagrams:\n"
            "- PREFER Mermaid whenever the image visibly contains a node/edge/process/flow structure. "
            "Mermaid expresses flow and relationships better than prose.\n"
            "- Only output Mermaid when the image visibly contains a node/edge/process/flow diagram.\n"
            "- Do not output Mermaid for photos, screenshots, plain text, equations, tables, normal charts, icons, or ambiguous figures.\n"
            "- If Mermaid would require inventing nodes or arrows, use structured Markdown instead.\n"
            "- Use flowchart TD, flowchart LR, graph TD, graph LR, sequenceDiagram, or another appropriate Mermaid syntax.\n"
            "- Include every visible node, box, component, actor, storage element, process, file, subsystem, and external system.\n"
            "- Include every visible arrow, line, connection, edge, and data flow.\n"
            "- Preserve arrow direction when visible.\n"
            "- Preserve labels on arrows if visible.\n"
            "- Preserve grouping boundaries, containers, subsystems, layers, computers, networks, and external actors.\n"
            "- After the Mermaid block, include detailed reconstruction notes describing layout, direction, grouping, "
            "line styles, missing details, and visual features Mermaid cannot express.\n\n"
            "Mermaid syntax requirements:\n"
            "- Mermaid diagrams must be parseable by mermaid-cli.\n"
            "- Use simple ASCII node IDs like n1, n2, server1, process_a.\n"
            "- Put Japanese labels, spaces, parentheses, punctuation, and long text inside quoted labels.\n"
            '- Good: n1["エラー処理機能"] --> n2["ログファイル"]\n'
            "- Bad: エラー処理機能 --> ログファイル\n"
            "- Do not put Markdown bullets, notes, prose, or table syntax inside Mermaid code blocks.\n"
            "- Mermaid code blocks must contain only Mermaid syntax.\n"
            "- If a detailed visual feature is hard to express in Mermaid, keep the Mermaid simple and explain the detail outside the code block.\n\n"
        )
        reconstructed_content = (
            "<Markdown table, Mermaid diagram, transcribed text, or detailed structured representation>"
        )
        mermaid_format_hint = (
            "If using Mermaid, include it as a fenced mermaid code block exactly like:\n"
            "```mermaid\n"
            "flowchart TD\n"
            '    A["Example"] --> B["Example"]\n'
            "```\n\n"
        )
    else:
        diagram_guidance = (
            "For flowcharts, architecture diagrams, block diagrams, dependency graphs, sequence diagrams, or data-flow diagrams:\n"
            "- Do not output Mermaid code blocks.\n"
            "- Reconstruct the diagram as structured Markdown text instead.\n"
            "- Include every visible node, box, component, actor, storage element, process, file, subsystem, and external system.\n"
            "- Include every visible arrow, line, connection, edge, and data flow.\n"
            "- Preserve arrow direction, labels, grouping boundaries, containers, layers, and layout notes when visible.\n\n"
        )
        reconstructed_content = (
            "<Markdown table, structured node/edge list, transcribed text, or detailed representation>"
        )
        mermaid_format_hint = ""

    return (
        "You are transcribing an image into text. The text will replace the image in a technical document.\n\n"
        "ABSOLUTE RULES - read these first:\n"
        "1. Describe ONLY what is actually visible inside the attached image.\n"
        "2. You have NO document context. None is given, and you must not assume, infer, or invent any.\n"
        "3. Transcribe EXACTLY. Every word, every line, every label, every number, every symbol that is "
        "visible in the image must appear in your output, character for character.\n"
        "4. Do NOT summarize. Do NOT paraphrase. Do NOT write a caption. Do NOT explain what the figure "
        "'is about'. Do NOT add background knowledge.\n"
        "5. Do NOT invent any text, node, arrow, row, column, or value that you cannot actually see. "
        "Inventing content is worse than omitting it.\n"
        "6. If text is too small, cut off, or unreadable, write '[unclear]'. Never guess it.\n"
        "7. Transcribe text in its original language. If the image contains Japanese, output that Japanese "
        "verbatim. Do not translate it away; you may add an English gloss afterwards in parentheses.\n"
        "8. Copy numbers exactly as printed. Do not round, reformat, or convert units.\n\n"
        "Your output will be audited against the image by a strict judge. The judge lists every visible "
        "string you omitted and every string you invented. Aim for complete coverage with zero invention.\n\n"
        "Output requirements:\n"
        "- Be exhaustive and concrete.\n"
        "- Transcribe all visible text, labels, titles, captions, legends, numbers, arrows, boxes, nodes, columns, rows, "
        "axes, units, UI labels, Japanese text, English text, and relationships.\n"
        "- Prefer structured Markdown that can stand in for the image.\n"
        "- The output may be multiline.\n"
        "- Return only the Markdown replacement for the image.\n"
        "- Do not include any preface like 'Here is the reconstruction'.\n\n"
        + diagram_guidance
        + "For tables:\n"
        "- Recreate the table as a Markdown table.\n"
        "- Preserve all headers, row labels, column labels, values, merged-cell meaning, units, footnotes, and notes.\n"
        "- If the table has merged cells, explain the merge/grouping after the table.\n"
        "- If there are multi-level headers, represent them as clearly as possible in Markdown and explain the hierarchy.\n\n"
        "For charts/graphs:\n"
        "- Identify chart type.\n"
        "- Recreate visible data as a Markdown table when values are visible or reasonably readable.\n"
        "- Transcribe x-axis, y-axis, tick values, units, scale, legend entries, and series names exactly as printed.\n"
        "- State trends only as far as they are visible in the plotted data. Do not speculate about causes.\n\n"
        "For screenshots:\n"
        "- Recreate the UI state in text.\n"
        "- Transcribe window titles, menus, dialogs, buttons, labels, fields, selected values, error messages, tables, "
        "visible paths, code, and logs exactly, including punctuation.\n"
        "- State what is selected, enabled, disabled, highlighted, or emphasized.\n\n"
        "For simple node/link diagrams:\n"
        "- Encode the structure explicitly, for example A --> B.\n"
        "- State the exact visible nodes and exact visible edges.\n"
        "- Describe the edge only as the visible structure (a direct link/edge). Do not assign it a semantic "
        "meaning that is not printed in the image.\n\n"
        "For photos or images with no text:\n"
        "- State what is visibly depicted, concretely and without interpretation.\n\n"
        "Required output structure:\n\n"
        "[Image reconstruction:\n"
        "Type: <table / flowchart / block diagram / chart / screenshot / photo / other>\n"
        "Title/caption: <title text printed inside the image, or 'none visible'>\n"
        "Reconstructed content:\n"
        f"{reconstructed_content}\n"
        "Detailed notes: <layout, relationships, visual encoding, and any text marked [unclear]>\n"
        "]\n\n"
        + mermaid_format_hint
        + "Make the output complete enough that another person could redraw the image and recover every "
        "word of its text from your output alone.\n"
    )


# =========================
# IMAGE LINE PROCESSING
# =========================


async def describe_image_line(
    client: ChatOpenAI,
    markdown_file: Path,
    lines,
    index: int,
    semaphore: asyncio.Semaphore,
):
    """
    Describes/reconstructs all images found on a single Markdown line.

    Returns:
      tuple[int, str]
      The line index and the replacement Markdown block.
    """
    async with semaphore:
        original_line = lines[index].rstrip("\n")
        images = extract_images_from_line(original_line, markdown_file)

        if not images:
            return index, lines[index]

        resolvable_images = []
        for img in images:
            if is_remote_url(img["resolved"]) or os.path.exists(img["resolved"]):
                resolvable_images.append(img)
            else:
                print(
                    f"[WARN] Skipping missing image for line {index + 1}: "
                    f"{img['resolved']}"
                )

        if not resolvable_images:
            return index, lines[index]

        original_image_blocks = build_original_image_blocks_for_compare(
            resolvable_images
        )

        content = []
        
        for image_number, img in enumerate(resolvable_images, start=1):
            if is_remote_url(img["resolved"]):
                image_url = img["resolved"]
            else:
                if not os.path.exists(img["resolved"]):
                    print(f"[WARN] Image file missing during encoding: {img['resolved']}")
                    continue
                
                image_url = image_file_to_data_url(img["resolved"])
                
                # Sanity check: ensure the data URL is not empty or malformed
                if len(image_url) < 50:
                    print(f"[WARN] Image encoding resulted in suspiciously short string for: {img['resolved']}")

            # 1. Add the image block FIRST (Crucial for Gemma/Qwen/LLaVA vision models)
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_url,
                        "detail": "high",
                    },
                }
            )

            # 2. Add the metadata text
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"\nImage {image_number} metadata:\n"
                        f"- Original Markdown: {img['original_markdown']}\n"
                        f"- Alt text: {img['alt']}\n"
                        f"- Original target: {img['original_target']}\n"
                        f"- Resolved path or URL: {img['resolved']}\n"
                    ),
                }
            )

        # 3. Append the main prompt AFTER the images
        content.append(
            {
                "type": "text",
                "text": build_reconstruction_prompt(),
            }
        )

        print(f"Processing image line {index + 1}: {original_line}")

        description = ""

        try:
            try:
                description = await asyncio.wait_for(
                    llm_ainvoke_text(
                        client,
                        [
                            {
                                "role": "user",
                                "content": content,
                            }
                        ],
                        temperature=TEMPERATURE,
                    ),
                    timeout=LLM_THINKING_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                print(
                    f"Image line {index + 1} timed out after "
                    f"{LLM_THINKING_TIMEOUT_SECONDS}s. "
                    "Retrying with thinking disabled..."
                )

                description = await asyncio.wait_for(
                    llm_ainvoke_text(
                        client,
                        [
                            {
                                "role": "user",
                                "content": content,
                            }
                        ],
                        temperature=TEMPERATURE,
                        enable_thinking=False,
                    ),
                    timeout=LLM_FALLBACK_TIMEOUT_SECONDS,
                )

        except Exception as e:
            print(
                f"[WARN] Image line {index + 1} failed after timeout fallback. "
                f"Using empty description. Error: {e}"
            )
            description = ""

        print("Initial description:")
        print(description)
        print("")

        if description and ENABLE_SHALLOW_RETRY and looks_too_shallow(description):
            print(f"Description for line {index + 1} looks too shallow. Retrying...")
            print("")

            if ENABLE_MERMAID_DIAGRAMS:
                diagram_retry_instruction = (
                    "If it is a table, output a Markdown table. If it is a flowchart, block diagram, architecture diagram, "
                    "or data-flow diagram, output a Mermaid diagram plus detailed notes. "
                )
            else:
                diagram_retry_instruction = (
                    "If it is a table, output a Markdown table. If it is a flowchart, block diagram, architecture diagram, "
                    "or data-flow diagram, output a structured node/edge list plus detailed notes. "
                )

            retry_messages = [
                {
                    "role": "user",
                    "content": content,
                },
                {
                    "role": "assistant",
                    "content": description,
                },
                {
                    "role": "user",
                    "content": (
                        "The previous answer is too shallow or caption-like. Rewrite it as a reconstruction-quality "
                        "Markdown replacement. The reader should be able to approximately redraw the original image from your output. "
                        + diagram_retry_instruction
                        + "Preserve all visible labels, arrows, "
                        "nodes, directions, groups, titles, captions, values, Japanese text, English text, and relationships. "
                        "Do not summarize. Do not merely say what the image is about. Return only the replacement Markdown. "
                        "Multiline output is allowed and preferred."
                    ),
                },
            ]

            try:
                try:
                    description = await asyncio.wait_for(
                        llm_ainvoke_text(
                            client,
                            retry_messages,
                            temperature=TEMPERATURE,
                        ),
                        timeout=LLM_THINKING_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    print(
                        f"Retry for image line {index + 1} timed out after "
                        f"{LLM_THINKING_TIMEOUT_SECONDS}s. "
                        "Retrying with thinking disabled..."
                    )

                    description = await asyncio.wait_for(
                        llm_ainvoke_text(
                            client,
                            retry_messages,
                            temperature=TEMPERATURE,
                            enable_thinking=False,
                        ),
                        timeout=LLM_FALLBACK_TIMEOUT_SECONDS,
                    )

            except Exception as e:
                print(
                    f"[WARN] Shallow retry for image line {index + 1} failed after timeout fallback. "
                    f"Using empty description. Error: {e}"
                )
                description = ""

            print("Retry description:")
            print(description)
            print("")

        if description:
            description = ensure_reconstruction_wrapper(description)

            description = await repair_mermaid_if_needed(
                client=client,
                original_content=content,
                description=description,
            )

            description = ensure_reconstruction_wrapper(description)

            description = await improve_mermaid_visual_match_loop(
                client=client,
                original_image_blocks=original_image_blocks,
                original_content=content,
                description=description,
            )

            description = ensure_reconstruction_wrapper(description)

            # Audit the finished reconstruction against the image and close any
            # coverage gaps. Runs last so it also audits Mermaid-derived rewrites.
            description, coverage_judge, coverage_score = (
                await improve_description_coverage_loop(
                    client=client,
                    original_image_blocks=original_image_blocks,
                    description=description,
                )
            )

            # Coverage rewrites can touch the Mermaid block, so re-validate syntax.
            description = await repair_mermaid_if_needed(
                client=client,
                original_content=content,
                description=description,
            )

            description = ensure_reconstruction_wrapper(description)

            if INCLUDE_COVERAGE_JUDGE_NOTE and coverage_judge:
                description = (
                    description.rstrip()
                    + "\n\n"
                    + "[Image description coverage judge:\n"
                    + f"Score: {coverage_score}/100\n"
                    + f"Reason: {coverage_judge.get('reason', '')}\n"
                    + "]"
                )

        image_tags = []

        for img in resolvable_images:
            if is_remote_url(img["resolved"]):
                src = img["resolved"]
            else:
                src = image_file_to_data_url(img["resolved"])

            alt_escaped = img["alt"].replace('"', "&quot;")
            image_tags.append(f'<img src="{src}" alt="{alt_escaped}">')

        if not image_tags and not description:
            return index, lines[index]

        media_html = "\n    ".join(image_tags)
        safe_description = description.replace("\n\n", "\n") if description else ""

        block = (
            f"<image-unit>\n"
            f"  <image-media>\n"
            f"    {media_html}\n"
            f"  </image-media>\n"
            f"  <image-description>\n"
            f"{safe_description}\n"
            f"  </image-description>\n"
            f"</image-unit>"
        )

        return index, block + "\n"


# =========================
# MAIN PROCESS
# =========================


def get_described_output_path(input_path: Path) -> Path:
    """
    For:
      sample.md

    Returns:
      sample.described.md
    """
    return input_path.with_name(input_path.stem + ".described" + input_path.suffix)


def should_skip_markdown_file(input_path: Path) -> tuple[bool, str]:
    """
    Returns:
      tuple[bool, str]
        skip, reason
    """
    if input_path.name.endswith(".described.md"):
        return True, "already a described output file"

    output_path = get_described_output_path(input_path)

    if output_path.exists():
        return True, f"described output already exists: {output_path}"

    return False, ""


def find_markdown_files_to_process(
    folder_path: Path,
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """
    Finds all Markdown files recursively, excluding files that should be skipped.

    Returns:
      tuple:
        files_to_process:
          list[Path]
        skipped_files:
          list[tuple[Path, reason]]
    """
    all_markdown_files = sorted(folder_path.rglob("*.md"))

    files_to_process = []
    skipped_files = []

    for md_file in all_markdown_files:
        skip, reason = should_skip_markdown_file(md_file)

        if skip:
            skipped_files.append((md_file, reason))
        else:
            files_to_process.append(md_file)

    return files_to_process, skipped_files


async def process_one_markdown_file(
    client: ChatOpenAI,
    input_path: Path,
    image_semaphore: asyncio.Semaphore,
):
    """
    Processes one Markdown file and writes:

      original.md -> original.described.md

    Returns:
      dict with processing result.
    """
    input_path = input_path.resolve()
    output_path = get_described_output_path(input_path)

    try:
        if not input_path.is_absolute():
            raise ValueError("Markdown input path must be absolute.")

        if not input_path.exists():
            raise FileNotFoundError(f"Markdown file not found: {input_path}")

        # Race-condition safety:
        # Another concurrent process may have created this while we were waiting.
        if output_path.exists():
            return {
                "status": "skipped",
                "input": str(input_path),
                "output": str(output_path),
                "reason": "described output already exists",
                "replaced_image_lines": 0,
            }

        lines = input_path.read_text(encoding="utf-8").splitlines(keepends=True)

        image_line_indices = find_image_line_indices(lines)

        print("")
        print("=" * 80)
        print(f"Processing file: {input_path}")
        print(f"Output file: {output_path}")
        print(f"Found {len(image_line_indices)} image line(s).")
        print("=" * 80)

        if not image_line_indices:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("".join(lines), encoding="utf-8")

            print(f"No image lines found. Copied file to: {output_path}")

            return {
                "status": "done",
                "input": str(input_path),
                "output": str(output_path),
                "reason": "no image lines found; copied original",
                "replaced_image_lines": 0,
            }

        tasks = [
            describe_image_line(
                client=client,
                markdown_file=input_path,
                lines=lines,
                index=index,
                semaphore=image_semaphore,
            )
            for index in image_line_indices
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        new_lines = list(lines)
        replaced = 0

        for result in results:
            # One image raising should not discard every other image's work for
            # this file; keep the original line for the failed one and continue.
            if isinstance(result, BaseException):
                print(f"[WARN] Image task failed for {input_path}: {result}")
                continue

            index, replacement_line = result
            new_lines[index] = replacement_line
            replaced += 1

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("".join(new_lines), encoding="utf-8")

        print(f"Done: {input_path}")
        print(f"Output: {output_path}")
        print(f"Replaced image lines: {replaced}/{len(results)}")

        return {
            "status": "done",
            "input": str(input_path),
            "output": str(output_path),
            "reason": "",
            "replaced_image_lines": replaced,
        }

    except Exception as exc:
        print("")
        print("ERROR while processing Markdown file:")
        print(f"Input: {input_path}")
        print(f"Error: {exc}")

        return {
            "status": "error",
            "input": str(input_path),
            "output": str(output_path),
            "reason": str(exc),
            "replaced_image_lines": 0,
        }


async def process_one_markdown_file_with_file_semaphore(
    client: ChatOpenAI,
    input_path: Path,
    image_semaphore: asyncio.Semaphore,
    file_semaphore: asyncio.Semaphore,
):
    """
    Wrapper that limits how many Markdown files are processed concurrently.
    """
    async with file_semaphore:
        return await process_one_markdown_file(
            client=client,
            input_path=input_path,
            image_semaphore=image_semaphore,
        )


async def process_markdown_folder():
    folder_path = Path(MARKDOWN_FOLDER).resolve()

    if not folder_path.is_absolute():
        raise ValueError("MARKDOWN_FOLDER must be an absolute path.")

    if not folder_path.exists():
        raise FileNotFoundError(f"Markdown folder not found: {folder_path}")

    if not folder_path.is_dir():
        raise NotADirectoryError(f"MARKDOWN_FOLDER is not a directory: {folder_path}")

    files_to_process, skipped_files = find_markdown_files_to_process(folder_path)

    print(f"Input folder: {folder_path}")
    print(f"Found Markdown file(s), excluding skipped: {len(files_to_process)}")
    print(f"Skipped Markdown file(s): {len(skipped_files)}")
    print(f"File concurrency: {FILE_CONCURRENCY}")
    print(f"Image/API concurrency: {CONCURRENCY}")
    print(f"Validate Mermaid: {VALIDATE_MERMAID}")
    print(f"Mermaid CLI: {MERMAID_CLI_BIN}")
    print(f"Mermaid repair attempts: {MERMAID_REPAIR_ATTEMPTS}")
    print(f"Mermaid visual match loop: {ENABLE_MERMAID_VISUAL_MATCH_LOOP}")
    print(f"Mermaid visual match attempts: {MERMAID_VISUAL_MATCH_ATTEMPTS}")
    print(f"Mermaid visual good-enough score: {MERMAID_VISUAL_MATCH_GOOD_ENOUGH_SCORE}")
    print(f"Description coverage loop: {ENABLE_DESCRIPTION_COVERAGE_LOOP}")
    print(f"Description coverage attempts: {DESCRIPTION_COVERAGE_ATTEMPTS}")
    print(f"Description coverage good-enough score: {DESCRIPTION_COVERAGE_GOOD_ENOUGH_SCORE}")
    print("")

    if skipped_files:
        print("Skipped files:")
        for skipped_path, reason in skipped_files:
            print(f"  - {skipped_path}")
            print(f"    Reason: {reason}")
        print("")

    if not files_to_process:
        print("No Markdown files to process.")
        return

    if VALIDATE_MERMAID or ENABLE_MERMAID_VISUAL_MATCH_LOOP:
        mmdc_path = shutil.which(MERMAID_CLI_BIN)

        if mmdc_path is None:
            msg = (
                f"Mermaid validation/rendering requested, but '{MERMAID_CLI_BIN}' was not found in PATH. "
                "Install it with: npm install -g @mermaid-js/mermaid-cli"
            )

            if MERMAID_CLI_REQUIRED:
                raise RuntimeError(msg)

            print(f"WARNING: {msg}")
        else:
            print(f"Found Mermaid CLI: {mmdc_path}")

            try:
                puppeteer_config_file = Path(MERMAID_PUPPETEER_CONFIG_FILE).expanduser()
                print(f"Mermaid Puppeteer config: {puppeteer_config_file}")

                if not puppeteer_config_file.exists():
                    msg = (
                        "WARNING: Mermaid Puppeteer config file does not exist:\n"
                        f"{puppeteer_config_file}"
                    )

                    if MERMAID_CLI_REQUIRED:
                        raise RuntimeError(msg)

                    print(msg)

            except NameError:
                print(
                    "WARNING: MERMAID_PUPPETEER_CONFIG_FILE is not defined. "
                    "If your mmdc requires the Chrome Headless Shell config, define it."
                )

            print("")

    client = make_chat_client()

    file_semaphore = asyncio.Semaphore(FILE_CONCURRENCY)

    # This is shared across all files, so total concurrent image/API calls
    # stays at CONCURRENCY, not FILE_CONCURRENCY * CONCURRENCY.
    image_semaphore = asyncio.Semaphore(CONCURRENCY)

    tasks = [
        process_one_markdown_file_with_file_semaphore(
            client=client,
            input_path=input_path,
            image_semaphore=image_semaphore,
            file_semaphore=file_semaphore,
        )
        for input_path in files_to_process
    ]

    results = await asyncio.gather(*tasks)

    done_results = [r for r in results if r["status"] == "done"]
    skipped_results = [r for r in results if r["status"] == "skipped"]
    error_results = [r for r in results if r["status"] == "error"]

    total_replaced = sum(r["replaced_image_lines"] for r in done_results)

    print("")
    print("=" * 80)
    print("Batch processing complete.")
    print("=" * 80)
    print(f"Input folder: {folder_path}")
    print(f"Markdown files selected: {len(files_to_process)}")
    print(f"Done: {len(done_results)}")
    print(f"Skipped during processing: {len(skipped_results)}")
    print(f"Errors: {len(error_results)}")
    print(f"Total replaced image lines: {total_replaced}")

    if error_results:
        print("")
        print("Errored files:")
        for result in error_results:
            print(f"  - {result['input']}")
            print(f"    Error: {result['reason']}")


# =========================
# SQLITE <image-unit> PROCESSING
#
# The folder pipeline above is still used by parser/server.py during PDF parsing;
# it embeds images as <image-unit> blocks with EMPTY descriptions. This section is
# the CLI: it points at a wiki SQLite database, finds those blocks in nodes.body,
# and fills in / audits the descriptions using the same judge loops.
# =========================


IMAGE_UNIT_RE = re.compile(
    r"<image-unit\b[^>]*>(?P<inner>.*?)</image-unit>",
    re.IGNORECASE | re.DOTALL,
)

IMAGE_MEDIA_RE = re.compile(
    r"<image-media\b[^>]*>(?P<media>.*?)</image-media>",
    re.IGNORECASE | re.DOTALL,
)

IMAGE_DESCRIPTION_RE = re.compile(
    r"<image-description\b[^>]*>(?P<description>.*?)</image-description>",
    re.IGNORECASE | re.DOTALL,
)

IMG_SRC_RE = re.compile(r"<img\b[^>]*\bsrc=[\"'](?P<src>[^\"']+)[\"']", re.IGNORECASE)

# Judge notes are appended to the description on every run. Strip old ones before
# re-judging so repeated runs do not stack them up.
JUDGE_NOTE_RE = re.compile(
    r"\n*\[(?:Image description coverage judge|Mermaid visual match judge|"
    r"Mermaid validation warning):.*?\n\]",
    re.DOTALL,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_judge_notes(description: str) -> str:
    return JUDGE_NOTE_RE.sub("", description or "").strip()


def extract_image_units(body: str):
    """
    Finds every <image-unit> block in one node body.

    Returns:
      list[dict] with:
        - span: (start, end) character offsets in body
        - media_html: raw inner HTML of <image-media>
        - sources: list of <img src="..."> values (base64 data URLs or remote URLs)
        - description: current <image-description> text, judge notes stripped
    """
    units = []

    for match in IMAGE_UNIT_RE.finditer(body or ""):
        inner = match.group("inner")

        media_match = IMAGE_MEDIA_RE.search(inner)
        description_match = IMAGE_DESCRIPTION_RE.search(inner)

        media_html = media_match.group("media").strip() if media_match else ""
        sources = IMG_SRC_RE.findall(media_html) if media_html else []

        raw_description = (
            description_match.group("description") if description_match else ""
        )

        units.append(
            {
                "span": match.span(),
                "media_html": media_html,
                "sources": sources,
                "description": strip_judge_notes(raw_description),
            }
        )

    return units


def render_image_unit(media_html: str, description: str) -> str:
    """
    Rebuilds an <image-unit> block, preserving the media payload untouched.
    Matches the block shape produced by describe_image_line().
    """
    safe_description = description.replace("\n\n", "\n") if description else ""

    return (
        f"<image-unit>\n"
        f"  <image-media>\n"
        f"    {media_html.strip()}\n"
        f"  </image-media>\n"
        f"  <image-description>\n"
        f"{safe_description}\n"
        f"  </image-description>\n"
        f"</image-unit>"
    )


def build_image_blocks_from_sources(sources):
    """
    Builds multimodal content blocks straight from the data URLs stored in the DB.
    """
    blocks = []

    if not sources:
        print("[CRITICAL] build_image_blocks_from_sources called with EMPTY sources list!")
        return blocks

    for image_number, src in enumerate(sources, start=1):
        # CRITICAL: Convert local paths to data URLs if needed
        if not is_remote_url(src) and not src.startswith("data:"):
            if os.path.exists(src):
                src = image_file_to_data_url(src)
                print(f"[INFO] Converted local path to data URL for Image {image_number}")
            else:
                print(f"[WARN] Image source is a local path but file not found: {src}")
                continue

        blocks.append({"type": "text", "text": f"Image {image_number}:"})
        blocks.append({
            "type": "image_url",
            "image_url": {"url": src, "detail": "high"},
        })

    print(f"[INFO] Successfully built {len(blocks)//2} image block(s) for LLM payload.")
    return blocks


async def request_initial_description(client: ChatOpenAI, content, label: str) -> str:
    """
    First-pass description, with the same thinking-timeout fallback as the folder path.
    """
    try:
        try:
            response_text = await asyncio.wait_for(
                llm_ainvoke_text(
                    client,
                    [{"role": "user", "content": content}],
                    temperature=TEMPERATURE,
                ),
                timeout=LLM_THINKING_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            print(
                f"{label} timed out after {LLM_THINKING_TIMEOUT_SECONDS}s. "
                "Retrying with thinking disabled..."
            )

            response_text = await asyncio.wait_for(
                llm_ainvoke_text(
                    client,
                    [{"role": "user", "content": content}],
                    temperature=TEMPERATURE,
                    enable_thinking=False,
                ),
                timeout=LLM_FALLBACK_TIMEOUT_SECONDS,
            )

        return response_text

    except Exception as exc:
        print(f"[WARN] {label} failed after timeout fallback. Error: {exc}")
        return ""


async def describe_image_unit(
    client: ChatOpenAI,
    sources,
    existing_description: str,
    semaphore: asyncio.Semaphore,
    label: str,
):
    async with semaphore:
        # DEBUG: Prove what we are sending
        if not sources:
            print(f"[CRITICAL] {label}: sources list is EMPTY. Skipping.")
            return ""
        
        print(f"[DEBUG] {label} first source preview: {str(sources[0])[:80]}...")

        image_blocks = build_image_blocks_from_sources(sources)
        
        if not image_blocks:
            print(f"[CRITICAL] {label}: image_blocks is empty after processing. Sources were: {sources}")
            return ""

        # CRITICAL FIX: Put images FIRST in the content array. 
        # Vision models (Gemma/Qwen) will ignore images if they come after a huge text prompt.
        content = list(image_blocks)
        content.append({"type": "text", "text": build_reconstruction_prompt()})

        description = (existing_description or "").strip()

        if description:
            print(f"{label}: existing description found; starting at judge loop.")
            description = ensure_reconstruction_wrapper(description)
        else:
            print(f"{label}: no description; generating.")
            description = await request_initial_description(client, content, label)

            if not description:
                return ""

            description = ensure_reconstruction_wrapper(description)

        description = await repair_mermaid_if_needed(
            client=client,
            original_content=content,
            description=description,
        )

        description = ensure_reconstruction_wrapper(description)

        description = await improve_mermaid_visual_match_loop(
            client=client,
            original_image_blocks=image_blocks,
            original_content=content,
            description=description,
        )

        description = ensure_reconstruction_wrapper(description)

        description, coverage_judge, coverage_score = (
            await improve_description_coverage_loop(
                client=client,
                original_image_blocks=image_blocks,
                description=description,
            )
        )

        # Coverage rewrites can touch the Mermaid block, so re-validate syntax.
        description = await repair_mermaid_if_needed(
            client=client,
            original_content=content,
            description=description,
        )

        description = ensure_reconstruction_wrapper(description)

        if INCLUDE_COVERAGE_JUDGE_NOTE and coverage_judge:
            description = (
                description.rstrip()
                + "\n\n"
                + "[Image description coverage judge:\n"
                + f"Score: {coverage_score}/100\n"
                + f"Reason: {coverage_judge.get('reason', '')}\n"
                + "]"
            )

        return description


def make_working_copy(source_path: Path, dest_path: Path) -> None:
    """
    Consistent whole-database copy via SQLite's online backup API.

    A plain file copy can capture a torn state because committed data may still
    live in the -wal side file. backup() copies committed pages properly.
    The source database is opened read-only and is never modified.
    """
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(dest_path) + suffix)
        if stale.exists():
            stale.unlink()

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)

    try:
        dest = sqlite3.connect(str(dest_path))
        try:
            with dest:
                source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def reindex_node_fts(conn: sqlite3.Connection, node_id: str) -> None:
    """
    Rebuilds the nodes_fts row for one node, mirroring GraphStore._reindex_fts.

    Neither librarian bootstrap path rebuilds nodes_fts, so a changed body would
    otherwise stay searchable only under its OLD description text.
    """
    row = conn.execute(
        "SELECT title, summary, body, keywords_json, status FROM nodes WHERE id=?",
        (node_id,),
    ).fetchone()

    if row is None:
        return

    conn.execute("DELETE FROM nodes_fts WHERE node_id=?", (node_id,))

    if row["status"] == "deleted":
        return

    try:
        keywords = json.loads(row["keywords_json"] or "[]")
    except Exception:
        keywords = []

    text = " ".join(
        part
        for part in [
            row["title"] or "",
            row["summary"] or "",
            row["body"] or "",
            " ".join(k for k in keywords if isinstance(k, str)),
        ]
        if part
    )

    conn.execute(
        "INSERT INTO nodes_fts(node_id, text) VALUES(?, ?)",
        (node_id, text),
    )


def invalidate_search_index(conn: sqlite3.Connection, node_ids) -> None:
    """
    Marks the rewritten nodes so the librarian rebuilds search state on next start.

    - nodes_fts: rebuilt here directly (no bootstrap path covers it).
    - vec_body:  rows dropped, so _bootstrap_vectors sees coverage_incomplete and re-embeds.
    - meta.search_index_version: cleared, so _bootstrap_search_items rebuilds
      search_items + vec_search_item.

    No embedder is imported; this is pure SQL on the working copy.
    """
    if not node_ids:
        return

    for node_id in node_ids:
        try:
            reindex_node_fts(conn, node_id)
        except sqlite3.OperationalError as exc:
            print(f"[WARN] nodes_fts reindex skipped for {node_id}: {exc}")

        try:
            conn.execute("DELETE FROM vec_body WHERE node_id=?", (node_id,))
        except sqlite3.OperationalError:
            # Vector tables may not exist yet; bootstrap will build them.
            pass

    try:
        conn.execute("DELETE FROM meta WHERE key='search_index_version'")
    except sqlite3.OperationalError as exc:
        print(f"[WARN] could not clear search_index_version: {exc}")

    print(
        f"Search index invalidated for {len(node_ids)} node(s): "
        "nodes_fts rebuilt, vec_body cleared, search_index_version reset."
    )


async def process_sqlite_database(database_path: str, output_path: str | None = None):
    """
    Fills in / audits <image-unit> descriptions inside a wiki SQLite database.

    The source database is never modified. All work lands in a working copy.
    """
    source_path = Path(database_path).resolve()

    if not source_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {source_path}")

    if output_path:
        working_path = Path(output_path).resolve()
    else:
        working_path = source_path.with_name(
            source_path.stem + ".described" + source_path.suffix
        )

    if working_path == source_path:
        raise ValueError("Working copy path must differ from the source database.")

    print("=" * 80)
    print(f"Source database (read-only): {source_path}")
    print(f"Working copy:                {working_path}")
    print("=" * 80)
    print(f"Image/API concurrency: {CONCURRENCY}")
    print(f"Model: {OPENAI_MODEL}")
    print(f"Mermaid diagrams: {ENABLE_MERMAID_DIAGRAMS}")
    print(f"Mermaid visual match loop: {ENABLE_MERMAID_VISUAL_MATCH_LOOP}")
    print(f"Description coverage loop: {ENABLE_DESCRIPTION_COVERAGE_LOOP}")
    print("")

    make_working_copy(source_path, working_path)
    print("Working copy created.")
    print("")

    conn = sqlite3.connect(str(working_path))
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute("SELECT id, body FROM nodes").fetchall()

        # node id -> its image units; plus the original body text to splice into.
        bodies = {}
        node_units = {}

        for row in rows:
            body = row["body"] or ""
            units = extract_image_units(body)

            if units:
                bodies[row["id"]] = body
                node_units[row["id"]] = units

        total_units = sum(len(units) for units in node_units.values())

        # The same image can appear in more than one node. Describe it once,
        # keyed on its media payload, and reuse the result everywhere.
        jobs = {}

        for units in node_units.values():
            for unit in units:
                if not unit["sources"]:
                    unit["key"] = None
                    continue

                key = hashlib.sha256(
                    "|".join(unit["sources"]).encode("utf-8")
                ).hexdigest()

                unit["key"] = key

                job = jobs.setdefault(
                    key,
                    {"sources": unit["sources"], "description": ""},
                )

                # Reuse any existing description found on any occurrence.
                if not job["description"] and unit["description"]:
                    job["description"] = unit["description"]

        already_described = sum(1 for job in jobs.values() if job["description"])

        print(f"Nodes with images: {len(node_units)}")
        print(f"<image-unit> blocks: {total_units}")
        print(f"Unique images: {len(jobs)}")
        print(f"  with an existing description (judge loop only): {already_described}")
        print(f"  empty (generate, then judge loop): {len(jobs) - already_described}")
        print("")

        if not jobs:
            print("No images found. Nothing to do.")
            return

        client = make_chat_client()

        semaphore = asyncio.Semaphore(CONCURRENCY)
        keys = list(jobs)

        tasks = [
            describe_image_unit(
                client=client,
                sources=jobs[key]["sources"],
                existing_description=jobs[key]["description"],
                semaphore=semaphore,
                label=f"Image {position}/{len(keys)}",
            )
            for position, key in enumerate(keys, start=1)
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        described = {}
        failed = 0

        for key, result in zip(keys, results):
            # One image failing must not discard the rest of the run.
            if isinstance(result, BaseException):
                print(f"[WARN] Image task failed: {result}")
                failed += 1
                continue

            if result:
                described[key] = result

        # Splice new descriptions back in, leaving <image-media> untouched.
        touched_nodes = []
        rewritten_units = 0

        for node_id, units in node_units.items():
            body = bodies[node_id]
            changed = False

            # Reverse order so earlier spans stay valid as we splice.
            for unit in sorted(units, key=lambda u: u["span"][0], reverse=True):
                description = described.get(unit["key"])

                if not description:
                    continue

                start, end = unit["span"]
                body = body[:start] + render_image_unit(
                    unit["media_html"], description
                ) + body[end:]

                changed = True
                rewritten_units += 1

            if changed:
                conn.execute(
                    "UPDATE nodes SET body=?, updated_at=? WHERE id=?",
                    (body, now_iso(), node_id),
                )
                touched_nodes.append(node_id)

        invalidate_search_index(conn, touched_nodes)

        conn.commit()

        print("")
        print("=" * 80)
        print("Done.")
        print("=" * 80)
        print(f"Images described: {len(described)}/{len(keys)} (failed: {failed})")
        print(f"<image-unit> blocks rewritten: {rewritten_units}/{total_units}")
        print(f"Nodes updated: {len(touched_nodes)}")
        print(f"Original left untouched: {source_path}")
        print(f"Result written to:       {working_path}")

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Fill in and audit <image-unit> descriptions inside a wiki SQLite "
            "database. Images are read from the base64 payloads already embedded "
            "in nodes.body. The source database is never modified: all work lands "
            "in a working copy."
        )
    )

    parser.add_argument(
        "database",
        help="Path to the source .sqlite file. Opened read-only, never modified.",
    )

    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help=(
            "Path for the working copy. "
            "Defaults to <name>.described.sqlite next to the source."
        ),
    )

    args = parser.parse_args()

    asyncio.run(process_sqlite_database(args.database, args.output))
