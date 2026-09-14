# Worker resources

Document formats are pipelines, not queues. A parser can perform small work
inline, call an external converter, use the GPU, and then wait for image
descriptions over the network. Each stage chooses the resource it actually
needs.

The server owns only three constrained resources:

| Resource | API | Intended work |
| --- | --- | --- |
| External | `await workers.run_external(...)` | Blocking Pandoc, LibreOffice, or synchronous library calls |
| GPU | `await workers.run_gpu(...)` | MinerU, local OCR, and local vision models |
| Network | `await workers.run_network(...)` | Async LLM and image-description requests |

Detection, markdown assembly, metadata, and other small bounded operations
run inline. There is intentionally no general CPU executor yet. Add one only
after profiling identifies CPU work that noticeably blocks the event loop.

## Why this remains responsive

`run_external` uses a bounded thread executor. `run_gpu` uses a bounded,
spawn-based process executor. Awaiting either yields control to asyncio, so
the API can continue detecting documents, serving health checks, assembling
small results, and making network calls.

The resources are independent. A GPU job never occupies an external slot,
and an LLM request never occupies a GPU process or external thread.

This does not make capacity infinite. When all external slots are occupied,
another external stage waits asynchronously. It does not block the whole
server, but it cannot start until a slot is free. A lightweight operation is
isolated from that wait only when it truly runs inline or uses another
resource. If external-job latency later needs strict prioritization, add
admission control or a reserved slot based on measurements rather than
creating a pool for every format.

## Implementing a parser

`BaseParser._extract` is async. Keep simple work directly in this method and
await a worker only around the stage that needs it:

```python
from formats.base import BaseParser, ParseOptions
from workers import Workers


def run_pandoc(document_path: str, output_dir: str) -> str:
    # Blocking subprocess wrapper. Return the generated Markdown path.
    ...


class DocxParser(BaseParser):
    name = "docx"

    @classmethod
    def detect(cls, data: bytes) -> bool:
        return is_docx_zip(data)  # inspect ZIP members; XLSX/PPTX also start with PK

    async def _extract(
        self,
        data: bytes,
        image_dir: str,
        options: ParseOptions,
        workers: Workers,
    ) -> str:
        document_path = write_temporary_docx(data, image_dir)  # small inline setup
        markdown_path = await workers.run_external(
            run_pandoc,
            document_path,
            image_dir,
        )
        return await embed_and_describe(markdown_path, options, workers)
```

The concrete PDF and DOCX parsers use `utils.markdown_images` after conversion.
It base64-encodes each unique extracted image once, schedules descriptions via
the network limiter, and emits the canonical `<image-unit>` representation.
The XLSX parser uses the same utility after its OpenPyXL stage. When available,
headless LibreOffice first recalculates formulas as a separate external stage;
neither recalculation nor workbook traversal blocks the asyncio event loop.

Do not put an entire parser in an executor merely because one stage is
blocking. This would make later network waits occupy that worker unnecessarily.

### Async subprocesses

Prefer asyncio's subprocess API when the converter has a normal command-line
interface. Reserve the same shared external capacity while it runs:

```python
import asyncio


async def convert(path: str, workers: Workers) -> bytes:
    async with workers.external_slot():
        process = await asyncio.create_subprocess_exec(
            "pandoc",
            path,
            "-t",
            "gfm",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace"))
        return stdout
```

Use `run_external` instead when an existing converter exposes only a blocking
Python function. Never call `run_external` from inside `external_slot`; both
acquire the same capacity and nesting them can deadlock.

### GPU stages

GPU functions must be importable top-level functions. Arguments and results
must be picklable because the executor uses spawned child processes:

```python
_model = None


def run_mineru(path: str) -> str:
    global _model
    if _model is None:
        _model = load_mineru_model()  # once in the GPU process
    return _model.parse(path)


# Inside _extract:
markdown = await workers.run_gpu(run_mineru, document_path)
```

Do not submit lambdas, closures, open file objects, HTTP clients, or parser
instances containing unpicklable state. For large uploads, prefer passing a
temporary file path instead of copying the complete byte string into the
process.

By default there is one GPU process. If the host has multiple GPUs, configure
ownership explicitly before increasing `gpu_workers`; merely increasing the
number can make several processes fight for the same device.

### Network and image-description stages

Use one long-lived async HTTP client and limit every active request through
`run_network`:

```python
import asyncio


async def describe_images(images, client, workers: Workers) -> list[str]:
    calls = [
        workers.run_network(client.describe_image, image)
        for image in images
    ]
    return await asyncio.gather(*calls)
```

The shared semaphore limits active calls across all documents. `gather` may
create many waiting coroutines, so apply a per-document image limit or process
large image collections in batches. Use `network_slot()` only when one logical
request requires several awaits while retaining the same slot. Do not call
`run_network` from inside `network_slot`.

## Inline-work rule

Inline work runs on the FastAPI event-loop thread. It should have a predictable,
short upper bound: magic-byte detection, small transformations, base64 for
ordinary images, and result assembly are appropriate. Large workbook traversal,
image resizing, compression, or unusually large base64 payloads are not
automatically safe merely because they are called "parsing." Measure event-loop
latency and introduce a CPU executor only if real workloads require it.

## Streaming and cancellation

`stream_response = True` on a parser enables SSE heartbeats for a long parse.
This is an HTTP response policy and is intentionally independent of resource
selection. A parser may stream even when it uses several different stages.

Cancelling an await stops the coroutine from waiting, but Python cannot forcibly
stop a function already running in a thread or process. External commands should
have timeouts, and network requests need connect/read timeouts.

## Configuration and deployment

`WorkerConfig` controls local limits:

```python
Workers(WorkerConfig(
    external_workers=4,
    gpu_workers=1,
    network_concurrency=8,
))
```

These limits apply per API process. Running four Uvicorn workers creates four
sets of executors and four network semaphores. On a one-GPU host, normally run
one application process or move GPU ownership into a separate service. The
`/workers` endpoint reports configured, active, and waiting work for each
resource.
