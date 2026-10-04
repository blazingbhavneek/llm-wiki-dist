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
  `standard`; `fast` is additive and uses `fast-v1` cache keys.

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
