"""The agent seam for one bounded worker turn.

``ChatAgent`` runs a bounded structured call through the ``ModelPort``.
``HermesAgent`` and ``PiAgent`` drive their CLIs as file-editing workers with
isolated working directories, bounded runtimes, and explicit model profiles.

Structured turns use a JSON artifact. Direct-edit turns use the edited file as
their result, so successful work does not depend on bookkeeping JSON.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from .config import HermesConfig, NeoConfig, PiConfig
from .images import scrub_base64
from .prompts import Prompt, render_agent_prompt
from .storage import read_text, write_text_atomic

#: The CLI flags in one place: if a Hermes release renames one, this is the
#: only table that changes, and every worker keeps the same contract.
HERMES_FLAGS: dict[str, str] = {
    "model": "--model",
    "provider": "--provider",
    "reasoning": "--reasoning",
    "toolsets": "--toolsets",
    "max_turns": "--max-turns",
    "cwd": "--in",
    "resume": "--resume",
    "prompt_file": "--query-file",
    "quiet": "-Q",
}

OUTPUT_TAIL_CHARS = 4000
POLL_SECONDS = 0.25


class AgentError(Exception):
    """A worker turn that produced nothing usable, with the reason."""


@dataclass
class AgentReply:
    payload: Optional[dict[str, Any]] = None
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    error: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.payload is not None and not self.error


@runtime_checkable
class AgentPort(Protocol):
    """One bounded worker turn that answers by writing one JSON artifact."""

    name: str

    async def run(
        self,
        prompt: Prompt,
        schema: type[BaseModel] | None,
        *,
        workdir: Path,
        artifact: str = "artifact.json",
        session: str = "",
        max_output_tokens: int = 8000,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> AgentReply: ...


def validate_payload(payload: Any, schema: type[BaseModel]) -> tuple[dict | None, str]:
    """Validate a worker artifact; never raise, so the caller can retry."""

    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    if not isinstance(payload, dict):
        return None, f"artifact must be a JSON object, got {type(payload).__name__}"
    try:
        return schema.model_validate(payload).model_dump(mode="json"), ""
    except ValidationError as exc:
        return None, scrub_base64(str(exc))[:4000]


def read_artifact(path: Path, schema: type[BaseModel]) -> tuple[dict | None, str]:
    """Read, validate and remove the artifact the worker was told to write."""

    if not path.exists():
        return None, f"worker did not create the artifact file {path.name}"
    raw = read_text(path)
    path.unlink(missing_ok=True)
    if not raw.strip():
        return None, f"artifact file {path.name} is empty"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"artifact file {path.name} is not valid JSON: {exc}"
    return validate_payload(payload, schema)


def read_output_payload(text: str, schema: type[BaseModel]) -> tuple[dict | None, str]:
    """Accept Hermes' final JSON when it returns it instead of using file_write."""

    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates.append(text.strip())
    decoder = json.JSONDecoder()
    for candidate in reversed(candidates):
        starts = [0] if candidate.startswith("{") else []
        starts.extend(match.start() for match in re.finditer(r"\{", candidate))
        for start in reversed(starts):
            try:
                raw, _ = decoder.raw_decode(candidate[start:])
            except json.JSONDecodeError:
                continue
            payload, error = validate_payload(raw, schema)
            if payload is not None:
                return payload, ""
    return None, "worker returned no valid JSON artifact"


# --------------------------------------------------------------------------
# Backend 1: bounded structured chat (no CLI, used by tests)
# --------------------------------------------------------------------------


class ChatAgent:
    """The same contract over ``ModelPort.structured``.

    It exists so the pipeline is runnable and testable without a CLI, and so a
    backend swap never leaks into the workers.
    """

    def __init__(self, model: Any) -> None:
        self.model = model
        self.name = f"chat:{getattr(model, 'name', 'model')}"

    async def run(
        self,
        prompt: Prompt,
        schema: type[BaseModel] | None,
        *,
        workdir: Path,
        artifact: str = "artifact.json",
        session: str = "",
        max_output_tokens: int = 8000,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> AgentReply:
        started = time.time()
        if schema is None:
            return AgentReply(
                error="chat backend cannot directly edit page.md",
                elapsed=time.time() - started,
            )
        messages = prompt.messages()
        try:
            payload = await self.model.structured(
                schema,
                messages,
                max_output_tokens=max_output_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never retried here
            return AgentReply(
                error=scrub_base64(f"{type(exc).__name__}: {exc}")[:2000],
                elapsed=time.time() - started,
            )
        checked, error = validate_payload(payload, schema)
        return AgentReply(
            payload=checked,
            error=error,
            session_id=session,
            usage={"output": max_output_tokens},
            elapsed=time.time() - started,
        )


# --------------------------------------------------------------------------
# Backend 2: the Hermes CLI as a confined worker
# --------------------------------------------------------------------------

#: Only these environment prefixes reach the worker; everything else in the
#: parent environment (including unrelated secrets) stays out of it.
AUTH_ENV_PREFIXES = ("ANTHROPIC", "OPENAI", "AZURE", "AWS", "GEMINI", "GOOGLE")
PASSTHROUGH_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "SSL_CERT_FILE")


class HermesAgent:
    """One CLI worker per turn, with a resumable named session.

    The worker sees: its working directory (prompt + artifact only), its
    session, and the profile's own home.  It never sees the pipeline's Python
    process environment, the source archive, or the publish target.
    """

    def __init__(
        self,
        hermes: HermesConfig,
        *,
        skills_dir: Path | str | None = None,
        timeout_seconds: int = 900,
    ) -> None:
        self.hermes = hermes
        self.skills_dir = Path(skills_dir) if skills_dir else None
        self.timeout_seconds = timeout_seconds
        self.name = f"hermes:{hermes.binary}"

    def environment(self) -> dict[str, str]:
        """A minimal, profile-local environment (plan 23)."""

        import os

        inherited_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
        env: dict[str, str] = {}
        for key, value in dict(os.environ).items():
            if key in PASSTHROUGH_ENV or key.startswith(AUTH_ENV_PREFIXES):
                env[key] = value
        env["HERMES_HOME"] = str(
            Path(
                self.hermes.home
                or os.environ.get("HERMES_HOME", "~/.hermes")
            ).expanduser()
        )
        # Keep state isolated while reusing configured model credentials by
        # symlink. Hermes resolves configuration from HERMES_HOME; the older
        # HERMES_CONFIG/HERMES_ENV variables are not configuration overrides.
        if self.hermes.home:
            profile_home = Path(env["HERMES_HOME"])
            profile_home.mkdir(parents=True, exist_ok=True)
            for name in ("config.yaml", ".env"):
                source = inherited_home / name
                target = profile_home / name
                if source.exists() and not target.exists() and not target.is_symlink():
                    target.symlink_to(source.resolve())
        env["HERMES_PROFILE"] = self.hermes.profile
        env["HERMES_INFERENCE_PROVIDER"] = self.hermes.provider
        env["HERMES_INFERENCE_MODEL"] = self.hermes.model
        if self.hermes.base_url:
            env["CUSTOM_BASE_URL"] = self.hermes.base_url
        env["HERMES_DISABLE_EGRESS"] = "1" if self.hermes.disable_egress else "0"
        if self.skills_dir is not None:
            env["SKILLS_DIR"] = str(self.skills_dir)
        return env

    def command(self, *, prompt_file: Path, workdir: Path, session: str) -> list[str]:
        """The CLI invocation, built from one flag table (see HERMES_FLAGS)."""

        hermes = self.hermes
        flags = HERMES_FLAGS
        command = [hermes.binary, "chat"]
        if hermes.model:
            command += [flags["model"], hermes.model]
        if hermes.provider:
            command += [flags["provider"], hermes.provider]
        if hermes.reasoning:
            command += [flags["reasoning"], hermes.reasoning]
        if hermes.toolsets:
            command += [flags["toolsets"], hermes.toolsets]
        # Do not pass generated document skills through --skills. Hermes name
        # lookup is not reliable for non-ASCII slugs, so callers place the
        # current validated SKILL.md directly in the task prompt instead.
        if hermes.max_turns:
            command += [flags["max_turns"], str(hermes.max_turns)]
        command += ["--oneshot", flags["quiet"]]
        command += [flags["cwd"], str(workdir)]
        if session:
            if hermes.persistent_session:
                command += ["--continue", session, "--create-if-missing"]
        command += [flags["prompt_file"], str(prompt_file)]
        return command + list(hermes.extra_args)


    async def run(
        self,
        prompt: Prompt,
        schema: type[BaseModel] | None,
        *,
        workdir: Path,
        artifact: str = "artifact.json",
        session: str = "",
        max_output_tokens: int = 8000,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> AgentReply:
        started = time.time()
        # Hermes changes into ``--in`` before reading the query.  Relative
        # paths were therefore resolved twice (cwd/workdir/workdir), causing
        # an immediate exit 1 and no artifact on every organizer call.
        workdir = Path(workdir).resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        artifact_path = workdir / artifact
        if schema is not None:
            artifact_path.unlink(missing_ok=True)

        prompt_file = workdir / f"{artifact}.prompt.md"
        rendered = (
            render_agent_prompt(prompt, artifact_path.name)
            if schema is not None
            else prompt.render()
        )
        write_text_atomic(prompt_file, rendered)

        command = self.command(prompt_file=prompt_file, workdir=workdir, session=session)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workdir),
                env=self.environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return AgentReply(
                error=f"{command[0]} not found ({self.name})",
                session_id=session,
                elapsed=time.time() - started,
            )

        output, exit_code = await _drain(
            process, stop_check=stop_check, timeout=self.timeout_seconds
        )
        text = scrub_base64(output)[-OUTPUT_TAIL_CHARS:]
        write_text_atomic(workdir / f"{artifact}.worker.log", text)
        elapsed = time.time() - started

        if schema is None:
            if exit_code not in (0, None):
                error = f"worker exit {exit_code}"
                if text.strip():
                    error += f"; worker output: {text.strip()[-2000:]}"
                return AgentReply(
                    text=text, error=error, session_id=session, elapsed=elapsed
                )
            return AgentReply(
                payload={},
                text=text,
                session_id=session,
                usage={"returncode": exit_code or 0},
                elapsed=elapsed,
            )

        payload, error = read_artifact(artifact_path, schema)
        if payload is None and exit_code in (0, None):
            payload, output_error = read_output_payload(output, schema)
            if payload is not None:
                error = ""
            elif error:
                error = f"{error}; {output_error}"

        if payload is None or exit_code not in (0, None):
            if exit_code not in (0, None):
                detail = error or "artifact was produced but the worker did not exit cleanly"
                error = f"worker exit {exit_code}: {detail}"
            if text.strip():
                error = f"{error}; worker output: {text.strip()[-2000:]}"
            return AgentReply(
                text=text, error=error, session_id=session, elapsed=elapsed
            )
        return AgentReply(
            payload=payload,
            text=text,
            session_id=session,
            usage={"returncode": exit_code or 0},
            elapsed=elapsed,
        )


class PiAgent(HermesAgent):
    """Run one isolated, non-interactive Pi file-editing turn."""

    def __init__(self, pi: PiConfig) -> None:
        self.pi = pi
        self.timeout_seconds = pi.timeout_seconds
        self.name = f"pi:{pi.binary}"

    def environment(self) -> dict[str, str]:
        import os

        env = {
            key: value
            for key, value in dict(os.environ).items()
            if key in PASSTHROUGH_ENV or key.startswith(AUTH_ENV_PREFIXES)
        }
        env["PI_CODING_AGENT_DIR"] = self.pi.config_dir
        return env

    def command(self, *, prompt_file: Path, workdir: Path, session: str) -> list[str]:
        pi = self.pi
        command = [
            pi.binary,
            "--provider",
            pi.provider,
            "--model",
            pi.model,
            "--api-key",
            pi.api_key,
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--approve",
            "--tools",
            pi.tools,
        ]
        if pi.thinking:
            command += ["--thinking", pi.thinking]
        command += list(pi.extra_args)
        return command + ["-p", prompt_file.read_text(encoding="utf-8")]


async def _drain(
    process: Any,
    *,
    stop_check: Optional[Callable[[], bool]],
    timeout: float,
) -> tuple[str, int | None]:
    """Collect output while honouring stop and timeout, then terminate."""

    async def _read(stream) -> str:
        return "" if stream is None else (await stream.read()).decode("utf-8", "replace")

    readers = [
        asyncio.ensure_future(_read(process.stdout)),
        asyncio.ensure_future(_read(process.stderr)),
    ]
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout

    while not all(task.done() for task in readers):
        if process.returncode is None and (
            (stop_check is not None and stop_check()) or loop.time() > deadline
        ):
            _terminate(process)
            break
        await asyncio.sleep(POLL_SECONDS)

    try:
        await asyncio.wait_for(process.wait(), timeout=15)
    except asyncio.TimeoutError:  # pragma: no cover - defensive
        _terminate(process, force=True)
        await asyncio.wait_for(process.wait(), timeout=15)

    output = "".join(
        task.result() for task in readers if task.done() and not task.cancelled()
    )
    return output, process.returncode


def _terminate(process: Any, *, force: bool = False) -> None:
    try:
        process.send_signal(15 if not force else 9)
    except ProcessLookupError:  # pragma: no cover - already gone
        return


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


def make_agent(
    config: NeoConfig, model: Any = None, *, skills_dir: Path | str | None = None
) -> AgentPort:
    """Build the configured worker backend."""

    if config.agent_backend == "hermes":
        return HermesAgent(
            config.hermes,
            skills_dir=skills_dir,
            timeout_seconds=config.hermes.timeout_seconds,
        )
    if config.agent_backend == "pi":
        return PiAgent(config.pi)
    if model is None:
        raise AgentError(f"backend {config.agent_backend!r} needs a model")
    return ChatAgent(model)


def clean_workdir(path: Path | str) -> Path:
    """A fresh, empty working directory for one worker turn."""

    directory = Path(path)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    return directory
