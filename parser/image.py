#!/usr/bin/env python3

import asyncio
import base64
import json
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

# =========================
# HARDCODED CONFIG VALUES
# =========================

MARKDOWN_FOLDER = "/home/seigyo/llm-wiki/mineru/"

# Number of Markdown files to process at the same time.
FILE_CONCURRENCY = 5

MERMAID_PUPPETEER_CONFIG_FILE = "/home/seigyo/llm-wiki/puppeteer-config.json"

OUTPUT_FILE = None
# If OUTPUT_FILE is None, output will be:
# input_filename.described.md

OPENAI_BASE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
OPENAI_API_KEY = (
    "<API_KEY>"
)
OPENAI_MODEL = "google/gemma-4-31b-it"

CONCURRENCY = 5
TEMPERATURE = 0.3

PREVIOUS_CONTEXT_LINES = 300
NEXT_CONTEXT_LINES = 10

TOP_P = 0.95
MAX_TOKENS = 16384


def normalize_invoke_url(base_url: str) -> str:
    base = (base_url or OPENAI_BASE_URL).rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def read_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


class _AsyncChatCompletions:
    def __init__(self, client: "NvidiaAsyncClient"):
        self._client = client

    async def create(
        self,
        *,
        model: str,
        messages,
        temperature: float,
        extra_body: dict | None = None,
        max_tokens: int = MAX_TOKENS,
    ):
        return await self._client.create_completion(
            model=model,
            messages=messages,
            temperature=temperature,
            extra_body=extra_body,
            max_tokens=max_tokens,
        )


class _AsyncChatNamespace:
    def __init__(self, client: "NvidiaAsyncClient"):
        self.completions = _AsyncChatCompletions(client)


class NvidiaAsyncClient:
    def __init__(self, *, base_url: str, api_key: str, timeout: int = 300):
        self.base_url = normalize_invoke_url(base_url)
        self.api_key = api_key
        self.timeout = timeout
        self.chat = _AsyncChatNamespace(self)

    async def create_completion(
        self,
        *,
        model: str,
        messages,
        temperature: float,
        extra_body: dict | None = None,
        max_tokens: int = MAX_TOKENS,
    ):
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": TOP_P,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": True},
        }
        if extra_body:
            payload.update(extra_body)

        return await asyncio.to_thread(self._post_json, payload)

    def _post_json(self, payload: dict):
        response = requests.post(
            self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()


# =========================
# MERMAID VALIDATION CONFIG
# =========================

VALIDATE_MERMAID = True
MERMAID_CLI_BIN = "mmdc"
MERMAID_REPAIR_ATTEMPTS = 2
MERMAID_PARSE_TIMEOUT_SECONDS = 30

# True:
#   If mmdc is missing, stop with an error.
# False:
#   If mmdc is missing, warn and skip Mermaid validation.
MERMAID_CLI_REQUIRED = True


# =========================
# MERMAID VISUAL MATCH LOOP
# =========================

ENABLE_MERMAID_VISUAL_MATCH_LOOP = True
MERMAID_VISUAL_MATCH_ATTEMPTS = 10
MERMAID_VISUAL_MATCH_GOOD_ENOUGH_SCORE = 95
MERMAID_RENDER_TIMEOUT_SECONDS = 30
JUDGE_TEMPERATURE = 0.0

# If True, final output includes a small note with the selected judge score.
INCLUDE_VISUAL_JUDGE_NOTE = True


# =========================
# OPTIONAL QUALITY RETRY
# =========================

# Keep this False if you only want Mermaid repair/visual refinement and not general quality retries.
ENABLE_SHALLOW_RETRY = False


# =========================
# MARKDOWN IMAGE MATCHING
# =========================

IMAGE_MARKDOWN_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)]+)\)")

FENCE_START_RE = re.compile(r"^\s*(```|~~~)")

MERMAID_BLOCK_RE = re.compile(
    r"```mermaid[ \t]*\r?\n(?P<code>.*?)```",
    re.IGNORECASE | re.DOTALL,
)


def is_remote_url(path: str) -> bool:
    parsed = urlparse(path)
    return parsed.scheme in {"http", "https"}


def strip_markdown_title(target: str) -> str:
    """
    Handles common Markdown image target forms:

      image.png
      image.png "title"
      image.png 'title'
      <image path.png>

    Note:
      If your paths contain spaces and also have titles, Markdown parsing can be ambiguous.
      For paths with spaces, prefer:
        ![alt](<path with spaces/image.png>)
    """
    target = target.strip()

    if target.startswith("<") and ">" in target:
        return target[1 : target.index(">")].strip()

    for quote in [' "', " '"]:
        if quote in target:
            target = target.split(quote, 1)[0]
            break

    return target.strip()


def remove_url_query_and_fragment(path: str) -> str:
    parsed = urlparse(path)

    if parsed.scheme or parsed.netloc:
        return path

    path = path.split("#", 1)[0]
    path = path.split("?", 1)[0]

    return path


def resolve_image_path(markdown_file: Path, target: str) -> str:
    """
    Resolves local relative image paths relative to the Markdown file directory.
    Remote URLs are returned unchanged.
    """
    target = strip_markdown_title(target)
    target = unquote(target)

    if is_remote_url(target):
        return target

    target = remove_url_query_and_fragment(target)

    image_path = Path(target)

    if not image_path.is_absolute():
        image_path = markdown_file.parent / image_path

    return str(image_path.resolve())


def image_file_to_data_url(image_path: str) -> str:
    mime_type, _ = mimetypes.guess_type(image_path)

    if mime_type is None:
        mime_type = "image/png"
    return f"data:{mime_type};base64,{read_b64(image_path)}"


def extract_images_from_line(line: str, markdown_file: Path):
    """
    Returns all Markdown images found in one line.
    """
    images = []

    for match in IMAGE_MARKDOWN_RE.finditer(line):
        alt = match.group("alt")
        target = match.group("target")
        resolved = resolve_image_path(markdown_file, target)

        images.append(
            {
                "alt": alt,
                "original_target": target,
                "resolved": resolved,
                "original_markdown": match.group(0),
            }
        )

    return images


def find_image_line_indices(lines):
    """
    Finds image lines while skipping fenced code blocks.

    This prevents replacement of literal Markdown examples like:

        ```md
        ![example](image.png)
        ```
    """
    image_line_indices = []
    inside_fence = False

    for i, line in enumerate(lines):
        if FENCE_START_RE.match(line):
            inside_fence = not inside_fence
            continue

        if inside_fence:
            continue

        if IMAGE_MARKDOWN_RE.search(line):
            image_line_indices.append(i)

    return image_line_indices


def build_context(lines, index: int) -> str:
    start = max(0, index - PREVIOUS_CONTEXT_LINES)
    end = min(len(lines), index + NEXT_CONTEXT_LINES + 1)

    context_lines = []

    for i in range(start, end):
        marker = " <-- IMAGE LINE" if i == index else ""
        context_lines.append(f"{i + 1}: {lines[i].rstrip()}{marker}")

    return "\n".join(context_lines)


def get_response_text(response) -> str:
    """
    Handles normal OpenAI-compatible response content.
    """
    if isinstance(response, dict):
        choices = response.get("choices", [])
        if not choices:
            return ""
        content = choices[0].get("message", {}).get("content", "")
    else:
        content = response.choices[0].message.content

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif hasattr(item, "text"):
                parts.append(item.text)
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()

    return str(content).strip()


def ensure_reconstruction_wrapper(description: str) -> str:
    description = description.strip()

    if not description.startswith(
        "[Image reconstruction:"
    ) and not description.startswith("[Image description:"):
        description = f"[Image reconstruction:\n{description}\n]"

    return description


def looks_too_shallow(description: str) -> bool:
    """
    Heuristic retry trigger.
    """
    lowered = description.lower()

    shallow_phrases = [
        "the image displays",
        "the image shows",
        "this image corresponds to",
        "it illustrates",
        "a diagram showing",
        "a figure showing",
        "representing the",
        "this diagram illustrates",
    ]

    structural_markers = [
        "[Image reconstruction:",
        "Type:",
        "Title/caption:",
        "Reconstructed content:",
        "Detailed notes:",
        "```mermaid",
        "flowchart",
        "graph TD",
        "graph LR",
        "sequenceDiagram",
        "-->",
        "|",
    ]

    has_structure = any(marker in description for marker in structural_markers)
    has_shallow_language = any(phrase in lowered for phrase in shallow_phrases)

    too_short_without_structure = len(description.strip()) < 700 and not has_structure

    return too_short_without_structure or (has_shallow_language and not has_structure)


# =========================
# MERMAID EXTRACTION / EXAMPLES
# =========================


def extract_mermaid_blocks(markdown_text: str):
    """
    Extracts fenced Mermaid blocks from Markdown.

    Returns:
      list[dict]
      Each item has:
        - index
        - code
        - full_block
    """
    blocks = []

    for i, match in enumerate(MERMAID_BLOCK_RE.finditer(markdown_text), start=1):
        blocks.append(
            {
                "index": i,
                "code": match.group("code").strip(),
                "full_block": match.group(0),
            }
        )

    return blocks


def get_good_mermaid_examples() -> str:
    """
    Examples sent to the model when Mermaid repair/improvement is needed.
    Keep IDs simple ASCII. Put Japanese or complex text inside quoted labels.
    """
    return r"""
Good Mermaid examples:

Example 1: simple top-down flowchart

```mermaid
flowchart TD
    A["Start"] --> B["Process"]
    B --> C["End"]
```

Example 2: Japanese labels with safe ASCII node IDs

```mermaid
flowchart LR
    n1["エラー処理機能"] --> n2["ローカルログファイル"]
    n1 --> n3["標準出力"]
    n1 --> n4["構成制御"]
```

Example 3: subgraphs for grouped systems

```mermaid
flowchart LR
    subgraph computer1["計算機1"]
        p1["エラー処理機能"]
        log1["ログファイル"]
    end

    subgraph storage["集中格納計算機"]
        db["集中ログ格納領域"]
    end

    p1 --> log1
    log1 --> db
```

Example 4: labeled arrows

```mermaid
flowchart TD
    app["アプリケーション"] -->|"エラー通知"| middleware["エラー管理ミドルウェア"]
    middleware -->|"ログ出力"| logfile["ログファイル"]
```

Example 5: dotted or dashed relation

```mermaid
flowchart LR
    n1["設定ファイル"] -.-> n2["参照"]
    n2 --> n3["処理"]
```

Example 6: multiple computers / nodes

```mermaid
flowchart LR
    subgraph c1["計算機A"]
        a1["ファイル管理機能"]
        a2["ローカルファイル"]
    end

    subgraph c2["計算機B"]
        b1["ファイル管理機能"]
        b2["ローカルファイル"]
    end

    a1 --> a2
    b1 --> b2
    a1 <-->|"通信"| b1
```

Bad Mermaid:

```mermaid
flowchart TD
    エラー処理機能 --> ログファイル
```

Good Mermaid:

```mermaid
flowchart TD
    n1["エラー処理機能"] --> n2["ログファイル"]
```

Rules:
- Use simple ASCII IDs like n1, n2, server_a, process_1.
- Put Japanese text, spaces, parentheses, punctuation, and long labels inside quoted labels.
- Do not use raw Japanese text as node IDs.
- Do not use Markdown bullets inside Mermaid code blocks.
- Do not put explanatory prose inside Mermaid code blocks.
- Mermaid code block should contain only Mermaid syntax.
- Prefer flowchart TD or flowchart LR for block diagrams and data-flow diagrams.
- If Mermaid syntax is uncertain, simplify the diagram and explain details outside the Mermaid block.
""".strip()


# =========================
# MERMAID VALIDATION / REPAIR
# =========================


async def validate_mermaid_code_with_mmdc(code: str, diagram_index: int):
    """
    Validates one Mermaid diagram by rendering it with mermaid-cli to SVG.

    Uses the configured Puppeteer config so mmdc launches the manually installed
    Chrome Headless Shell instead of trying Chromium/Snap/default Puppeteer browser.

    Returns:
      tuple[bool, str]
      - True, "" if valid
      - False, error_text if invalid
    """
    if not VALIDATE_MERMAID:
        return True, ""

    mmdc_path = shutil.which(MERMAID_CLI_BIN)

    if mmdc_path is None:
        msg = (
            f"Mermaid validation requested, but '{MERMAID_CLI_BIN}' was not found in PATH. "
            "Install it with: npm install -g @mermaid-js/mermaid-cli"
        )

        if MERMAID_CLI_REQUIRED:
            raise RuntimeError(msg)

        print(f"WARNING: {msg}")
        return True, ""

    puppeteer_config_file = Path(MERMAID_PUPPETEER_CONFIG_FILE).expanduser()

    if not puppeteer_config_file.exists():
        msg = (
            "Mermaid validation requested, but Puppeteer config file was not found:\n"
            f"{puppeteer_config_file}\n\n"
            "Expected config should point to your working Chrome Headless Shell, for example:\n"
            "{\n"
            '  "executablePath": "/home/seigyo/llm-wiki/chrome-headless-shell-linux64/chrome-headless-shell",\n'
            '  "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]\n'
            "}"
        )

        if MERMAID_CLI_REQUIRED:
            raise RuntimeError(msg)

        print(f"WARNING: {msg}")
        return True, ""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_file = tmpdir_path / f"diagram_{diagram_index}.mmd"
        output_file = tmpdir_path / f"diagram_{diagram_index}.svg"

        input_file.write_text(code, encoding="utf-8")

        cmd = [
            mmdc_path,
            "-p",
            str(puppeteer_config_file),
            "-i",
            str(input_file),
            "-o",
            str(output_file),
        ]

        process = None

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=MERMAID_PARSE_TIMEOUT_SECONDS,
            )

        except asyncio.TimeoutError:
            if process is not None:
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass

            return (
                False,
                f"Mermaid CLI timed out after {MERMAID_PARSE_TIMEOUT_SECONDS} seconds.\n"
                f"Command: {' '.join(cmd)}",
            )

        except Exception as exc:
            return (
                False,
                "Mermaid CLI failed to start or crashed during validation.\n"
                f"Command: {' '.join(cmd)}\n"
                f"Error: {exc}",
            )

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if (
            process.returncode == 0
            and output_file.exists()
            and output_file.stat().st_size > 0
        ):
            return True, ""

        error_text = "\n".join(
            part
            for part in [
                f"Mermaid block #{diagram_index} failed validation.",
                f"Return code: {process.returncode}",
                f"Command: {' '.join(cmd)}",
                f"STDOUT:\n{stdout_text}" if stdout_text else "",
                f"STDERR:\n{stderr_text}" if stderr_text else "",
                f"Mermaid code:\n```mermaid\n{code}\n```",
            ]
            if part
        )

        return False, error_text


async def validate_all_mermaid_blocks(markdown_text: str):
    """
    Validates all Mermaid code blocks in a generated Markdown replacement.

    Returns:
      tuple[bool, str]
      - True, "" if no Mermaid blocks or all valid
      - False, combined error text if any invalid
    """
    blocks = extract_mermaid_blocks(markdown_text)

    if not blocks:
        return True, ""

    errors = []

    for block in blocks:
        ok, error_text = await validate_mermaid_code_with_mmdc(
            code=block["code"],
            diagram_index=block["index"],
        )

        if not ok:
            errors.append(error_text)

    if errors:
        return False, "\n\n".join(errors)

    return True, ""


def build_mermaid_repair_prompt(error_text: str, current_description: str) -> str:
    return (
        "The Markdown replacement you produced contains at least one Mermaid diagram that does not parse or render.\n\n"
        "Fix the Mermaid syntax while preserving the reconstruction content and meaning. "
        "Return the full corrected Markdown replacement, not only the Mermaid block.\n\n"
        "Important repair rules:\n"
        "- Keep Mermaid node IDs simple ASCII, such as n1, n2, p1, log_file, storage_server.\n"
        "- Put Japanese text, spaces, punctuation, parentheses, and long labels inside quoted labels.\n"
        "- Do not use raw Japanese labels as node IDs.\n"
        "- Do not put normal Markdown text inside a Mermaid code block.\n"
        "- Do not put bullet lists inside a Mermaid code block.\n"
        "- Do not use unsupported syntax.\n"
        "- If a Mermaid feature is uncertain, simplify the diagram so it parses.\n"
        "- Preserve all nodes, edges, labels, groups, and directionality as much as possible.\n"
        "- Keep explanatory details outside the Mermaid code block.\n"
        "- Return only the corrected Markdown replacement.\n\n"
        f"Mermaid parse/render error:\n{error_text}\n\n"
        f"{get_good_mermaid_examples()}\n\n"
        f"Current Markdown replacement to fix:\n{current_description}\n"
    )


async def repair_mermaid_if_needed(
    client: NvidiaAsyncClient,
    original_content,
    description: str,
):
    """
    Validates Mermaid blocks. If invalid, asks the model to repair them.

    Args:
      client:
        NVIDIA chat client.
      original_content:
        The original multimodal content list containing prompt plus image(s).
      description:
        Current generated Markdown replacement.

    Returns:
      str: Mermaid-validated or best-effort repaired Markdown replacement.
    """
    if not VALIDATE_MERMAID:
        return description

    blocks = extract_mermaid_blocks(description)

    if not blocks:
        return description

    for attempt in range(1, MERMAID_REPAIR_ATTEMPTS + 1):
        ok, error_text = await validate_all_mermaid_blocks(description)

        if ok:
            print(f"Mermaid validation passed with {len(blocks)} block(s).")
            return description

        print(
            f"Mermaid validation failed. Repair attempt {attempt}/{MERMAID_REPAIR_ATTEMPTS}."
        )
        print(error_text)
        print("")

        repair_prompt = build_mermaid_repair_prompt(
            error_text=error_text,
            current_description=description,
        )

        repair_response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": original_content,
                },
                {
                    "role": "assistant",
                    "content": description,
                },
                {
                    "role": "user",
                    "content": repair_prompt,
                },
            ],
            temperature=TEMPERATURE,
        )

        description = get_response_text(repair_response).strip()
        description = ensure_reconstruction_wrapper(description)

        blocks = extract_mermaid_blocks(description)

        if not blocks:
            print(
                "Repair response contains no Mermaid blocks. Skipping further Mermaid validation."
            )
            return description

    ok, error_text = await validate_all_mermaid_blocks(description)

    if not ok:
        print("WARNING: Mermaid validation still failed after repair attempts.")
        print(error_text)
        print("")

        description = (
            description.rstrip()
            + "\n\n"
            + "[Mermaid validation warning:\n"
            + "The Mermaid diagram above could not be validated automatically. "
            + "The parse/render error was:\n\n"
            + "```text\n"
            + error_text.strip()
            + "\n```\n"
            + "]"
        )

    return description


# =========================
# MERMAID RENDERING / VISUAL MATCH JUDGE
# =========================


def extract_json_object(text: str):
    """
    Extracts a JSON object from model output.
    Handles plain JSON or fenced ```json blocks.
    """
    text = text.strip()

    fenced = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if fenced:
        text = fenced.group(1).strip()
    else:
        obj = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if obj:
            text = obj.group(0).strip()

    try:
        return json.loads(text)
    except Exception:
        return None


async def render_mermaid_code_to_png_data_url(code: str, diagram_index: int):
    """
    Renders one Mermaid diagram to PNG using mmdc.

    Uses the configured Puppeteer config so mmdc launches the manually installed
    Chrome Headless Shell instead of trying Chromium/Snap/default Puppeteer browser.

    Returns:
      tuple[bool, str, str]
        ok, png_data_url, error_text
    """
    if not VALIDATE_MERMAID:
        return (
            False,
            "",
            "Mermaid rendering requires VALIDATE_MERMAID=True because it uses mmdc.",
        )

    mmdc_path = shutil.which(MERMAID_CLI_BIN)

    if mmdc_path is None:
        msg = (
            f"Mermaid render requested, but '{MERMAID_CLI_BIN}' was not found in PATH. "
            "Install it with: npm install -g @mermaid-js/mermaid-cli"
        )

        if MERMAID_CLI_REQUIRED:
            raise RuntimeError(msg)

        return False, "", msg

    puppeteer_config_file = Path(MERMAID_PUPPETEER_CONFIG_FILE).expanduser()

    if not puppeteer_config_file.exists():
        msg = (
            "Mermaid render requested, but Puppeteer config file was not found:\n"
            f"{puppeteer_config_file}\n\n"
            "Expected config should point to your working Chrome Headless Shell, for example:\n"
            "{\n"
            '  "executablePath": "/home/seigyo/llm-wiki/chrome-headless-shell-linux64/chrome-headless-shell",\n'
            '  "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]\n'
            "}"
        )

        if MERMAID_CLI_REQUIRED:
            raise RuntimeError(msg)

        return False, "", msg

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_file = tmpdir_path / f"diagram_{diagram_index}.mmd"
        output_file = tmpdir_path / f"diagram_{diagram_index}.png"

        input_file.write_text(code, encoding="utf-8")

        cmd = [
            mmdc_path,
            "-p",
            str(puppeteer_config_file),
            "-i",
            str(input_file),
            "-o",
            str(output_file),
        ]

        process = None

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=MERMAID_RENDER_TIMEOUT_SECONDS,
            )

        except asyncio.TimeoutError:
            if process is not None:
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass

            return (
                False,
                "",
                f"Mermaid PNG render timed out after {MERMAID_RENDER_TIMEOUT_SECONDS} seconds.\n"
                f"Command: {' '.join(cmd)}",
            )

        except Exception as exc:
            return (
                False,
                "",
                "Mermaid CLI failed to start or crashed during PNG rendering.\n"
                f"Command: {' '.join(cmd)}\n"
                f"Error: {exc}",
            )

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if (
            process.returncode != 0
            or not output_file.exists()
            or output_file.stat().st_size == 0
        ):
            error_text = "\n".join(
                part
                for part in [
                    f"Mermaid block #{diagram_index} failed PNG rendering.",
                    f"Return code: {process.returncode}",
                    f"Command: {' '.join(cmd)}",
                    f"STDOUT:\n{stdout_text}" if stdout_text else "",
                    f"STDERR:\n{stderr_text}" if stderr_text else "",
                    f"Mermaid code:\n```mermaid\n{code}\n```",
                ]
                if part
            )
            return False, "", error_text

        data_url = image_file_to_data_url(str(output_file))
        return True, data_url, ""


async def render_all_mermaid_blocks_to_pngs(markdown_text: str):
    """
    Renders every Mermaid block in a Markdown replacement to PNG.

    Returns:
      tuple[bool, list[dict], str]

      ok:
        True if all Mermaid blocks rendered.
      rendered:
        list of:
          {
            "index": int,
            "code": str,
            "data_url": str,
          }
      error_text:
        combined render errors if any.
    """
    blocks = extract_mermaid_blocks(markdown_text)

    if not blocks:
        return True, [], ""

    rendered = []
    errors = []

    for block in blocks:
        ok, data_url, error_text = await render_mermaid_code_to_png_data_url(
            code=block["code"],
            diagram_index=block["index"],
        )

        if ok:
            rendered.append(
                {
                    "index": block["index"],
                    "code": block["code"],
                    "data_url": data_url,
                }
            )
        else:
            errors.append(error_text)

    if errors:
        return False, rendered, "\n\n".join(errors)

    return True, rendered, ""


def build_original_image_blocks_for_compare(images):
    """
    Builds image blocks for original Markdown image(s), reused by visual judge/refiner.
    """
    blocks = []

    for image_number, img in enumerate(images, start=1):
        if is_remote_url(img["resolved"]):
            image_url = img["resolved"]
        else:
            if not os.path.exists(img["resolved"]):
                raise FileNotFoundError(
                    f"Image not found for visual comparison: {img['resolved']}"
                )

            image_url = image_file_to_data_url(img["resolved"])

        blocks.append(
            {
                "type": "text",
                "text": (
                    f"Original image {image_number}:\n"
                    f"- Original Markdown: {img['original_markdown']}\n"
                    f"- Alt text: {img['alt']}\n"
                    f"- Resolved path or URL: {img['resolved']}\n"
                ),
            }
        )

        blocks.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": image_url,
                    "detail": "high",
                },
            }
        )

    return blocks


async def judge_mermaid_visual_match(
    client: NvidiaAsyncClient,
    original_image_blocks,
    rendered_mermaid_images,
    description: str,
    context: str,
):
    """
    Judge how well rendered Mermaid image(s) match original image(s).

    Important judging policy:
      - Do NOT reject just because the original is horizontal and Mermaid rendered vertical.
      - Node/component correctness is mandatory.
      - Edge/relationship correctness is mandatory.
      - Missing an edge is a serious failure.
      - Wrong node identity/label is a serious failure.

    Returns:
      dict:
        {
          "score": int 0-100,
          "reason": str,
          "missing": list[str],
          "wrong": list[str],
          "suggested_fixes": list[str]
        }
    """
    content = [
        {
            "type": "text",
            "text": (
                "You are a strict visual judge comparing an original technical document image "
                "against rendered Mermaid reconstruction image(s).\n\n"
                "Your main job is to verify whether the Mermaid reconstruction preserves the "
                "original diagram's INFORMATION, not whether Mermaid chose the exact same visual orientation.\n\n"
                "CRITICAL JUDGING RULES:\n"
                "1. Do NOT penalize heavily just because the original diagram is horizontal but Mermaid rendered it vertical.\n"
                "2. Do NOT penalize heavily just because Mermaid uses top-to-bottom layout instead of left-to-right layout.\n"
                "3. Layout orientation differences are acceptable IF all nodes/components and relationships are preserved.\n"
                "4. If every original node is present, every relationship/edge is present, every edge direction is correct, "
                "and all important labels are preserved, the score may be 100 even if the layout direction differs.\n"
                "5. Missing nodes are serious errors.\n"
                "6. Wrong node labels or mismatched node identities are serious errors.\n"
                "7. Missing edges/relationships are very serious errors.\n"
                "8. Wrong edge direction is a very serious error.\n"
                "9. Missing or wrong arrow labels are errors when those labels are visible/meaningful in the original.\n"
                "10. Extra nodes or extra edges that change the meaning are errors.\n\n"
                "Before scoring, compare systematically:\n"
                "- List the visible nodes/components in the original image.\n"
                "- Check that each original node/component appears in the Mermaid rendering.\n"
                "- Check that there are no incorrectly substituted nodes.\n"
                "- List every visible arrow/edge/relationship in the original image.\n"
                "- Check that each relationship exists in the Mermaid rendering.\n"
                "- Check that every relationship direction is correct.\n"
                "- Check that important edge labels are preserved.\n"
                "- Check grouping/subgraphs/containers only when they carry meaningful information.\n"
                "- Treat orientation/layout as secondary unless it destroys readability or changes meaning.\n\n"
                "Score from 0 to 100:\n"
                "- 100 = all nodes/components are present, all relationships/edges are present, all directions are correct, "
                "important labels are correct. Layout may differ, including horizontal vs vertical.\n"
                "- 90 = all core nodes and edges are correct, only very minor label/style/layout issues.\n"
                "- 80 = mostly correct, but one or two minor non-critical details are missing or visually unclear.\n"
                "- 60 = main idea correct, but several missing/wrong nodes, edges, labels, or groups.\n"
                "- 40 = partially related, but many structural errors.\n"
                "- 20 = very incomplete reconstruction with only a few matching elements.\n"
                "- 0 = unrelated, unusable, or fails to represent the original diagram.\n\n"
                "MANDATORY REJECTION GUIDANCE:\n"
                "- If a major original node/component is missing, the score should usually be below 80.\n"
                "- If multiple original nodes/components are missing, the score should usually be below 60.\n"
                "- If any critical relationship/edge is missing, the score should usually be below 80.\n"
                "- If multiple relationships/edges are missing, the score should usually be below 60.\n"
                "- If edge directions are wrong, the score should drop significantly.\n"
                "- If nodes are present but connected incorrectly, the score should drop significantly.\n"
                "- Do not give a high score to a diagram that has the right-looking nodes but misses edges.\n"
                "- Do not give a high score to a diagram that has edges but connects the wrong nodes.\n\n"
                "What to IGNORE or treat as minor:\n"
                "- Horizontal original rendered vertically by Mermaid.\n"
                "- Left-to-right original rendered top-to-bottom by Mermaid.\n"
                "- Minor spacing differences.\n"
                "- Minor shape differences, unless shape carries important meaning.\n"
                "- Mermaid's automatic layout choices.\n\n"
                "What to focus on most:\n"
                "- exact node/component coverage\n"
                "- Japanese/English labels\n"
                "- arrows/edges and their directions\n"
                "- arrow labels\n"
                "- relationship completeness\n"
                "- grouping/subgraphs/containers when meaningful\n"
                "- missing or extra items\n"
                "- whether someone could reconstruct the original diagram's meaning from the Mermaid output\n\n"
                "Return STRICT JSON only, with this schema:\n"
                "{\n"
                '  "score": 0,\n'
                '  "reason": "short explanation that mentions node and edge correctness",\n'
                '  "missing": ["missing nodes, missing edges, or missing labels"],\n'
                '  "wrong": ["wrong nodes, wrong edges, wrong directions, or harmful extras"],\n'
                '  "suggested_fixes": ["concrete fixes, especially missing edges or node corrections"]\n'
                "}\n\n"
                "Important: If the only issue is orientation/layout direction, say that clearly and still give a high score. "
                "If any edge is missing, say exactly which edge is missing. Never overlook missing relationships.\n\n"
                "Surrounding context:\n"
                f"{context}\n\n"
                "Current Markdown replacement:\n"
                f"{description}\n"
            ),
        }
    ]

    content.extend(original_image_blocks)

    for item in rendered_mermaid_images:
        content.append(
            {
                "type": "text",
                "text": (
                    f"Rendered Mermaid diagram #{item['index']} from this code:\n"
                    f"```mermaid\n{item['code']}\n```"
                ),
            }
        )

        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": item["data_url"],
                    "detail": "high",
                },
            }
        )

    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {
                "role": "user",
                "content": content,
            }
        ],
        temperature=JUDGE_TEMPERATURE,
    )

    raw = get_response_text(response)
    parsed = extract_json_object(raw)

    if not parsed:
        return {
            "score": 0,
            "reason": f"Judge did not return valid JSON. Raw output: {raw}",
            "missing": [],
            "wrong": ["Invalid judge JSON"],
            "suggested_fixes": ["Return valid JSON next time."],
        }

    try:
        score = int(parsed.get("score", 0))
    except Exception:
        score = 0

    score = max(0, min(100, score))

    missing = parsed.get("missing", [])
    wrong = parsed.get("wrong", [])
    suggested_fixes = parsed.get("suggested_fixes", [])

    if not isinstance(missing, list):
        missing = []
    if not isinstance(wrong, list):
        wrong = []
    if not isinstance(suggested_fixes, list):
        suggested_fixes = []

    return {
        "score": score,
        "reason": str(parsed.get("reason", "")),
        "missing": missing,
        "wrong": wrong,
        "suggested_fixes": suggested_fixes,
    }


async def improve_mermaid_from_visual_feedback(
    client: NvidiaAsyncClient,
    original_image_blocks,
    rendered_mermaid_images,
    description: str,
    judge_result: dict,
    context: str,
):
    """
    Asks model to improve the Mermaid reconstruction using visual comparison feedback.
    """
    content = [
        {
            "type": "text",
            "text": (
                "You are improving a Mermaid reconstruction of a technical document image.\n\n"
                "You are given:\n"
                "1. The original image.\n"
                "2. The currently rendered Mermaid diagram image.\n"
                "3. The current Markdown replacement.\n"
                "4. Judge feedback explaining what is missing or wrong.\n\n"
                "Task:\n"
                "- Rewrite the full Markdown replacement.\n"
                "- Fix the Mermaid diagram so its rendered visual structure better matches the original image.\n"
                "- Preserve all useful textual reconstruction notes outside the Mermaid block.\n"
                "- Return only the full corrected Markdown replacement.\n\n"
                "Mermaid rules:\n"
                "- Use simple ASCII node IDs like n1, n2, server_a, proc_1.\n"
                "- Put Japanese text, spaces, punctuation, parentheses, and long labels inside quoted labels.\n"
                "- Do not use raw Japanese as node IDs.\n"
                "- Mermaid code block must contain only Mermaid syntax.\n"
                "- Do not put Markdown bullets or explanatory prose inside Mermaid code blocks.\n"
                "- If a visual feature is hard to express, simplify the Mermaid and explain the feature outside the block.\n\n"
                f"{get_good_mermaid_examples()}\n\n"
                "Judge feedback:\n"
                f"{json.dumps(judge_result, ensure_ascii=False, indent=2)}\n\n"
                "Surrounding context:\n"
                f"{context}\n\n"
                "Current Markdown replacement:\n"
                f"{description}\n"
            ),
        }
    ]

    content.extend(original_image_blocks)

    for item in rendered_mermaid_images:
        content.append(
            {
                "type": "text",
                "text": (
                    f"Current rendered Mermaid diagram #{item['index']} from this code:\n"
                    f"```mermaid\n{item['code']}\n```"
                ),
            }
        )

        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": item["data_url"],
                    "detail": "high",
                },
            }
        )

    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {
                "role": "user",
                "content": content,
            }
        ],
        temperature=TEMPERATURE,
    )

    return ensure_reconstruction_wrapper(get_response_text(response))


async def improve_mermaid_visual_match_loop(
    client: NvidiaAsyncClient,
    original_image_blocks,
    original_content,
    description: str,
    context: str,
):
    """
    Iteratively renders Mermaid, asks a visual judge to compare it to the original image,
    asks the model to improve it, and keeps the best-scoring candidate.
    """
    if not ENABLE_MERMAID_VISUAL_MATCH_LOOP:
        return description

    if not extract_mermaid_blocks(description):
        return description

    best_description = description
    best_score = -1
    best_judge = None

    current_description = description

    for attempt in range(1, MERMAID_VISUAL_MATCH_ATTEMPTS + 1):
        print(f"Mermaid visual match attempt {attempt}/{MERMAID_VISUAL_MATCH_ATTEMPTS}")

        current_description = ensure_reconstruction_wrapper(current_description)

        # Step 1: Ensure Mermaid syntax/render validity.
        current_description = await repair_mermaid_if_needed(
            client=client,
            original_content=original_content,
            description=current_description,
        )

        if not extract_mermaid_blocks(current_description):
            print("No Mermaid block found after repair; stopping visual match loop.")
            break

        # Step 2: Render Mermaid to PNG.
        ok, rendered_images, render_error = await render_all_mermaid_blocks_to_pngs(
            current_description
        )

        if not ok:
            print("Mermaid rendered image generation failed:")
            print(render_error)
            print("Trying syntax repair again...")

            current_description = await repair_mermaid_if_needed(
                client=client,
                original_content=original_content,
                description=current_description,
            )

            ok, rendered_images, render_error = await render_all_mermaid_blocks_to_pngs(
                current_description
            )

            if not ok:
                print("Still cannot render Mermaid. Skipping this candidate.")
                print(render_error)
                continue

        # Step 3: Judge visual match.
        judge_result = await judge_mermaid_visual_match(
            client=client,
            original_image_blocks=original_image_blocks,
            rendered_mermaid_images=rendered_images,
            description=current_description,
            context=context,
        )

        score = judge_result["score"]

        print(f"Mermaid visual judge score: {score}/100")
        print(f"Judge reason: {judge_result.get('reason', '')}")
        print("")

        # Step 4: Keep best candidate.
        if score > best_score:
            best_score = score
            best_description = current_description
            best_judge = judge_result

        # Step 5: Stop early if good enough.
        if score >= MERMAID_VISUAL_MATCH_GOOD_ENOUGH_SCORE:
            print(
                f"Mermaid visual match score {score} >= "
                f"{MERMAID_VISUAL_MATCH_GOOD_ENOUGH_SCORE}; stopping early."
            )
            break

        # Step 6: Improve for next attempt.
        current_description = await improve_mermaid_from_visual_feedback(
            client=client,
            original_image_blocks=original_image_blocks,
            rendered_mermaid_images=rendered_images,
            description=current_description,
            judge_result=judge_result,
            context=context,
        )

    print(f"Best Mermaid visual score selected: {best_score}/100")
    print("")

    if INCLUDE_VISUAL_JUDGE_NOTE and best_judge:
        best_description = (
            best_description.rstrip()
            + "\n\n"
            + "[Mermaid visual match judge:\n"
            + f"Score: {best_score}/100\n"
            + f"Reason: {best_judge.get('reason', '')}\n"
            + "]"
        )

    return ensure_reconstruction_wrapper(best_description)


# =========================
# MAIN RECONSTRUCTION PROMPT
# =========================


def build_reconstruction_prompt(
    markdown_file: Path, index: int, original_line: str, context: str
) -> str:
    return (
        "You are replacing an embedded image in a Markdown technical document with a reconstruction-quality textual representation.\n\n"
        "The goal is NOT to write a short caption. The goal is to extract enough information that a reader could "
        "approximately recreate the original image, table, chart, screenshot, or diagram from your output.\n\n"
        "Use both:\n"
        "1. The image itself.\n"
        "2. The surrounding Markdown context.\n\n"
        "Very important:\n"
        "- Do not merely describe the visual appearance.\n"
        "- Do not produce a one-sentence summary.\n"
        "- Do not say only what the figure is generally about.\n"
        "- Preserve structure, labels, relationships, arrows, tables, values, and layout.\n"
        "- If the image contains Japanese text, transcribe the Japanese text accurately.\n"
        "- If the context gives a Japanese title/caption, preserve it and optionally add an English explanation.\n"
        "- If some text is too small or unclear, write '[unclear]' rather than inventing content.\n\n"
        "Output requirements:\n"
        "- Be exhaustive and concrete.\n"
        "- Preserve all visible text, labels, titles, captions, legends, numbers, arrows, boxes, nodes, columns, rows, "
        "axes, units, UI labels, Japanese text, English text, and relationships.\n"
        "- Prefer structured Markdown that can replace the image in the document.\n"
        "- The output may be multiline.\n"
        "- Return only the Markdown replacement for the image line.\n"
        "- Do not include any preface like 'Here is the reconstruction'.\n\n"
        "For flowcharts, architecture diagrams, block diagrams, dependency graphs, sequence diagrams, or data-flow diagrams:\n"
        "- Output a Mermaid diagram whenever possible.\n"
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
        "For tables:\n"
        "- Recreate the table as a Markdown table.\n"
        "- Preserve all headers, row labels, column labels, values, merged-cell meaning, units, footnotes, and notes.\n"
        "- If the table has merged cells, explain the merge/grouping after the table.\n"
        "- If there are multi-level headers, represent them as clearly as possible in Markdown and explain the hierarchy.\n\n"
        "For charts/graphs:\n"
        "- Identify chart type.\n"
        "- Recreate visible data as a Markdown table when values are visible or reasonably readable.\n"
        "- Describe x-axis, y-axis, units, scale, legend, series, colors/patterns, trends, outliers, and key comparisons.\n"
        "- Include the main takeaway, but do not replace raw details with only a takeaway.\n\n"
        "For screenshots:\n"
        "- Recreate the UI state in text.\n"
        "- Preserve window titles, menus, dialogs, buttons, labels, fields, selected values, error messages, tables, "
        "visible paths, code, logs, and layout.\n"
        "- Describe what is selected, enabled, disabled, highlighted, or emphasized.\n\n"
        "For simple node/link diagrams:\n"
        "- Encode the structure explicitly, for example A --> B.\n"
        "- State the exact visible nodes and exact visible edges.\n"
        "- If the context defines the meaning of the edge, explain that meaning.\n"
        "- If the exact semantic meaning is not given by context, say only that the visible structure is a direct link/edge "
        "and avoid unsupported guesses.\n\n"
        "Preferred output structure:\n\n"
        "[Image reconstruction:\n"
        "Type: <table / flowchart / block diagram / chart / screenshot / photo / other>\n"
        "Title/caption: <visible title or context-derived figure title if available>\n"
        "Reconstructed content:\n"
        "<Markdown table, Mermaid diagram, transcribed text, or detailed structured representation>\n"
        "Detailed notes: <layout, relationships, visual encoding, missing/unclear text, and context-based meaning>\n"
        "]\n\n"
        "If using Mermaid, include it as a fenced mermaid code block exactly like:\n"
        "```mermaid\n"
        "flowchart TD\n"
        '    A["Example"] --> B["Example"]\n'
        "```\n\n"
        "Make the output detailed enough that another person could redraw the image approximately from the text alone.\n\n"
        f"Markdown file: {markdown_file}\n"
        f"Image line number: {index + 1}\n\n"
        f"Original image Markdown line:\n{original_line}\n\n"
        f"Surrounding Markdown context:\n{context}\n"
    )


# =========================
# IMAGE LINE PROCESSING
# =========================


async def describe_image_line(
    client: NvidiaAsyncClient,
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

        context = build_context(lines, index)
        original_image_blocks = build_original_image_blocks_for_compare(images)

        prompt = build_reconstruction_prompt(
            markdown_file=markdown_file,
            index=index,
            original_line=original_line,
            context=context,
        )

        content = [
            {
                "type": "text",
                "text": prompt,
            }
        ]

        for image_number, img in enumerate(images, start=1):
            if is_remote_url(img["resolved"]):
                image_url = img["resolved"]
            else:
                if not os.path.exists(img["resolved"]):
                    raise FileNotFoundError(
                        f"Image not found for line {index + 1}: {img['resolved']}"
                    )

                image_url = image_file_to_data_url(img["resolved"])

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

            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_url,
                        "detail": "high",
                    },
                }
            )

        print(f"Processing image line {index + 1}: {original_line}")

        description = ""

        try:
            try:
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=OPENAI_MODEL,
                        messages=[
                            {
                                "role": "user",
                                "content": content,
                            }
                        ],
                        temperature=TEMPERATURE,
                    ),
                    timeout=120,
                )
            except asyncio.TimeoutError:
                print(
                    f"Image line {index + 1} timed out after 120s. "
                    "Retrying with thinking disabled..."
                )

                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=OPENAI_MODEL,
                        messages=[
                            {
                                "role": "user",
                                "content": content,
                            }
                        ],
                        temperature=TEMPERATURE,
                        extra_body={
                            "chat_template_kwargs": {
                                "enable_thinking": False,
                            }
                        },
                    ),
                    timeout=120,
                )

            description = get_response_text(response)

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
                        "If it is a table, output a Markdown table. If it is a flowchart, block diagram, architecture diagram, "
                        "or data-flow diagram, output a Mermaid diagram plus detailed notes. Preserve all visible labels, arrows, "
                        "nodes, directions, groups, titles, captions, values, Japanese text, English text, and relationships. "
                        "Do not summarize. Do not merely say what the image is about. Return only the replacement Markdown. "
                        "Multiline output is allowed and preferred."
                    ),
                },
            ]

            try:
                try:
                    retry_response = await asyncio.wait_for(
                        client.chat.completions.create(
                            model=OPENAI_MODEL,
                            messages=retry_messages,
                            temperature=TEMPERATURE,
                        ),
                        timeout=120,
                    )
                except asyncio.TimeoutError:
                    print(
                        f"Retry for image line {index + 1} timed out after 120s. "
                        "Retrying with thinking disabled..."
                    )

                    retry_response = await asyncio.wait_for(
                        client.chat.completions.create(
                            model=OPENAI_MODEL,
                            messages=retry_messages,
                            temperature=TEMPERATURE,
                            extra_body={
                                "chat_template_kwargs": {
                                    "enable_thinking": False,
                                }
                            },
                        ),
                        timeout=120,
                    )

                description = get_response_text(retry_response)

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
                context=context,
            )

            description = ensure_reconstruction_wrapper(description)

        image_tags = []

        for img in images:
            if is_remote_url(img["resolved"]):
                src = img["resolved"]
            else:
                if not os.path.exists(img["resolved"]):
                    raise FileNotFoundError(
                        f"Image not found for line {index + 1}: {img['resolved']}"
                    )

                src = image_file_to_data_url(img["resolved"])

            alt_escaped = img["alt"].replace('"', "&quot;")
            image_tags.append(f'<img src="{src}" alt="{alt_escaped}">')

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
    client: NvidiaAsyncClient,
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

        results = await asyncio.gather(*tasks)

        new_lines = list(lines)

        for index, replacement_line in results:
            new_lines[index] = replacement_line

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("".join(new_lines), encoding="utf-8")

        print(f"Done: {input_path}")
        print(f"Output: {output_path}")
        print(f"Replaced image lines: {len(results)}")

        return {
            "status": "done",
            "input": str(input_path),
            "output": str(output_path),
            "reason": "",
            "replaced_image_lines": len(results),
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
    client: NvidiaAsyncClient,
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
    print(f"Previous context lines: {PREVIOUS_CONTEXT_LINES}")
    print(f"Next context lines: {NEXT_CONTEXT_LINES}")
    print(f"Validate Mermaid: {VALIDATE_MERMAID}")
    print(f"Mermaid CLI: {MERMAID_CLI_BIN}")
    print(f"Mermaid repair attempts: {MERMAID_REPAIR_ATTEMPTS}")
    print(f"Mermaid visual match loop: {ENABLE_MERMAID_VISUAL_MATCH_LOOP}")
    print(f"Mermaid visual match attempts: {MERMAID_VISUAL_MATCH_ATTEMPTS}")
    print(f"Mermaid visual good-enough score: {MERMAID_VISUAL_MATCH_GOOD_ENOUGH_SCORE}")
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

    client = NvidiaAsyncClient(
        base_url=OPENAI_BASE_URL,
        api_key=OPENAI_API_KEY,
    )

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


if __name__ == "__main__":
    asyncio.run(process_markdown_folder())
