import asyncio
import base64
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

load_dotenv()


MERMAID_PUPPETEER_CONFIG_FILE = os.environ.get(
    "PUPPETEER_CONFIG_PATH", "./puppeteer-config.json"
)

# =========================
# MERMAID VALIDATION CONFIG
# =========================

ENABLE_MERMAID_DIAGRAMS = True
VALIDATE_MERMAID = True
MERMAID_CLI_BIN = "mmdc"
MERMAID_REPAIR_ATTEMPTS = 2
MERMAID_PARSE_TIMEOUT_SECONDS = 30

# True:
#   If mmdc is missing, stop with an error.
# False:
#   If mmdc is missing, warn and skip Mermaid validation.
MERMAID_CLI_REQUIRED = False


# =========================
# MERMAID VISUAL MATCH LOOP
# =========================

ENABLE_MERMAID_VISUAL_MATCH_LOOP = True
MERMAID_VISUAL_MATCH_ATTEMPTS = 10
MERMAID_VISUAL_MATCH_GOOD_ENOUGH_SCORE = 95
MERMAID_RENDER_TIMEOUT_SECONDS = 30

# If True, final output includes a small note with the selected judge score.
INCLUDE_VISUAL_JUDGE_NOTE = True


# =========================
# MARKDOWN IMAGE MATCHING
# =========================

IMAGE_MARKDOWN_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)]+)\)")

FENCE_START_RE = re.compile(r"^\s*(```|~~~)")

MERMAID_BLOCK_RE = re.compile(
    r"```mermaid[ \t]*\r?\n(?P<code>.*?)```",
    re.IGNORECASE | re.DOTALL,
)


def read_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def ensure_reconstruction_wrapper(description: str) -> str:
    description = description.strip()

    if not description.startswith(
        "[Image reconstruction:"
    ) and not description.startswith("[Image description:"):
        description = f"[Image reconstruction:\n{description}\n]"

    return description


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
# MERMAID VALIDATION / RENDERING
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
            '  "executablePath": "/path/to/chrome-headless-shell",\n'
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


def remove_mermaid_blocks(markdown_text: str) -> str:
    return ensure_reconstruction_wrapper(MERMAID_BLOCK_RE.sub("", markdown_text).strip())


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
            '  "executablePath": "/path/to/chrome-headless-shell",\n'
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
                print(
                    "[WARN] Skipping missing image for visual comparison: "
                    f"{img['resolved']}"
                )
                continue

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
