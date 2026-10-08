# llm-wiki-air architecture

The public pipeline is split into five phases: `convert`, `wiki`, `linker`,
`index`, and `publisher`. `runner/` composes them, and `common/` contains
phase-neutral paths, storage, settings, context, policies, and markers.

## Boundaries

- A phase exposes its `Config`, `Input`, `Result`, and `run` from `__init__.py`.
- A phase writes only its own output paths from `common.paths.DataLayout`.
- Phase code imports `common` and `jev`; orchestration belongs in `runner`.
- The CLI is an adapter over runner functions. Existing command names and flags
  remain compatible.
- `standard` is the default policy. Missing persisted policy data means
  `standard`. The engine never branches on a policy name: it calls the hooks on
  `common/policy.py:Policy`, whose defaults are the standard behaviour, and
  `graph/fast/` overrides them (`sync --fast`). Fast keys its caches with
  `<version>:<fast version>` via `Policy.cache_key`.
- `sync` builds and promotes one document at a time; `publisher/ahead.py` only
  fills content-addressed caches (parse, linker metadata) under
  `metadata/cache/` in the background. Keep every write in the serial loop.

## Frozen data contract

Do not rename or remove paths, JSON keys, schema versions, SQLite tables,
markers, IDs, prompt/version constants, Git refs, or GROWI paths. New data is
additive. Upgrade old artifacts lazily, only when a document is already being
touched, and keep the old bytes when a deterministic upgrade would differ.

## Verification

From this directory, use the project's configured Python environment:

```bash
python -m compileall -q common convert wiki linker index publisher runner
python -m unittest discover -s tests
```

Real projects should run `python -m runner.compat_check --project configs/<project>.ini`
(it runs sync, build all and index on a copy with model/parser/GROWI-write calls
blocked) before a deployment.

## Do not

- put phase ordering in a phase;
- add a second implementation of sync or publication;
- change standard prompts, IDs, markers, output names, or schemas during a
  folder move;
- bulk migrate existing project data;
- make a phase depend on the full `Settings` object once its typed config is
  available.

## Starting subagents here

This session runs `gemma-4-31B` through a custom OpenAI-compatible gateway
(`model`, `openai_base_url` in `~/.codex/config.toml`), not a stock OpenAI model.

- Omit `model` on `spawn_agent` so the subagent inherits the session model. That
  is the only working path.
- Never set `model`. `spawn_agent` validates against a fixed list
  (`gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`) that
  rejects `gemma-4-31B`, and every one of those names 404s at this gateway.
- Never set `reasoning_effort`. `gemma-4-31B` supports none (the gateway returns
  an empty supported list), so `low`/`medium`/`xhigh` all fail the spawn. The
  `model_reasoning_effort` value in `~/.codex/config.toml` is ignored by it.
- Ignore what a subagent says it is running as; it guesses. Only the spawn
  result and gateway errors are evidence.
- Finished subagents still count against the concurrency limit; `close_agent`
  once their result is integrated.
