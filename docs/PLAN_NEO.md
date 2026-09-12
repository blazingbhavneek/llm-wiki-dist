# Implementation plan — neo, fully hardcoded

Companion to `PROMPT.md` (the playbook that says **what a lossless wiki is**) and to the
current `llm-wiki-dist/graph/neo/` package (the prototype that gets it done *sometimes*).
This document says **exactly what to edit**, in what order, and how to prove each step.

Written for an implementer working one work package at a time. It contains the code you
will type. Where it gives code, **type that code** — do not "improve" it. Where it says
"delete", delete. If something in this plan cannot work in the real file, stop and report
the exact line, do not invent a workaround.

---

## STATUS — review of the implementation (2026-09-12)

Reviewed the working tree (uncommitted; the implementer's sandbox could not write `.git`).

| Check | Result |
|---|---|
| WP-1 `ModelPort.text` / `ChatModelPort.text` | ✅ verbatim |
| WP-2 `page.py` | ✅ identical to the verified draft apart from `demote_h1` being factored out (as WP-5 asked) and two `ponytail:` comments |
| WP-3 prompts (`section_write_prompt`, `intro_prompt`, judge paragraph, `target_line` rule) | ✅ |
| WP-4 `_select_references`, `_valid_reference_facts(target)` clamp, no agent | ✅ |
| WP-5 `_write_section` / `_write_intro` / `_rewrite_page` / `_rewrite_all` / sidecar state | ✅ verbatim |
| WP-6 `run_pipeline`, `_review.md`, `run.json`, `__main__` without `--agent-backend` | ✅ |
| WP-7 deletions, C2 grep | ✅ `agent.py` gone, `HermesConfig`/`PiConfig`/`LINK_PROMPT_VERSION` gone, grep clean |
| Tests | ✅ `test_neo_chunking` 33 + `test_neo_page` 12; whole suite green |
| WP-8 real-model smoke | ⏳ **not run** — do it before trusting `wiki` mode in `PLAN_SYNC.md` WP-S1 |

Fixed during review: `test_reference_research_inlines_full_seeds_and_keeps_provenance` had
been deleted instead of adapted (WP-4 step 8); restored with the `tokens=` argument and a
`target_line` clamp assertion. Also noticed: the implementer added cache/venv patterns to the
root `.gitignore` — harmless, keep or drop as you like.

**Next:** commit as one `neo WP-1..7` commit (the per-WP split is lost since nothing was
committed), then run WP-8 on a real manual.

---

## How to use this document

1. Read **Why we are doing this** and **Hard constraints** first. Constraints override
   everything else in this document.
2. Do **one work package at a time**, in order: WP-0 → WP-8.
3. Each work package has **Goal → Files → Steps → Verify → Revert**. Do not start the next
   one until Verify passes.
4. **Commit after each work package**, message `neo WP-N: <goal line>`. One package, one
   commit, so any package can be reverted alone.
5. Run tests with (there is no `pytest` — it is `unittest`, and the argument is a dotted
   module path, not a file path):

   ```bash
   cd llm-wiki-dist
   .venv/bin/python -m unittest tests.test_neo_chunking tests.test_neo_page
   ```

   and before every commit, the whole suite:

   ```bash
   cd llm-wiki-dist
   for f in tests/test_*.py; do .venv/bin/python -m unittest "tests.$(basename ${f%.py})" || echo "FAILED $f"; done
   ```

---

## Why we are doing this

### What neo is trying to do

Turn one big Japanese Markdown manual (thousands of lines, base64 images inline) into a
folder of numbered wiki pages (`001-…md`, `002-…md`, …) where:

1. every source line belongs to exactly one page (no gaps, no overlap),
2. **nothing technical is lost** — code blocks, tables, identifiers like `mpf_mfs_open`,
   error codes like `E_MFS_ETIMEOUT`, hex values, warnings — all survive verbatim,
3. each page reads on its own: it starts with what it is for, and it pulls in the few facts
   from *other* pages that a reader needs (with a `（参照元: 原文 123-145行）` marker),
4. pages link to each other, and `index.md` links to all of them.

### What the current code does, and where it breaks

Phases 1–2 (`windows.py`, `document_map.py`) are already how we want everything to work:
Python cuts the source into windows, the model fills a tiny JSON schema per window, Python
validates the result mechanically (`validate_seed_plan`, `_verify_ranges`) and retries with
the exact error. The model cannot skip work because the schema and the checks do not let it.

Phases 3–5 in `pipeline.py` are the opposite. For every page they:

- run a **LangGraph ReAct agent** with `search_references` / `read_reference` /
  `finish_research` tools and *hope* the weak model calls the tools in the right order
  (`_run_reference_agent`, ~200 lines, plus a fallback for when it doesn't),
- launch the **Hermes or Pi CLI** as a subprocess and ask it to *read six files, then write
  `wiki-plan.md`* (`wiki_page_plan_prompt`), then launch it again to *edit `page.md` in
  place* (`simple_page_edit_prompt`), then launch it a third time to *add links*
  (`link_page_prompt`),
- guard against the agent not doing that with retry loops whose error strings tell the
  story: `"planner exited without writing wiki-plan.md"`, `"writer exited without changing
  page.md"`, `"Restore the exact writer input in case a small model edited page.md
  prematurely"`.

With `gemma-4-31B` those agent turns fail or half-work often, each failure costs a 900 s
subprocess timeout, and when a turn "succeeds" nothing mechanical proves the code blocks
and identifiers survived — an LLM judge is asked to notice, and the judge is the same weak
model.

### What "hardcoded" means here

The same discipline as phases 1–2 and as `graph/pages.py` / `graph/librarian.py`:

- **Python decides the work units.** Python cuts each page into sections of ≤ 80 source
  lines. The model rewrites *one section at a time* with the section's numbered source
  text inside the prompt. No files, no tools, nothing to "go and read".
- **Python decides what must survive.** Before any LLM judge runs, Python checks that
  every fenced code block, every table row, every identifier-like token, every image and
  every assigned cross-page fact is present in the draft. If not, the exact list of what is
  missing goes back into the next prompt. The model cannot talk its way past this.
- **Python decides the references.** The previous and next page always, plus the three
  pages that share the most vocabulary. One structured compare call per reference (that
  call already exists and stays).
- **Python does the linking.** First occurrence of another page's title becomes a link.
  Previous/next page links at the bottom. No model call, links resolve by construction.
- **No CLI agent, no ReAct agent, no tools.** `agent.py`, `HermesConfig`, `PiConfig` and
  every agent prompt are deleted in WP-7.

What we give up, on purpose: the model no longer reorders sections across the page or
invents a reader-oriented structure. It gets an intro paragraph (one call, additive) and
readable prose *inside* each section. Lossless beats beautiful (`PROMPT.md`, rule 2).

### Calls per page, after this plan

| Step | Calls | Kind |
|---|---|---|
| reference research | up to 5 (prev, next, 3 lexical) | `structured(ReferenceResearchResult)` — exists |
| section write | 1 per section, ×≤3 attempts | `text()` — **new** |
| section judge | 1 per accepted draft, ×≤2 API retries | `structured(PageJudgeResult)` — exists |
| intro | 1 | `text()` — new |
| links | 0 | Python |

A 200-line page with 3 sections costs about 5 + 3×(1+1) + 1 = 12 short calls, none of them
a subprocess, all of them with the full input in the prompt.

---

## Hard constraints

- **C1 — Phases 1–2 untouched.** Do not edit `windows.py`, `document_map.py`,
  `markdown_blocks.py`, `images.py`, `ids.py`, `storage.py`, `schemas.py`, nor the
  phase-1/2 prompt builders (`window_inventory_prompt`, `regional_plan_prompt`,
  `semantic_plan_prompt`, `seed_plan_compile_prompt`) nor `PROMPT_VERSION` /
  `SEED_PLAN_VERSION`. Existing tests for them must keep passing unchanged.
- **C2 — No agents.** After WP-7, `graph/neo/` must not import `langgraph`,
  `langchain_core.tools`, `subprocess`/`asyncio.create_subprocess_exec`, and must not
  contain the words `hermes`, `pi_`, `PiConfig`, `HermesConfig`, `create_react_agent`,
  `StructuredTool`. Every model call is `model.structured(...)` or `model.text(...)` with
  everything the model needs *inside the prompt*.
- **C3 — Mechanical checks decide, the judge only adds.** A draft that fails
  `check_section` is never accepted, whatever any LLM says. The LLM judge can only add
  omissions to the feedback list; it cannot approve.
- **C4 — Never silently lose content.** If every attempt for a section fails the mechanical
  check, the section is published as the **exact source lines** and the page is listed in
  `wiki/_review.md` with the errors. A page is never left out of the wiki.
- **C5 — Scope.** Only these files change: `llm-wiki-dist/graph/neo/{config,model,wire,
  prompts,pipeline,page,__main__,__init__,agent}.py`, `llm-wiki-dist/tests/test_neo_chunking.py`,
  new `llm-wiki-dist/tests/test_neo_page.py`. Nothing under `graph/` outside `neo/`,
  nothing in `frontend/`, nothing in `pyproject.toml`.
- **C6 — Tests green before every commit**, whole suite, with the loop command above.
- **C7 — Type the code as given.** If a snippet does not fit the file (a name differs, a
  signature changed), stop and report it; do not patch around it.

---

## Reference — the finished flow

```
run_pipeline(source)
  │
  ├─ Phase 1  observe_document      (unchanged)   work/observations/…
  ├─ Phase 2  build_seed_plan       (unchanged)   state/plan.json, work/seeds/NNN-*.md
  │
  └─ _rewrite_all(pages)            ≤ rewrite_concurrency pages in parallel
       │   tokens = word_tokens(each page's source)          ← Python
       └─ _rewrite_page(page)                                 work/page-NNN/
            ├─ _research_references
            │     _select_references  prev + next + top-3 lexical   ← Python
            │     per reference: structured(ReferenceResearchResult)   (exists)
            │     _valid_reference_facts (+ target_line clamp)   ← Python
            │     reference-research.md
            ├─ split_sections(owner range) → [(s,e), …]        ← Python
            ├─ assign_facts(facts, sections)                   ← Python
            ├─ per section: _write_section
            │     loop ≤ write_attempts:
            │        text(section_write_prompt + feedback)
            │        normalize_draft, _preserve_image_placeholders
            │        check_section  → errors? feedback, retry   ← Python gate
            │        structured(page_judge_prompt) → missing? feedback, retry
            │     best clean candidate, else VERBATIM source + flag
            ├─ _write_intro   text(intro_prompt), identifier guard
            ├─ "# title" + intro + sections
            ├─ link_titles + _nav_footer                       ← Python
            └─ restore_images
       write wiki/NNN-*.md + state/pages/NNN.json
  write index.md, state/manifest.json, wiki/_review.md (only if any verbatim section)
```

Run directory after this plan (`.wiki/neo/<slug>-<hash>/`):

```
source/original.md
state/plan.json               seed plan (unchanged format + reference_ranges/provenance)
state/pages/NNN.json          per-page sidecar (see WP-5 for fields)
state/manifest.json
state/run.json
work/observations/…           phase 1 (unchanged)
work/planning/…               phase 2 (unchanged)
work/seeds/NNN-*.md           numbered source per page, for humans
work/research-NNN/            reference-NNN-attempt-01-prompt.md, reference-NNN.json, reference-research.md
work/page-NNN/                section-01-attempt-01-prompt.md, section-01-attempt-01.md,
                              section-01-judge-01-attempt-01-prompt.md, section-01-judge-01.json,
                              intro-prompt.md, intro.md
wiki/index.md
wiki/NNN-<slug>.md
wiki/_review.md               only when some section fell back to verbatim source
```

---

## WP-0 — Baseline

**Goal:** know the starting state and have a branch.

**Files:** none.

**Steps:**

1. `cd /mnt/common/Code/llm-wiki-dist && git status` must be clean. You are on branch
   `growi`. Create the work branch: `git checkout -b neo-hardcoded`.
2. Run the neo tests: `cd llm-wiki-dist && .venv/bin/python -m unittest tests.test_neo_chunking`.
   Expected: `Ran 44 tests … OK`.
3. Run the whole suite with the loop command. Note any `FAILED` line — those are
   pre-existing and not yours, but write them down so you can tell later.

**Verify:** 44 neo tests OK; you have the list of pre-existing failures (expected: none).

**Revert:** nothing to revert.

---

## WP-1 — `ModelPort.text()`: one plain-text model call

**Goal:** the pipeline can ask the model for Markdown as plain text. Markdown inside a JSON
string is where weak models break (escaping, truncation leaves nothing parseable); plain
text is what they are best at, and a truncated answer is still checkable.

**Files:** `llm-wiki-dist/graph/neo/model.py`, `llm-wiki-dist/tests/test_neo_chunking.py`.

**Steps:**

1. In `model.py`, add `import asyncio` and `import re` at the top (after `from __future__`).

2. In the `ModelPort` Protocol, after the `structured` declaration, add:

   ```python
       async def text(
           self,
           messages: Sequence[BaseMessage],
           *,
           max_output_tokens: int | None = None,
       ) -> str:  # pragma: no cover - protocol declaration
           ...
   ```

3. Add this module-level constant above `class ChatModelPort`:

   ```python
   _THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
   ```

4. In `ChatModelPort`, after the `structured` method, add:

   ```python
       async def text(
           self,
           messages: Sequence[BaseMessage],
           *,
           max_output_tokens: int | None = None,
       ) -> str:
           """One bounded plain-text completion; the caller validates the content."""

           llm = (
               self.llm.bind(max_tokens=max_output_tokens)
               if max_output_tokens is not None
               else self.llm
           )
           reply = await asyncio.wait_for(
               llm.ainvoke(list(messages)), timeout=self.config.request_timeout
           )
           content = getattr(reply, "content", reply)
           if isinstance(content, list):
               content = "".join(
                   part.get("text", "") if isinstance(part, dict) else str(part)
                   for part in content
               )
           return _THINK_RE.sub("", str(content))
   ```

5. In `tests/test_neo_chunking.py`, class `FakeModel`, add after `structured`:

   ```python
       async def text(self, messages, *, max_output_tokens=None) -> str:
           self.calls.append("text")
           return await self.behaviour(None, messages)
   ```

   (Behaviours receive `schema=None` for text calls. Existing behaviours never get `None`
   because nothing calls `text()` yet.)

**Verify:**

```bash
cd llm-wiki-dist && .venv/bin/python -c "
from graph.neo.model import ModelPort, ChatModelPort
import inspect; print(inspect.signature(ChatModelPort.text)); print(inspect.signature(ModelPort.text))"
.venv/bin/python -m unittest tests.test_neo_chunking
```

Expected: signature prints; 44 tests OK.

**Revert:** `git checkout -- graph/neo/model.py tests/test_neo_chunking.py`.

---

## WP-2 — `neo/page.py`: the pure functions

**Goal:** all decisions that do not need a model live in one file of pure functions with no
IO, so they can be tested with strings. This is the core of the whole plan.

**Files:** new `llm-wiki-dist/graph/neo/page.py`, new `llm-wiki-dist/tests/test_neo_page.py`,
`llm-wiki-dist/graph/neo/wire.py`.

**Steps:**

1. In `wire.py`, class `ReferenceFact`, add one field after `source_end`:

   ```python
       target_line: int = 0
   ```

   Docstring stays. (`target_line` = the line of the *target* page's source after which
   the fact belongs; `0` = unknown → first section.)

2. Create `llm-wiki-dist/graph/neo/page.py` with exactly this content:

   ```python
   """Pure helpers for section-wise page writing.

   No file IO and no model calls live here, so every function is testable with
   plain strings.  Python decides section boundaries, what must survive a
   rewrite verbatim, where cross-page facts go, and which titles get linked.
   """

   from __future__ import annotations

   import re
   from typing import Sequence

   from .markdown_blocks import atomic_windows, build_block_index
   from .wire import ReferenceFact

   HEADING_RE = re.compile(r"^#{1,4} \S")
   CODE_TOKEN_RE = re.compile(r"0[xX][0-9A-Fa-f]+|[A-Za-z_][A-Za-z0-9_]{2,}")
   WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}|[ァ-ヶー]{3,}|[一-龯]{2,}")
   REFERENCE_MARKER_RE = re.compile(r"（参照元:\s*原文\s*(\d+)\s*(?:[-–—]\s*(\d+)\s*)?行）")
   THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
   PLACEHOLDER_RE = re.compile(r"\[\[NEO-IMAGE:[A-Za-z0-9_-]+\]\]")


   def _nonblank(lines: Sequence[str], start: int, end: int) -> int:
       return sum(1 for line in lines[start - 1 : end] if line.strip())


   def split_sections(
       lines: Sequence[str],
       start: int,
       end: int,
       *,
       target: int = 80,
       min_lines: int = 8,
   ) -> list[tuple[int, int]]:
       """Cut one page's owned range into writer-sized sections.

       Cuts happen before Markdown headings (never inside a fence, table or
       image unit); anything longer than ``target`` is split on atomic-block
       boundaries; anything with fewer than ``min_lines`` non-blank lines is
       merged into its neighbour.  The result tiles ``start..end`` exactly.
       """

       page = list(lines[start - 1 : end])
       total = len(page)
       if total == 0:
           return []
       index = build_block_index(page)
       cuts = [
           number
           for number, line in enumerate(page, start=1)
           if number > 1 and HEADING_RE.match(line) and index.cut_is_safe(number)
       ]
       bounds = [1, *cuts, total + 1]
       ranges = [(bounds[i], bounds[i + 1] - 1) for i in range(len(bounds) - 1)]

       split: list[tuple[int, int]] = []
       for s, e in ranges:
           if e - s + 1 > target:
               for ws, we in atomic_windows(page[s - 1 : e], target=target):
                   split.append((s + ws - 1, s + we - 1))
           else:
               split.append((s, e))

       merged: list[list[int]] = []
       for s, e in split:
           if merged and _nonblank(page, merged[-1][0], merged[-1][1]) < min_lines:
               merged[-1][1] = e
           else:
               merged.append([s, e])
       if len(merged) > 1 and _nonblank(page, merged[-1][0], merged[-1][1]) < min_lines:
           merged[-2][1] = merged[-1][1]
           merged.pop()

       result = [(start + s - 1, start + e - 1) for s, e in merged]
       assert result[0][0] == start and result[-1][1] == end
       assert all(result[i][1] + 1 == result[i + 1][0] for i in range(len(result) - 1))
       return result


   def code_tokens(text: str) -> set[str]:
       """Identifier-like tokens that a lossless rewrite must keep verbatim."""

       found: set[str] = set()
       for token in CODE_TOKEN_RE.findall(PLACEHOLDER_RE.sub(" ", text or "")):
           if (
               token[:2].lower() == "0x"
               or "_" in token
               or any(char.isdigit() for char in token)
               or any(char.isupper() for char in token[1:])
           ):
               found.add(token)
       return found


   def word_tokens(text: str) -> set[str]:
       """Coarse vocabulary used only to rank reference candidates."""

       return set(WORD_RE.findall(PLACEHOLDER_RE.sub(" ", text or "")))


   def verbatim_blocks(
       lines: Sequence[str], start: int, end: int
   ) -> list[tuple[str, int, int]]:
       """Fences and tables inside ``start..end`` as (kind, abs_start, abs_end)."""

       page = list(lines[start - 1 : end])
       index = build_block_index(page)
       return [
           (block.kind, start + block.start - 1, start + block.end - 1)
           for block in index.blocks
           if block.kind in ("fence", "table")
       ]


   def _fence_flags(lines: Sequence[str]) -> list[bool]:
       """True for every line that is a fence delimiter or inside a fence."""

       flags: list[bool] = []
       inside = False
       for line in lines:
           if line.lstrip().startswith(("```", "~~~")):
               flags.append(True)
               inside = not inside
               continue
           flags.append(inside)
       return flags


   def demote_h1(text: str) -> str:
       """Turn ``# `` into ``## `` outside fences; the page owns the only H1."""

       lines = text.splitlines()
       flags = _fence_flags(lines)
       return "\n".join(
           ("## " + line[2:]) if not flags[i] and line.startswith("# ") else line
           for i, line in enumerate(lines)
       )


   def normalize_draft(raw: str) -> str:
       """Strip model noise (thinking, a whole-output fence) and demote H1."""

       text = THINK_RE.sub("", raw or "").strip()
       lines = text.splitlines()
       if len(lines) >= 2 and lines[-1].strip() == "```":
           first = lines[0].strip()
           fence_count = sum(1 for line in lines if line.lstrip().startswith("```"))
           if first in ("```markdown", "```md") or (first == "```" and fence_count % 2 == 1):
               lines = lines[1:-1]
       text = demote_h1("\n".join(line.rstrip() for line in lines))
       return text.strip() + "\n"


   def table_row_key(line: str) -> str:
       return re.sub(r"\s*\|\s*", "|", line.strip())


   def check_section(
       draft: str,
       *,
       lines: Sequence[str],
       source_text: str,
       block_ranges: Sequence[tuple[str, int, int]],
       placeholders: Sequence[str],
       facts: Sequence[ReferenceFact],
   ) -> list[str]:
       """Mechanical lossless checks. Every returned string is writer feedback."""

       errors: list[str] = []
       if not draft.strip():
           return ["出力が空である。節の本文をMarkdownで書くこと。"]
       draft_lines = draft.splitlines()
       draft_compact = "\n".join(line.rstrip() for line in draft_lines)
       draft_rows = {table_row_key(line) for line in draft_lines if line.strip().startswith("|")}

       for kind, s, e in block_ranges:
           block = [lines[number - 1] for number in range(s, e + 1)]
           if kind == "fence":
               inner = "\n".join(line.rstrip() for line in block[1:-1])
               if inner.strip() and inner not in draft_compact:
                   errors.append(
                       f"原文 {s}-{e}行のコードブロックが一字一句同じ形で含まれていない。"
                       "中身を変えず、そのまま貼ること。"
                   )
           else:
               missing = [
                   line for line in block
                   if line.strip().startswith("|") and table_row_key(line) not in draft_rows
               ]
               if missing:
                   errors.append(
                       f"原文 {s}-{e}行の表から次の行が欠けている（表は全行そのまま写す）: "
                       + missing[0].strip()[:80]
                   )

       for placeholder in placeholders:
           count = draft.count(placeholder)
           if count != 1:
               errors.append(f"画像トークン {placeholder} は必ず1回だけ置くこと（現在{count}回）。")

       missing_tokens = sorted(code_tokens(source_text) - code_tokens(draft))
       if missing_tokens:
           errors.append(
               "次の識別子・定数が本文から消えている。省略や言い換えをせず必ず書くこと: "
               + ", ".join(missing_tokens[:40])
           )

       marked = [
           (int(match.group(1)), int(match.group(2) or match.group(1)))
           for match in REFERENCE_MARKER_RE.finditer(draft)
       ]
       for fact in facts:
           if not any(ms <= fact.source_start and fact.source_end <= me for ms, me in marked):
               errors.append(
                   f"参照事実「{fact.description.strip()[:60]}」を本文へ組み込み、"
                   f"その直後に（参照元: 原文 {fact.source_start}-{fact.source_end}行）と書くこと。"
               )
       return errors


   def assign_facts(
       facts: Sequence[ReferenceFact], sections: Sequence[tuple[int, int]]
   ) -> list[list[ReferenceFact]]:
       """Bucket each fact into the section that owns its ``target_line``."""

       buckets: list[list[ReferenceFact]] = [[] for _ in sections]
       if not sections:
           return buckets
       for fact in facts:
           index = next(
               (i for i, (s, e) in enumerate(sections) if s <= fact.target_line <= e), 0
           )
           buckets[index].append(fact)
       return buckets


   def _link_once(line: str, title: str, filename: str) -> str | None:
       at = line.find(title)
       while at >= 0:
           before = line[:at]
           if before.count("[") == before.count("]") and before.count("`") % 2 == 0:
               return before + f"[{title}]({filename})" + line[at + len(title):]
           at = line.find(title, at + 1)
       return None


   def link_titles(markdown: str, targets: Sequence[tuple[str, str]]) -> str:
       """Wrap the first plain occurrence of each other page's title in a link.

       Skips fences, headings, tables, HTML/image lines, inline code and existing
       link text.  Idempotent: a title already linked to its file is left alone.
       """

       lines = markdown.splitlines()
       flags = _fence_flags(lines)
       for title, filename in sorted(targets, key=lambda item: -len(item[0])):
           title = title.strip()
           if len(title) < 2 or f"]({filename})" in "\n".join(lines):
               continue
           for i, line in enumerate(lines):
               stripped = line.lstrip()
               if flags[i] or stripped.startswith(("#", "|", "<", "[[NEO-IMAGE", "![")):
                   continue
               linked = _link_once(line, title, filename)
               if linked is not None:
                   lines[i] = linked
                   break
       return "\n".join(lines).rstrip() + "\n"
   ```

3. Create `llm-wiki-dist/tests/test_neo_page.py`:

   ```python
   """Pure section/lossless/link helpers used by the neo page writer."""

   from __future__ import annotations

   import unittest

   from graph.neo import page
   from graph.neo.wire import ReferenceFact


   def source() -> list[str]:
       lines = ["# Title", "", "intro a", "b", "c", "d", "e", "f", "g", "h"]
       lines += ["## A", "```sh", "# comment", "x_y = 1", "```"]
       lines += ["| a | b |", "|---|---|", "| 1 | mpf_open |", ""]
       lines += ["## B"] + [f"line {i} E_CODE_{i}" for i in range(100)]
       return lines


   class SplitSectionsTests(unittest.TestCase):
       def test_sections_tile_the_range_and_cut_before_headings(self) -> None:
           lines = source()
           sections = page.split_sections(lines, 1, len(lines), target=40, min_lines=8)
           self.assertEqual(sections[0][0], 1)
           self.assertEqual(sections[-1][1], len(lines))
           for (_, left_end), (right_start, _) in zip(sections, sections[1:]):
               self.assertEqual(left_end + 1, right_start)
           # "## A" is line 11, "## B" is line 20: both are cut points.
           self.assertIn(11, [s for s, _ in sections])
           self.assertIn(20, [s for s, _ in sections])
           # 100 lines after "## B" are split into ~40-line windows.
           self.assertGreater(len(sections), 3)

       def test_heading_inside_fence_is_not_a_cut(self) -> None:
           lines = ["## top", "text", "```sh", "# not a heading", "```", "tail"]
           self.assertEqual(page.split_sections(lines, 1, 6, target=80, min_lines=1), [(1, 6)])

       def test_tiny_sections_merge(self) -> None:
           lines = ["## a", "x", "## b", "y", "## c", "z"]
           self.assertEqual(page.split_sections(lines, 1, 6, target=80, min_lines=3), [(1, 6)])

       def test_offsets_are_absolute(self) -> None:
           lines = ["skip"] * 10 + ["## a"] + ["x"] * 10 + ["## b"] + ["y"] * 10
           sections = page.split_sections(lines, 11, 32, target=80, min_lines=2)
           self.assertEqual(sections, [(11, 21), (22, 32)])


   class LosslessCheckTests(unittest.TestCase):
       def test_code_tokens_keep_identifiers_and_drop_prose(self) -> None:
           tokens = page.code_tokens("call mpf_mfs_open with E_MFS_ETIMEOUT at 0x1F; the file [[NEO-IMAGE:abc_1]]")
           self.assertEqual(tokens, {"mpf_mfs_open", "E_MFS_ETIMEOUT", "0x1F"})

       def test_verbatim_blocks_are_found_with_absolute_lines(self) -> None:
           self.assertEqual(
               page.verbatim_blocks(source(), 1, 20),
               [("fence", 12, 15), ("table", 16, 18)],
           )

       def test_normalize_strips_wrapper_fence_and_demotes_h1_outside_fences(self) -> None:
           raw = "```markdown\n# Title\n\n```sh\n# comment\nx_y = 1\n```\n```"
           self.assertEqual(
               page.normalize_draft(raw),
               "## Title\n\n```sh\n# comment\nx_y = 1\n```\n",
           )
           self.assertEqual(page.normalize_draft("<think>hmm</think>text"), "text\n")

       def test_check_section_reports_every_lost_item(self) -> None:
           lines = source()
           blocks = page.verbatim_blocks(lines, 1, 20)
           source_text = "\n".join(lines[:20])
           fact = ReferenceFact(description="設定が必要", source_start=300, source_end=301)
           errors = page.check_section(
               "## A\n\nsome prose without the code or table\n",
               lines=lines,
               source_text=source_text,
               block_ranges=blocks,
               placeholders=["[[NEO-IMAGE:x]]"],
               facts=[fact],
           )
           joined = "\n".join(errors)
           self.assertIn("12-15行のコードブロック", joined)
           self.assertIn("16-18行の表", joined)
           self.assertIn("[[NEO-IMAGE:x]]", joined)
           self.assertIn("mpf_open", joined)
           self.assertIn("x_y", joined)
           self.assertIn("300-301行", joined)

       def test_check_section_passes_a_lossless_draft(self) -> None:
           lines = source()
           draft = (
               "## A\n\n```sh\n# comment\nx_y = 1\n```\n\n"
               "|a|b|\n|---|---|\n|1|mpf_open|\n\n[[NEO-IMAGE:x]]\n\n"
               "設定が必要である。（参照元: 原文 300-301行）\n"
           )
           errors = page.check_section(
               draft,
               lines=lines,
               source_text="\n".join(lines[10:19]),
               block_ranges=page.verbatim_blocks(lines, 1, 20),
               placeholders=["[[NEO-IMAGE:x]]"],
               facts=[ReferenceFact(description="設定が必要", source_start=300, source_end=301)],
           )
           self.assertEqual(errors, [])

       def test_facts_land_in_the_section_owning_target_line(self) -> None:
           sections = [(1, 10), (11, 20)]
           facts = [ReferenceFact(target_line=15), ReferenceFact(target_line=0), ReferenceFact(target_line=99)]
           buckets = page.assign_facts(facts, sections)
           self.assertEqual([len(bucket) for bucket in buckets], [2, 1])


   class LinkTitlesTests(unittest.TestCase):
       def test_links_first_plain_occurrence_longest_title_first(self) -> None:
           text = "# H\n\nsee ファイル管理API and ファイル管理 here\n```\nファイル管理\n```\n"
           linked = page.link_titles(text, [("ファイル管理", "1.md"), ("ファイル管理API", "2.md")])
           self.assertIn("[ファイル管理API](2.md)", linked)
           self.assertIn("[ファイル管理](1.md)", linked)
           self.assertIn("```\nファイル管理\n```", linked)
           self.assertEqual(page.link_titles(linked, [("ファイル管理", "1.md")]), linked)

       def test_skips_headings_tables_images_and_inline_code(self) -> None:
           text = "## 概要\n| 概要 | x |\n<img alt='概要'>\n`概要` and 概要\n"
           linked = page.link_titles(text, [("概要", "1.md")])
           self.assertEqual(linked, "## 概要\n| 概要 | x |\n<img alt='概要'>\n`概要` and [概要](1.md)\n")


   if __name__ == "__main__":
       unittest.main()
   ```

**Verify:**

```bash
cd llm-wiki-dist && .venv/bin/python -m unittest tests.test_neo_page tests.test_neo_chunking
```

Expected: all `test_neo_page` tests OK (12), 44 old tests OK. If `split_sections` asserts
fail, you mistyped the function — compare character by character with this plan.

**Revert:** `git checkout -- graph/neo/wire.py && rm graph/neo/page.py tests/test_neo_page.py`.

---

## WP-3 — Prompts: section writer, intro, judge edit, `target_line`

**Goal:** the three prompt texts the new flow uses. Every input the model needs is inside
the prompt; no file paths, no tool names.

**Files:** `llm-wiki-dist/graph/neo/prompts.py`, `llm-wiki-dist/graph/neo/config.py`.

**Steps:**

1. In `config.py`, change the version constant (resume must not reuse pages written by
   the agent flow):

   ```python
   REWRITE_PROMPT_VERSION = "neo-sections-ja-1"
   ```

2. In `prompts.py`, function `reference_research_prompt`, in the `判定規則:` list, after
   the line that starts `"- 各事実のsource_start/source_endは、` add one more rule line:

   ```python
               "- target_lineには、その事実を本文へ入れるべき対象原文の行番号"
               "（対象原文に表示された番号のうち、最も関係の深い行）を書く。\n"
   ```

3. In `prompts.py`, function `page_judge_prompt`, in `system=(...)`, **replace** the
   paragraph that starts `"単なる原文の整形・言い換えで、` and ends
   `"根拠のない説明や不要な長文化を要求してはならない。\n\n"` with:

   ```python
               "候補は原文の一部（節）だけを書き直したものである。節の範囲外の情報、"
               "より詳しい説明、一般的な解説、構成の改善を要求してはならない。"
               "「他ページから追加した事実」が候補に含まれていなければ、それは欠落として報告する。\n\n"
   ```

   The first paragraph (`重要な欠落とは…捏造された欠落を報告しない。`) stays.

4. In `prompts.py`, add these two builders at the end of the file:

   ```python
   # --------------------------------------------------------------------------
   # Section writing (plain text output)
   # --------------------------------------------------------------------------


   def section_write_prompt(
       *,
       page_title: str,
       page_summary: str,
       index: int,
       count: int,
       source_start: int,
       source_end: int,
       numbered_section: str,
       facts_text: str,
       image_context: str,
       output_language: str,
       feedback: Sequence[str] = (),
   ) -> Prompt:
       """Rewrite one section losslessly; everything needed is in this prompt."""

       feedback_block = ""
       if feedback:
           feedback_block = (
               "# 前回の出力の不足（必ず全て直す）\n- "
               + "\n- ".join(item.strip() for item in feedback if item.strip())
               + "\n\n"
           )
       return Prompt(
           kind="section_write",
           version=REWRITE_PROMPT_VERSION,
           system=(
               "あなたは日本語技術Wikiの執筆者である。原文の一部（節）を、"
               "単独で読んで理解できるWikiの節に書き直す。\n"
               "- 出力はMarkdown本文だけ。前置き、説明、JSON、出力全体をコードフェンスで囲むことは禁止。\n"
               "- 事実を捏造しない。原文と追加事実にない引数、動作、例、一般論を書かない。\n"
               "- 原文の技術情報を一切落とさない。コードブロック、表、識別子、定数、数値、単位、"
               "エラーコード、警告文は一字も変えずにそのまま写す。"
           ),
           body=(
               "# 対象\n"
               f"- ページ: {page_title}\n"
               f"- ページ全体の要約: {page_summary or '要約なし'}\n"
               f"- この節: {index}/{count}（原文 {source_start}-{source_end}行）\n"
               f"- 本文は{output_language}で書く。\n\n"
               "# 書き方\n"
               "- 見出しは`##`以下を使う。`# `（H1）は書かない。\n"
               "- 原文の見出しは残してよいが、内容が分かる名前に変えてよい。\n"
               "- 段落や箇条書きに整理し、何のための機能か、いつ使うか、何に注意するかが"
               "原文から分かる範囲で伝わるようにする。文の意味、条件、順序に関わる情報は変えない。\n"
               "- 先頭の行番号は出典を示すためのもので、本文には書かない。\n"
               "- リンク（`[...](...)`）は書かない。「関連ページ」などの一覧も作らない。\n"
               "- 章番号、頁番号、目次など技術的な意味のない体裁だけは省いてよい。\n"
               "- 画像トークンは一字も変えず、元と同じ話題の直後に1回だけ置く。\n"
               f"{image_context}\n\n"
               "# 他ページから追加する事実\n"
               f"{facts_text}\n"
               "各事実は本文の該当箇所へ自然に組み込み、その段落の直後に"
               "`（参照元: 原文 S-E行）`（SとEは各事実の出典行）と書く。"
               f"原文 {source_start}-{source_end}行の情報にはこのマーカーを付けない。\n\n"
               f"{feedback_block}"
               "--- 行番号付き原文（この節） ---\n"
               f"{numbered_section}"
           ),
       )


   def intro_prompt(
       *,
       page_title: str,
       page_summary: str,
       body: str,
       output_language: str,
   ) -> Prompt:
       """One additive lead paragraph written from the finished body only."""

       return Prompt(
           kind="intro",
           version=REWRITE_PROMPT_VERSION,
           system=(
               "あなたは日本語技術Wikiの編集者である。Markdown本文だけを出力する。"
               "前置き、見出し、リンク、箇条書き、コードフェンスは書かない。"
           ),
           body=(
               "次のWiki記事の冒頭に置く導入文を書く。2〜6文で、何のための機能・情報か、"
               "いつ使うか、この記事を読むと何が分かるかを説明する。"
               f"本文にない事実、識別子、数値は書かない。{output_language}で書く。\n\n"
               f"# タイトル\n{page_title}\n\n"
               f"# 要約\n{page_summary or '要約なし'}\n\n"
               f"# 本文\n{body}"
           ),
       )
   ```

   `Sequence` is already imported at the top of `prompts.py`.

**Verify:**

```bash
cd llm-wiki-dist && .venv/bin/python -c "
from graph.neo.prompts import section_write_prompt, intro_prompt, page_judge_prompt
p = section_write_prompt(page_title='t', page_summary='s', index=1, count=2, source_start=10, source_end=20, numbered_section='10: x', facts_text='なし', image_context='（このページに画像はない）', output_language='Japanese', feedback=['a'])
assert '前回の出力の不足' in p.render() and '10: x' in p.render() and 'wiki-plan' not in p.render()
j = page_judge_prompt(page_title='t', owner_ranges='1-2', numbered_original='1: a', candidate='b', output_language='Japanese')
assert '節の範囲外' in j.system and '未完成' not in j.system
print(intro_prompt(page_title='t', page_summary='s', body='b', output_language='Japanese').kind)"
.venv/bin/python -m unittest tests.test_neo_chunking
```

Expected: prints `intro`. **One old test now fails**:
`test_judge_rejects_a_merely_cleaned_source_excerpt` (it asserts the paragraph you just
replaced). Delete that test method now so the suite is green before you commit.

**Revert:** `git checkout -- graph/neo/prompts.py graph/neo/config.py tests/test_neo_chunking.py`.

---

## WP-4 — Reference selection without an agent

**Goal:** `_research_references` picks references in Python (adjacent + lexical overlap)
and keeps the existing pairwise structured compare. `_run_reference_agent` and the
`ReferenceSelection` fallback go away.

**Files:** `llm-wiki-dist/graph/neo/pipeline.py`, `llm-wiki-dist/tests/test_neo_chunking.py`.

**Steps:**

1. In `pipeline.py` imports: add

   ```python
   from .page import (
       REFERENCE_MARKER_RE,
       assign_facts,
       check_section,
       code_tokens,
       demote_h1,
       link_titles,
       normalize_draft,
       split_sections,
       verbatim_blocks,
       word_tokens,
       PLACEHOLDER_RE,
   )
   ```

   (Some names are used only from WP-5 on; importing them now is fine.)

2. Delete these from `pipeline.py` entirely: `_reference_finish_error`,
   `_run_reference_agent`, the module-level `_REFERENCE_MARKER` regex. In
   `_reference_ranges_from_markdown`, replace `_REFERENCE_MARKER.finditer` with
   `REFERENCE_MARKER_RE.finditer`.

3. Replace `_valid_reference_facts` with this version (adds the `target` clamp):

   ```python
   def _valid_reference_facts(
       facts: Sequence[ReferenceFact], page: SeedPage, target: SeedPage
   ) -> list[ReferenceFact]:
       """Keep only cited facts that point inside the compared reference page."""

       valid: list[ReferenceFact] = []
       seen: set[tuple[str, int, int]] = set()
       for fact in facts:
           description = fact.description.strip()
           start, end = fact.source_start, fact.source_end
           if not description or not any(
               owner_start <= start <= end <= owner_end
               for owner_start, owner_end in page.owner_ranges
           ):
               continue
           key = (description, start, end)
           if key in seen:
               continue
           seen.add(key)
           if not any(s <= fact.target_line <= e for s, e in target.owner_ranges):
               fact.target_line = 0
           valid.append(fact)
       return valid
   ```

4. Add this function right above `_research_references`:

   ```python
   def _select_references(
       page: SeedPage,
       pages: Sequence[SeedPage],
       tokens: dict[int, set[str]],
       *,
       limit: int,
   ) -> list[SeedPage]:
       """Previous and next page always, plus the pages sharing the most vocabulary."""

       others = [item for item in pages if item.number != page.number]
       if not others:
           return []
       counts: dict[str, int] = {}
       for vocabulary in tokens.values():
           for token in vocabulary:
               counts[token] = counts.get(token, 0) + 1
       common = {token for token, count in counts.items() if count > len(pages) / 2}
       mine = tokens.get(page.number, set()) - common
       adjacent = [item for item in others if abs(item.number - page.number) == 1]
       scored = sorted(
           (
               (len(mine & (tokens.get(item.number, set()) - common)), -item.number, item)
               for item in others
               if item not in adjacent
           ),
           key=lambda entry: (entry[0], entry[1]),
           reverse=True,
       )
       picks = [item for score, _, item in scored[: max(0, limit)] if score > 0]
       return sorted(adjacent + picks, key=lambda item: item.number)
   ```

5. Replace the whole `_research_references` function with:

   ```python
   async def _research_references(
       page: SeedPage,
       *,
       pages: Sequence[SeedPage],
       lines: Sequence[str],
       units: Sequence[ImageUnit],
       tokens: dict[int, set[str]],
       model: ModelPort,
       config: NeoConfig,
       work_root: Path,
       seed_root: Path,
       stop_check: StopCheck,
       on_progress: Progress,
   ) -> tuple[list[_ReferenceEvidence], str]:
       """Python selects references; one structured compare call per reference."""

       selected = _select_references(
           page, pages, tokens, limit=config.reference_candidates
       )
       if not selected:
           return [], "# 参照調査結果\n\n他のWikiページはない。\n"

       research_dir = clean_workdir(work_root / f"research-{page.number:03d}")
       target_source = _numbered_source(
           lines, page.owner_ranges, _page_units(page, units)
       )
       _emit(
           on_progress,
           "research",
           "selected",
           page=page.title,
           candidates=len(selected),
           references=[item.number for item in selected],
       )

       evidence: list[_ReferenceEvidence] = []
       for current, candidate in enumerate(selected, start=1):
           reference_source = _numbered_source(
               lines, candidate.owner_ranges, _page_units(candidate, units)
           )
           prompt = reference_research_prompt(
               target_number=page.number,
               target_title=page.title,
               target_ranges=_ranges_text(page.owner_ranges),
               target_source=target_source,
               reference_number=candidate.number,
               reference_title=candidate.title,
               reference_ranges=_ranges_text(candidate.owner_ranges),
               reference_source=reference_source,
               output_language=config.output_language,
           )
           result, attempts, error = await _structured_with_artifacts(
               schema=ReferenceResearchResult,
               prompt=prompt,
               model=model,
               output_dir=research_dir,
               stem=f"reference-{candidate.number:03d}",
               attempts=config.reference_attempts,
               max_output_tokens=config.reference_max_output_tokens,
               stop_check=stop_check,
           )
           facts = (
               _valid_reference_facts(result.useful_facts, candidate, page)
               if result
               else []
           )
           reason = (
               result.no_useful_information_reason.strip()
               if result is not None
               else f"調査呼び出し失敗: {error}"
           )
           evidence.append(
               _ReferenceEvidence(
                   page=candidate, facts=facts, no_useful_information_reason=reason
               )
           )
           _emit(
               on_progress,
               "research",
               "reference_done",
               page=page.title,
               reference=candidate.title,
               current=current,
               total=len(selected),
               facts=len(facts),
               attempts=attempts,
               error=error,
           )

       research = _render_reference_research(
           page, evidence, lines=lines, units=units, seed_root=seed_root
       )
       write_text_atomic(research_dir / "reference-research.md", research)
       return evidence, research
   ```

6. In `_render_reference_research`, remove the `researcher_report: str = ""` parameter
   and the `if researcher_report.strip(): …` block.

7. Remove the imports that are now unused in `pipeline.py`: `warnings`,
   `from langchain_core.tools import StructuredTool`,
   `from langgraph.prebuilt import create_react_agent`, and `reference_selection_prompt`,
   `ReferenceSelection` from the `.prompts` / `.wire` import lists.

8. Tests, in `tests/test_neo_chunking.py`:
   - delete `test_reference_agent_cannot_finish_before_required_reads`;
   - in `test_reference_research_inlines_full_seeds_and_keeps_provenance`: remove the
     `if schema is ReferenceSelection:` branch from the fake `research` behaviour, and
     pass `tokens={1: set(), 2: set()}` to `_research_references` (target and reference
     are adjacent, so they are selected without vocabulary). Replace the assertion
     `self.assertEqual(pipeline._researched_ranges(evidence), [(3, 4)])` with
     `self.assertEqual([(f.source_start, f.source_end) for e in evidence for f in e.facts], [(3, 4)])`
     (`_researched_ranges` is deleted in WP-5). Remove `ReferenceSelection` from the
     `graph.neo.wire` import.
   - add to the same class:

     ```python
         def test_reference_selection_is_adjacent_plus_lexical_overlap(self) -> None:
             def seed(number: int) -> pipeline.SeedPage:
                 return pipeline.SeedPage(
                     number=number, title=f"p{number}", chapter="", summary="",
                     owner_ranges=[(number, number)], filename=f"{number:03d}.md",
                     page_id=f"page-{number:03d}",
                 )

             pages = [seed(n) for n in range(1, 8)]
             tokens = {n: {"共通語"} for n in range(1, 8)}
             tokens[4] |= {"mpf_open", "ファイル"}
             tokens[1] |= {"mpf_open", "ファイル"}
             tokens[7] |= {"mpf_open"}
             tokens[6] |= {"無関係"}
             chosen = pipeline._select_references(pages[3], pages, tokens, limit=2)
             self.assertEqual([item.number for item in chosen], [1, 3, 5, 7])
     ```

     (3 and 5 are adjacent; 1 shares two tokens, 7 shares one, 6 shares only the common
     word which is ignored; limit=2 lexical picks.)

   This WP leaves `_rewrite_page` still calling the old `_research_references`
   signature (it passes `model=judge_model`, no `tokens`). That is fine only until WP-5;
   the `_rewrite_page`-level tests (`test_rewrite_publishes_only_after_reference_enrichment_check`,
   `test_keeps_original_and_selects_repaired_second_version`) will now fail. **Delete
   those two tests in this WP** — WP-5 replaces them.

**Verify:**

```bash
cd llm-wiki-dist && .venv/bin/python -m unittest tests.test_neo_chunking tests.test_neo_page
grep -n "create_react_agent\|StructuredTool\|_run_reference_agent\|ReferenceSelection" graph/neo/pipeline.py || echo "clean"
```

Expected: tests OK, `clean`.

**Revert:** `git checkout -- graph/neo/pipeline.py tests/test_neo_chunking.py`.

---

## WP-5 — Section writer: the new `_rewrite_page`

**Goal:** replace the plan/edit/judge/enrichment agent loop with: sections → write →
mechanical check → judge → repair → assemble → intro → links → images. This is the largest
package. Type it in the order given; do not run tests until step 9.

**Files:** `llm-wiki-dist/graph/neo/pipeline.py`, `llm-wiki-dist/graph/neo/config.py`,
`llm-wiki-dist/tests/test_neo_chunking.py`.

**Steps:**

1. `config.py`, class `NeoConfig`: **replace** the block from the comment
   `# Independent page rewriting` down to and including `link_max_output_tokens: int = 8000`
   with:

   ```python
       # Section-wise page writing
       rewrite_concurrency: int = 4
       section_target_lines: int = 80
       section_min_lines: int = 8
       write_attempts: int = 3
       write_max_output_tokens: int = 8000
       intro_max_output_tokens: int = 1500
       reference_candidates: int = 3
       reference_attempts: int = 3
       reference_max_output_tokens: int = 4000
       judge_attempts: int = 2
       judge_max_output_tokens: int = 4000
   ```

   Leave `agent_backend`, `hermes`, `pi` in place for now — WP-7 deletes them.

2. `pipeline.py`, dataclasses. Replace `RewriteResult` with:

   ```python
   @dataclass
   class RewriteResult:
       page: SeedPage
       markdown: str
       attempts: int
       judge_score: int | None = None
       missing_important_information: list[str] = field(default_factory=list)
       verbatim_sections: list[str] = field(default_factory=list)
   ```

   Delete `_JudgedCandidate`. Add, after `_ReferenceEvidence`:

   ```python
   @dataclass
   class _SectionCandidate:
       markdown: str
       attempt: int
       score: int | None = None
       missing: list[str] = field(default_factory=list)
       errors: list[str] = field(default_factory=list)


   @dataclass
   class _SectionResult:
       markdown: str
       attempts: int
       score: int | None = None
       missing: list[str] = field(default_factory=list)
       errors: list[str] = field(default_factory=list)
       verbatim: bool = False
   ```

3. Delete these functions from `pipeline.py` (they only served the agent flow):
   `_yaml_string`, `_render_page`, `_page_index`, `_reference_view`,
   `_stage_rewrite_references`, `_researched_ranges`, `_missing_research_markers`,
   `_judge_wiki_plan`, `_judge_candidate`, `_judge_reference_enrichment`,
   `_INTERNAL_FRONTMATTER`, `_strip_internal_frontmatter`.
   **Keep** `_seed_path`, `_write_reference_seeds`, `_slice_ranges`, `_numbered_source`,
   `_prompt_safe`, `_page_units`, `_image_context`, `_image_neighbors`,
   `_insert_image_near_context`, `_preserve_image_placeholders`, `_judge_feedback`,
   `_structured_with_artifacts`, `_reference_ranges_from_markdown`,
   `_valid_reference_ranges`, `_render_reference_research` — all still used.

4. Add these helpers above `_rewrite_page`:

   ```python
   def _facts_text(
       facts: Sequence[ReferenceFact],
       lines: Sequence[str],
       units: Sequence[ImageUnit],
   ) -> str:
       if not facts:
           return "なし"
       rendered: list[str] = []
       for number, fact in enumerate(facts, start=1):
           fact_units = [
               unit
               for unit in units
               if fact.source_start <= unit.source_start
               and unit.source_end <= fact.source_end
           ]
           excerpt = _numbered_source(
               lines, [(fact.source_start, fact.source_end)], fact_units
           ).rstrip()
           rendered.append(
               f"## 追加事実 {number}\n"
               f"- 事実: {fact.description.strip()}\n"
               f"- 理由: {fact.reason.strip() or '単独で理解するために必要'}\n"
               f"- 出典: 原文 {fact.source_start}-{fact.source_end}行\n"
               "- 根拠抜粋:\n```text\n"
               f"{excerpt}\n```"
           )
       return "\n\n".join(rendered)


   def _verbatim_section(
       lines: Sequence[str], start: int, end: int, units: Sequence[ImageUnit]
   ) -> str:
       """Lossless by construction: the exact source lines, images as placeholders."""

       return demote_h1(_prompt_safe(slice_text(list(lines), start, end), units)).rstrip() + "\n"


   def _nav_footer(page: SeedPage, pages: Sequence[SeedPage]) -> str:
       previous = next((item for item in pages if item.number == page.number - 1), None)
       following = next((item for item in pages if item.number == page.number + 1), None)
       parts = []
       if previous:
           parts.append(f"前のページ: [{previous.title}]({previous.filename})")
       if following:
           parts.append(f"次のページ: [{following.title}]({following.filename})")
       return ("\n---\n\n" + " ｜ ".join(parts) + "\n") if parts else ""
   ```

5. Add `_write_section`:

   ```python
   async def _write_section(
       page: SeedPage,
       start: int,
       end: int,
       *,
       index: int,
       count: int,
       facts: Sequence[ReferenceFact],
       lines: Sequence[str],
       units: Sequence[ImageUnit],
       model: ModelPort,
       config: NeoConfig,
       task_dir: Path,
       stop_check: StopCheck,
       on_progress: Progress,
   ) -> _SectionResult:
       """Write one section until Python's lossless checks and the judge are satisfied."""

       section_units = [
           unit for unit in units if start <= unit.source_start and unit.source_end <= end
       ]
       placeholders = [unit.placeholder for unit in section_units]
       numbered = _numbered_source(lines, [(start, end)], section_units)
       source_text = _prompt_safe(slice_text(list(lines), start, end), section_units)
       blocks = verbatim_blocks(lines, start, end)
       facts_text = _facts_text(facts, lines, units)
       stem = f"section-{index:02d}"
       candidates: list[_SectionCandidate] = []
       feedback: list[str] = []

       for attempt in range(1, max(1, config.write_attempts) + 1):
           if stop_check and stop_check():
               raise asyncio.CancelledError("page writing cancelled")
           prompt = section_write_prompt(
               page_title=page.title,
               page_summary=page.summary,
               index=index,
               count=count,
               source_start=start,
               source_end=end,
               numbered_section=numbered,
               facts_text=facts_text,
               image_context=_image_context(section_units, lines),
               output_language=config.output_language,
               feedback=feedback,
           )
           write_text_atomic(
               task_dir / f"{stem}-attempt-{attempt:02d}-prompt.md", prompt.render()
           )
           try:
               raw = await model.text(
                   prompt.messages(), max_output_tokens=config.write_max_output_tokens
               )
           except Exception as exc:  # noqa: BLE001 - bounded retry with evidence
               feedback = [f"前回の呼び出しが失敗した: {type(exc).__name__}: {exc}"[:500]]
               write_text_atomic(
                   task_dir / f"{stem}-attempt-{attempt:02d}-error.txt", feedback[0] + "\n"
               )
               continue
           draft = normalize_draft(raw)
           # Facts quote other pages' images; only this section's tokens may survive.
           draft = PLACEHOLDER_RE.sub(
               lambda match: match.group(0) if match.group(0) in placeholders else "",
               draft,
           )
           draft = _preserve_image_placeholders(draft, section_units, lines)
           write_text_atomic(task_dir / f"{stem}-attempt-{attempt:02d}.md", draft)
           errors = check_section(
               draft,
               lines=lines,
               source_text=source_text,
               block_ranges=blocks,
               placeholders=placeholders,
               facts=facts,
           )
           if errors:
               candidates.append(_SectionCandidate(draft, attempt, errors=errors))
               feedback = errors
               _emit(
                   on_progress, "write", "section_retry",
                   page=page.title, section=index, attempt=attempt,
                   error="; ".join(errors)[:300],
               )
               continue
           judge_original = numbered
           if facts:
               judge_original += "\n\n--- 他ページから追加した事実（必ず反映） ---\n" + facts_text
           judgment, _judge_attempts, judge_error = await _structured_with_artifacts(
               schema=PageJudgeResult,
               prompt=page_judge_prompt(
                   page_title=f"{page.title}（節 {index}/{count}）",
                   owner_ranges=f"{start}-{end}",
                   numbered_original=judge_original,
                   candidate=draft,
                   output_language=config.output_language,
               ),
               model=model,
               output_dir=task_dir,
               stem=f"{stem}-judge-{attempt:02d}",
               attempts=config.judge_attempts,
               max_output_tokens=config.judge_max_output_tokens,
               stop_check=stop_check,
           )
           if judgment is None:
               # Python's checks passed; an unavailable judge must not block publication.
               candidates.append(_SectionCandidate(draft, attempt, errors=[]))
               _emit(on_progress, "write", "judge_unavailable",
                     page=page.title, section=index, attempt=attempt, error=judge_error)
               break
           missing = _judge_feedback(judgment)
           candidates.append(
               _SectionCandidate(draft, attempt, score=judgment.coverage_score, missing=missing)
           )
           _emit(
               on_progress, "write", "section_judged",
               page=page.title, section=index, attempt=attempt,
               score=judgment.coverage_score, missing=len(missing),
           )
           if not missing:
               break
           feedback = missing

       clean = [item for item in candidates if not item.errors]
       if clean:
           best = max(
               clean,
               key=lambda item: (
                   not item.missing,
                   item.score if item.score is not None else 0,
                   item.attempt,
               ),
           )
           return _SectionResult(
               markdown=best.markdown,
               attempts=len(candidates),
               score=best.score,
               missing=best.missing,
           )
       # Every attempt lost content. Publish the exact source instead and flag it.
       errors = candidates[-1].errors if candidates else list(feedback)
       _emit(on_progress, "write", "section_verbatim",
             page=page.title, section=index, error="; ".join(errors)[:300])
       return _SectionResult(
           markdown=_verbatim_section(lines, start, end, section_units),
           attempts=len(candidates),
           errors=errors,
           verbatim=True,
       )
   ```

6. Add `_write_intro`:

   ```python
   async def _write_intro(
       page: SeedPage,
       body: str,
       *,
       model: ModelPort,
       config: NeoConfig,
       task_dir: Path,
       stop_check: StopCheck,
   ) -> str:
       """One lead paragraph. Anything it cannot justify from the body is dropped."""

       if stop_check and stop_check():
           raise asyncio.CancelledError("page writing cancelled")
       prompt = intro_prompt(
           page_title=page.title,
           page_summary=page.summary,
           body=body,
           output_language=config.output_language,
       )
       write_text_atomic(task_dir / "intro-prompt.md", prompt.render())
       try:
           raw = await model.text(
               prompt.messages(), max_output_tokens=config.intro_max_output_tokens
           )
       except Exception as exc:  # noqa: BLE001 - the summary is an acceptable intro
           write_text_atomic(task_dir / "intro-error.txt", f"{type(exc).__name__}: {exc}\n")
           return page.summary
       text = PLACEHOLDER_RE.sub("", normalize_draft(raw))
       text = "\n".join(
           line for line in text.splitlines()
           if line.strip() and not line.startswith("#") and "](" not in line
       ).strip()
       if not text or code_tokens(text) - code_tokens(body):
           return page.summary
       write_text_atomic(task_dir / "intro.md", text + "\n")
       return text
   ```

7. Replace `_rewrite_page` entirely with:

   ```python
   async def _rewrite_page(
       page: SeedPage,
       *,
       pages: Sequence[SeedPage],
       lines: list[str],
       units: Sequence[ImageUnit],
       tokens: dict[int, set[str]],
       model: ModelPort,
       config: NeoConfig,
       work_root: Path,
       seed_root: Path,
       source_line_count: int,
       stop_check: StopCheck,
       on_progress: Progress,
   ) -> RewriteResult:
       if len(page.owner_ranges) != 1:
           raise PipelineError(f"page {page.number} must own one contiguous range")
       start, end = page.owner_ranges[0]
       page_units = _page_units(page, units)
       task_dir = clean_workdir(work_root / f"page-{page.number:03d}")

       evidence, _research = await _research_references(
           page, pages=pages, lines=lines, units=units, tokens=tokens, model=model,
           config=config, work_root=work_root, seed_root=seed_root,
           stop_check=stop_check, on_progress=on_progress,
       )
       facts = [fact for item in evidence for fact in item.facts]
       sections = split_sections(
           lines, start, end,
           target=config.section_target_lines, min_lines=config.section_min_lines,
       )
       buckets = assign_facts(facts, sections)

       drafts: list[str] = []
       scores: list[int] = []
       missing: list[str] = []
       verbatim: list[str] = []
       attempts = 0
       for index, ((s, e), section_facts) in enumerate(zip(sections, buckets), start=1):
           result = await _write_section(
               page, s, e, index=index, count=len(sections), facts=section_facts,
               lines=lines, units=units, model=model, config=config, task_dir=task_dir,
               stop_check=stop_check, on_progress=on_progress,
           )
           drafts.append(result.markdown.rstrip())
           attempts += result.attempts
           if result.score is not None:
               scores.append(result.score)
           missing.extend(f"原文 {s}-{e}行: {item}" for item in result.missing)
           if result.verbatim:
               verbatim.append(f"原文 {s}-{e}行: " + "; ".join(result.errors))

       body = "\n\n".join(drafts)
       intro = await _write_intro(
           page, body, model=model, config=config, task_dir=task_dir, stop_check=stop_check
       )
       markdown = f"# {page.title}\n\n{intro.rstrip()}\n\n{body}\n"
       markdown = link_titles(
           markdown,
           [(item.title, item.filename) for item in pages if item.number != page.number],
       )
       markdown += _nav_footer(page, pages)
       restored, unresolved = restore_images(markdown, page_units)
       if unresolved:
           raise PipelineError(f"page {page.number} has unresolved image placeholders: {unresolved}")
       page.reference_ranges = _reference_ranges_from_markdown(
           restored, page.owner_ranges, source_line_count
       )
       return RewriteResult(
           page=page,
           markdown=restored,
           attempts=attempts,
           judge_score=min(scores) if scores else None,
           missing_important_information=missing,
           verbatim_sections=verbatim,
       )
   ```

8. Replace `_rewrite_all` with:

   ```python
   async def _rewrite_all(
       pages: Sequence[SeedPage],
       *,
       lines: list[str],
       units: Sequence[ImageUnit],
       model: ModelPort,
       config: NeoConfig,
       work_root: Path,
       seed_root: Path,
       wiki_root: Path,
       state_root: Path,
       source_path: Path,
       source_snapshot_path: Path,
       source_sha256: str,
       source_line_count: int,
       stop_check: StopCheck,
       on_progress: Progress,
   ) -> list[RewriteResult]:
       semaphore = asyncio.Semaphore(max(1, config.rewrite_concurrency))
       tokens = {
           item.number: word_tokens(
               _prompt_safe(_slice_ranges(lines, item.owner_ranges), _page_units(item, units))
           )
           for item in pages
       }

       results: list[RewriteResult] = []
       pending: list[SeedPage] = []
       for page in pages:
           output_path = wiki_root / page.filename
           state_path = _page_state_path(state_root, page)
           resumed = (
               _resume_rewritten_page(
                   output_path, state_path, page,
                   rewrite_version=REWRITE_PROMPT_VERSION,
                   source_line_count=source_line_count,
               )
               if config.resume
               else None
           )
           if resumed is not None:
               results.append(resumed)
               _emit(on_progress, "rewrite", "page_resumed",
                     current=len(results), total=len(pages), page=page.title)
           else:
               output_path.unlink(missing_ok=True)
               state_path.unlink(missing_ok=True)
               for old in (work_root / f"page-{page.number:03d}", work_root / f"research-{page.number:03d}"):
                   if old.is_dir():
                       shutil.rmtree(old)
               pending.append(page)

       async def one(page: SeedPage) -> RewriteResult:
           async with semaphore:
               return await _rewrite_page(
                   page, pages=pages, lines=lines, units=units, tokens=tokens,
                   model=model, config=config, work_root=work_root, seed_root=seed_root,
                   source_line_count=source_line_count,
                   stop_check=stop_check, on_progress=on_progress,
               )

       for completed, task in enumerate(
           asyncio.as_completed([one(page) for page in pending]), start=len(results) + 1
       ):
           result = await task
           results.append(result)
           write_text_atomic(wiki_root / result.page.filename, result.markdown)
           _write_page_state(
               _page_state_path(state_root, result.page), result,
               rewrite_version=REWRITE_PROMPT_VERSION, pages=pages,
               source_path=source_path, source_snapshot_path=source_snapshot_path,
               source_sha256=source_sha256,
           )
           _emit(
               on_progress, "rewrite", "page_done",
               current=completed, total=len(pages), page=result.page.title,
               attempts=result.attempts, score=result.judge_score,
               verbatim_sections=len(result.verbatim_sections),
           )
       return sorted(results, key=lambda item: item.page.number)
   ```

9. Sidecar state. Replace the JSON body inside `_write_page_state` with:

   ```python
           {
               "number": result.page.number,
               "title": result.page.title,
               "filename": result.page.filename,
               "status": "rewritten",
               "rewrite_version": rewrite_version,
               "source_ranges": _ranges_json(result.page.owner_ranges),
               "reference_ranges": _ranges_json(result.page.reference_ranges),
               "provenance": _page_provenance(
                   result.page, pages,
                   source_path=source_path, source_snapshot_path=source_snapshot_path,
                   source_sha256=source_sha256,
               ),
               "content_sha256": sha256_text(result.markdown),
               "attempts": result.attempts,
               "judge_score": result.judge_score,
               "missing_important_information": result.missing_important_information,
               "verbatim_sections": result.verbatim_sections,
           }
   ```

   Replace `_resume_rewritten_page` with (no more frontmatter migration; the version bump
   already invalidates old runs):

   ```python
   def _resume_rewritten_page(
       output_path: Path,
       state_path: Path,
       page: SeedPage,
       *,
       rewrite_version: str,
       source_line_count: int,
   ) -> RewriteResult | None:
       """Reuse a page whose sidecar state still matches the file on disk."""

       if not output_path.exists() or not state_path.exists():
           return None
       try:
           markdown = output_path.read_text(encoding="utf-8")
           state = read_json(state_path)
           if (
               state.get("rewrite_version") != rewrite_version
               or state.get("filename") != page.filename
               or state.get("content_sha256") != sha256_text(markdown)
           ):
               return None
           page.reference_ranges = _valid_reference_ranges(
               state.get("reference_ranges", []), source_line_count
           )
           return RewriteResult(
               page=page,
               markdown=markdown,
               attempts=int(state.get("attempts", 0)),
               judge_score=state.get("judge_score"),
               missing_important_information=list(state.get("missing_important_information", [])),
               verbatim_sections=list(state.get("verbatim_sections", [])),
           )
       except (OSError, TypeError, ValueError):
           return None
   ```

10. Tests. In `tests/test_neo_chunking.py`, class `RewriteJudgeTests` is now empty apart
    from the reference tests you kept in WP-4; rename it `SectionWriteTests` and add:

    ```python
        @staticmethod
        def lines() -> list[str]:
            return [
                "# 対象API", "", "mpf_open は開く。", "", "```c", "int mpf_open(int filenum);", "```",
                "", "| 引数 | 意味 |", "|---|---|", "| filenum | 番号 |", "",
                "注意:", "E_TIMEOUT が返ることがある。", "続き。",
                "# 設定", "有効化が必要。", "設定の詳細。",
            ]

        @staticmethod
        def pages() -> list[pipeline.SeedPage]:
            return [
                pipeline.SeedPage(number=1, title="対象API", chapter="", summary="開く",
                                  owner_ranges=[(1, 15)], filename="001-api.md", page_id="page-001"),
                pipeline.SeedPage(number=2, title="設定", chapter="", summary="設定",
                                  owner_ranges=[(16, 18)], filename="002-settings.md", page_id="page-002"),
            ]

        async def test_lost_code_block_is_fed_back_and_final_page_is_lossless(self) -> None:
            prompts: list[str] = []

            async def behaviour(schema, messages):
                prompt = messages[-1].content
                if schema is ReferenceResearchResult:
                    return ReferenceResearchResult(useful_facts=[ReferenceFact(
                        description="有効化が必要。", reason="前提", source_start=17,
                        source_end=17, target_line=3)])
                if schema is PageJudgeResult:
                    return PageJudgeResult(coverage_score=100)
                if "導入文" in prompt:
                    return "このページは mpf_open の使い方を説明する。"
                prompts.append(prompt)
                if "前回の出力の不足" not in prompt:
                    return "## 対象API\n\nmpf_open は開く。設定の有効化が必要。（参照元: 原文 17-17行）\n\n| 引数 | 意味 |\n|---|---|\n| filenum | 番号 |\n\n## 注意\nE_TIMEOUT が返ることがある。続き。\n"
                return ("## 対象API\n\nmpf_open は開く。設定の有効化が必要。（参照元: 原文 17-17行）\n\n```c\nint mpf_open(int filenum);\n```\n\n"
                        "| 引数 | 意味 |\n|---|---|\n| filenum | 番号 |\n\n## 注意\nE_TIMEOUT が返ることがある。続き。\n")

            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = await pipeline._rewrite_page(
                    self.pages()[0], pages=self.pages(), lines=self.lines(), units=[],
                    tokens={1: set(), 2: set()}, model=FakeModel(behaviour),
                    config=pipeline.NeoConfig(write_attempts=3, section_target_lines=80, section_min_lines=1),
                    work_root=root, seed_root=root / "seeds", source_line_count=18,
                    stop_check=None, on_progress=None,
                )

            self.assertEqual(len(prompts), 2)
            self.assertIn("コードブロック", prompts[1])
            self.assertIn("int mpf_open(int filenum);", result.markdown)
            self.assertTrue(result.markdown.startswith("# 対象API\n\nこのページは"))
            self.assertIn("[設定](002-settings.md)の有効化が必要", result.markdown)
            self.assertIn("次のページ: [設定](002-settings.md)", result.markdown)
            self.assertEqual(result.verbatim_sections, [])
            self.assertEqual(result.page.reference_ranges, [(17, 17)])

        async def test_section_that_never_passes_is_published_verbatim_and_flagged(self) -> None:
            async def behaviour(schema, messages):
                if schema is ReferenceResearchResult:
                    return ReferenceResearchResult(no_useful_information_reason="なし")
                if schema is PageJudgeResult:
                    return PageJudgeResult(coverage_score=100)
                return "## 対象API\n\n何も書かない。\n"

            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = await pipeline._rewrite_page(
                    self.pages()[0], pages=self.pages(), lines=self.lines(), units=[],
                    tokens={1: set(), 2: set()}, model=FakeModel(behaviour),
                    config=pipeline.NeoConfig(write_attempts=2, section_min_lines=1),
                    work_root=root, seed_root=root / "seeds", source_line_count=18,
                    stop_check=None, on_progress=None,
                )

            self.assertEqual(len(result.verbatim_sections), 1)
            self.assertIn("mpf_open", result.verbatim_sections[0])
            self.assertIn("int mpf_open(int filenum);", result.markdown)
            self.assertIn("| filenum | 番号 |", result.markdown)

        async def test_judge_omissions_are_fed_back(self) -> None:
            judged = 0
            prompts: list[str] = []

            async def behaviour(schema, messages):
                nonlocal judged
                prompt = messages[-1].content
                if schema is ReferenceResearchResult:
                    return ReferenceResearchResult(no_useful_information_reason="なし")
                if schema is PageJudgeResult:
                    judged += 1
                    if judged == 1:
                        return PageJudgeResult(coverage_score=60, missing_important_information=[
                            ImportantOmission(description="タイムアウトの条件", source_start=14, source_end=14)])
                    return PageJudgeResult(coverage_score=100)
                if "導入文" in prompt:
                    return "導入。"
                prompts.append(prompt)
                return ("## 対象API\n\nmpf_open は開く。\n\n```c\nint mpf_open(int filenum);\n```\n\n"
                        "| 引数 | 意味 |\n|---|---|\n| filenum | 番号 |\n\n## 注意\nE_TIMEOUT が返ることがある。続き。\n")

            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = await pipeline._rewrite_page(
                    self.pages()[0], pages=self.pages(), lines=self.lines(), units=[],
                    tokens={1: set(), 2: set()}, model=FakeModel(behaviour),
                    config=pipeline.NeoConfig(write_attempts=3, section_min_lines=1),
                    work_root=root, seed_root=root / "seeds", source_line_count=18,
                    stop_check=None, on_progress=None,
                )

            self.assertEqual(len(prompts), 2)
            self.assertIn("タイムアウトの条件", prompts[1])
            self.assertEqual(result.judge_score, 100)
            self.assertEqual(result.missing_important_information, [])
    ```

    Add `ImportantOmission` to the `graph.neo.wire` import at the top of the test file.

    Why the fixture has no second heading inside page 1: with one section the fake writer
    is called exactly once per attempt, so `len(prompts)` counts attempts. Multi-section
    behaviour is covered by the end-to-end test in WP-6, where the source has headings.

    Also in `PublicationTests`, delete `test_rewritten_markdown_has_no_internal_frontmatter`,
    `test_legacy_frontmatter_is_migrated_to_sidecar_state`,
    `test_reference_paths_are_absolute_and_include_summaries`,
    `test_rewrite_references_are_copied_inside_the_task_directory`. Keep
    `test_cross_page_markers_are_collected_without_rejecting_the_page`,
    `test_dropped_image_placeholder_is_restored_once`,
    `test_dropped_image_is_reinserted_next_to_original_heading`,
    `test_provenance_maps_imported_ranges_to_their_wiki_pages` — they still pass.

    `run_pipeline` still calls `_prepare_agent` and `_link_all` at this point; that is
    fixed in WP-6. Nothing in the tests calls `run_pipeline`, so the suite can be green.

**Verify:**

```bash
cd llm-wiki-dist && .venv/bin/python -m unittest tests.test_neo_chunking tests.test_neo_page
```

Expected: OK. Then inspect one thing by hand: in the first new test, temporarily print
`result.markdown` and confirm the order is `# 対象API` → intro → `## 対象API` section →
`## 注意` … → `---` → nav line. Remove the print.

**Revert:** `git checkout -- graph/neo/pipeline.py graph/neo/config.py tests/test_neo_chunking.py`.

---

## WP-6 — Publish: `run_pipeline`, manifest, `_review.md`, CLI

**Goal:** the end-to-end driver uses the new writer, links are already inside pages, the
review file lists verbatim sections, and the CLI has no `--agent-backend`.

**Files:** `llm-wiki-dist/graph/neo/pipeline.py`, `llm-wiki-dist/graph/neo/__main__.py`.

**Steps:**

1. Delete from `pipeline.py`: `_MARKDOWN_LINK`, `_without_link_markup`,
   `_validate_additive_links`, `_stage_link_references`, `_link_page`, `_link_all`,
   `_prepare_agent`. Delete the import line `from .agent import AgentPort, clean_workdir, make_agent`
   and add `from .storage import clean_workdir` **after** moving `clean_workdir` (the last
   function of `agent.py`, 7 lines) to the end of `storage.py` verbatim, with
   `import shutil` added to `storage.py` (it is not there today).

   Also remove from the `.prompts` import list: `link_page_prompt`,
   `reference_enrichment_judge_prompt`, `simple_page_edit_prompt`,
   `wiki_plan_judge_prompt`, `wiki_page_plan_prompt`; add `intro_prompt`,
   `section_write_prompt`. Remove `WikiPlanJudgeResult` from the `.wire` import. Remove
   `LINK_PROMPT_VERSION` from the `.config` import.

2. In `_manifest`, replace the per-page dict with:

   ```python
               {
                   "number": page.number,
                   "title": page.title,
                   "chapter": page.chapter,
                   "filename": page.filename,
                   "owner_ranges": _ranges_json(page.owner_ranges),
                   "reference_ranges": _ranges_json(page.reference_ranges),
                   "provenance": _page_provenance(
                       page, pages,
                       source_path=source_path, source_snapshot_path=source_snapshot_path,
                       source_sha256=source_hash,
                   ),
                   "status": "rewritten",
                   "attempts": by_number[page.number].attempts,
                   "judge_score": by_number[page.number].judge_score,
                   "missing_important_information": by_number[page.number].missing_important_information,
                   "verbatim_sections": by_number[page.number].verbatim_sections,
               }
   ```

3. In `run_pipeline`:
   - in the `if not resumed_seed_plan:` cleanup loop, replace the glob
     `work_root.glob("rewrite-*-attempt-*")` with two globs `work_root.glob("page-*")`
     and `work_root.glob("research-*")` (same `rmtree` body);
   - delete `agent = _prepare_agent(config, model, work_root, slug)`;
   - in the `_rewrite_all(` call, replace `agent=agent, judge_model=model,` with `model=model,`;
   - delete the whole `fallback = [...]` / `if not fallback: results = await _link_all(...)`
     block;
   - replace the `if fallback: review = [...] … else: unlink` block with:

     ```python
       flagged = [item for item in results if item.verbatim_sections]
       if flagged:
           review = [
               "# Human review required",
               "",
               "These sections were published as the exact source text because every "
               "rewrite attempt lost content. Check them by hand:",
               "",
           ]
           for item in flagged:
               review.append(f"- `{item.page.filename}`")
               review.extend(f"  - {note}" for note in item.verbatim_sections)
           write_text_atomic(wiki_root / "_review.md", "\n".join(review) + "\n")
       else:
           (wiki_root / "_review.md").unlink(missing_ok=True)
     ```

   - in the `run.json` dict, replace the keys from `"rewritten"` to `"link_failures"`
     with:

     ```python
               "rewritten": len(pages),
               "review_pages": len(flagged),
               "verbatim_sections": sum(len(item.verbatim_sections) for item in results),
     ```

   - in the final `_emit(on_progress, "publish", "done", …)` replace
     `fallback_pages=len(fallback)` with `review_pages=len(flagged)`.

4. `__main__.py`: delete the `--agent-backend` argument and `agent_backend=args.agent_backend,`.
   Delete the `elif stage == "judge":`, `elif stage == "research":`,
   `elif stage == "rewrite" and step in {…}:` and `elif stage == "link" …:` branches;
   keep `elif stage == "rewrite" and step == "page_done":` and add before it:

   ```python
           elif stage == "write":
               pieces = [str(event.get("page", "")), f"section={event.get('section', '?')}"]
               for key in ("attempt", "score", "missing"):
                   if event.get(key) is not None:
                       pieces.append(f"{key}={event[key]}")
               detail = " ".join(pieces)
           elif stage == "research":
               detail = f"{event.get('page', '')} references={event.get('references') or event.get('reference', '')}"
   ```

5. `__init__.py`: it re-exports nothing from `agent`; only replace its docstring with
   `"""neo: overlapping observation, seed planning, and section-wise lossless rewriting."""`
   (keep the `from … import` lines and `__all__`).

**Verify:**

```bash
cd llm-wiki-dist
.venv/bin/python -m unittest tests.test_neo_chunking tests.test_neo_page
.venv/bin/python -c "import graph.neo.pipeline, graph.neo.__main__; print('imports ok')"
grep -n "_link_all\|_prepare_agent\|agent\." graph/neo/pipeline.py || echo clean
```

Then a **fake end-to-end run** (no network): put this in `tests/test_neo_chunking.py`,
class `SectionWriteTests`:

```python
    async def test_run_pipeline_end_to_end_with_fake_model(self) -> None:
        async def behaviour(schema, messages):
            prompt = messages[-1].content
            if schema is WindowInventory:
                return await whole_window(schema, messages)
            if schema is RegionalPlan:
                match = re.search(r"地域 \d+: 原文 (\d+)-(\d+)行", prompt)
                start, end = map(int, match.groups())
                return RegionalPlan(pages=[RegionalPage(title="全体", scope="all", source_start=start, source_end=end)])
            if schema is SemanticPlan:
                return SemanticPlan(plan="one page")
            if schema is SeedPlan:
                end = int(re.search(r"原文は1-(\d+)行である", prompt).group(1))
                return SeedPlan(pages=[SeedRange(title="全体", summary="all", source_start=1, source_end=end)])
            if schema is ReferenceResearchResult:
                return ReferenceResearchResult(no_useful_information_reason="なし")
            if schema is PageJudgeResult:
                return PageJudgeResult(coverage_score=100)
            if "導入文" in prompt:
                return "導入。"
            # Echo the numbered source without its line numbers: lossless by construction.
            body = prompt.split("--- 行番号付き原文（この節） ---\n", 1)[1]
            return "\n".join(line.split(": ", 1)[1] if ": " in line else line for line in body.splitlines())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "doc.md"
            source.write_text(fake_source(), encoding="utf-8")
            config = pipeline.NeoConfig(output_root=str(root / "out"), planner_attempts=1, map_attempts=1)
            run_root = await pipeline.run_pipeline(source, config=config, model=FakeModel(behaviour))

        wiki = run_root / "wiki"
        pages = sorted(p.name for p in wiki.glob("0*.md"))
        self.assertTrue(pages)
        self.assertTrue((wiki / "index.md").exists())
        self.assertFalse((wiki / "_review.md").exists())
        text = (wiki / pages[0]).read_text(encoding="utf-8")
        self.assertIn("def function_1(): ...", text)
        self.assertIn(base64_run(), text)          # image bytes restored
        self.assertNotIn("[[NEO-IMAGE:", text)     # no placeholder left
```

The two regexes are the same markers `HierarchicalPlanningTests` already relies on
(`地域 N: 原文 S-E行` in the regional prompt, `原文は1-N行である` in the compile prompt).
`import re` at the top of the test file if it is not there. Import `RegionalPlan`,
`RegionalPage`, `SemanticPlan`, `SeedPlan`, `SeedRange`, `WindowInventory` from
`graph.neo.wire` if they are not already imported. If the fake seed plan is rejected by
`validate_seed_plan`, read the error it feeds back (it is in the second prompt) and fix
the **fake**, not the pipeline.

Expected: all tests OK, `imports ok`, `clean`.

**Revert:** `git checkout -- graph/neo/ tests/test_neo_chunking.py`.

---

## WP-7 — Delete the agent code

**Goal:** C2 holds: nothing agent-shaped remains in `graph/neo/`.

**Files:** `llm-wiki-dist/graph/neo/agent.py` (delete), `config.py`, `prompts.py`,
`wire.py`, `tests/test_neo_chunking.py`.

**Steps:**

1. `git rm llm-wiki-dist/graph/neo/agent.py`.
2. `config.py`: delete `class HermesConfig`, `class PiConfig`, the fields
   `agent_backend`, `hermes`, `pi` from `NeoConfig`, the constant `LINK_PROMPT_VERSION`.
   If `Field` is now unused, drop it from the import.
3. `prompts.py`: delete `artifact_instruction`, `render_agent_prompt`,
   `reference_selection_prompt`, `wiki_page_plan_prompt`, `simple_page_edit_prompt`,
   `wiki_plan_judge_prompt`, `link_page_prompt`, `reference_enrichment_judge_prompt`.
   Remove `LINK_PROMPT_VERSION` from its `.config` import.
4. `wire.py`: delete `class ReferenceSelection`, `class WikiPlanJudgeResult`.
5. `tests/test_neo_chunking.py`: delete `class PiAgentTests`, the import
   `from graph.neo.agent import AgentReply, PiAgent`, and the tests
   `test_link_prompt_requires_inline_links_only`,
   `test_wiki_planner_reads_evidence_and_writes_a_concrete_plan`,
   `test_plan_judge_rejects_an_original_outline_copy` (and any test that still mentions
   a deleted name — `grep -n "link_page_prompt\|wiki_page_plan_prompt\|simple_page_edit_prompt\|_link_page\|_validate_additive_links\|_prepare_agent\|_page_index\|_stage_rewrite_references\|_render_page" tests/test_neo_chunking.py` must print nothing).
6. Update the module docstring at the top of `pipeline.py` to:

   ```python
   """The neo pipeline: deterministic partition, section-wise lossless rewriting.

   1. Overlapping 250-line windows are described without assigning ownership.
   2. Regional and document planners compile one exact sequential seed partition.
   3. Python picks references (adjacent + shared vocabulary); one structured
      compare call per reference collects facts to import.
   4. Python cuts each page into sections; the model rewrites one section at a
      time as plain Markdown; Python checks fences, tables, identifiers, images
      and imported facts survived before a judge looks for semantic omissions.
   5. Python writes the title, one model-written intro, links and navigation.

   There is no CLI agent and no tool-calling agent. Everything the model needs
   is inside the prompt; every decision about what survives is made in Python.
   """
   ```

**Verify:**

```bash
cd llm-wiki-dist
grep -rn "hermes\|PiConfig\|HermesConfig\|create_react_agent\|StructuredTool\|langgraph\|subprocess\|langchain_core.tools" graph/neo/ || echo "C2 clean"
.venv/bin/python -m unittest tests.test_neo_chunking tests.test_neo_page
for f in tests/test_*.py; do .venv/bin/python -m unittest "tests.$(basename ${f%.py})" || echo "FAILED $f"; done
```

Expected: `C2 clean`; neo tests OK; no `FAILED` line (other than any you noted in WP-0).

**Revert:** `git checkout -- graph/neo/ tests/ && git checkout HEAD -- graph/neo/agent.py`.

---

## WP-8 — Real-model smoke run (manual, no commit)

**Goal:** see the flow work once against `gemma-4-31B` on a real manual before calling it
done. No code changes expected; if something is wrong, report it, do not patch it in this WP.

**Steps:**

1. Take a real source (e.g. the Moove manual named in `PROMPT.md`, or any ≥ 2000-line
   Markdown with fences and images). Run:

   ```bash
   cd llm-wiki-dist
   .venv/bin/python -m graph.neo /path/to/manual.md --output /tmp/neo-smoke 2>&1 | tee /tmp/neo-smoke.log
   ```

2. While it runs, watch `/tmp/neo-smoke.log`. You should see `[neo] research: selected`,
   `[neo] write: section_judged … score=…`, occasional `section_retry`, and
   `[neo] rewrite: page_done`. You should **never** see a 900-second stall.
3. When it finishes, check:
   - `wiki/index.md` links every `NNN-*.md`;
   - pick three pages; for each, open `work/page-NNN/section-01-attempt-01-prompt.md` and
     the matching `.md` draft — confirm the prompt holds the numbered source and the draft
     is Markdown, not JSON;
   - `grep -c "参照元: 原文" wiki/*.md` is > 0 on at least some pages;
   - `grep -L "^# " wiki/0*.md` prints nothing (every page has its H1);
   - `grep -l "\[\[NEO-IMAGE" wiki/*.md` prints nothing;
   - `wiki/_review.md`: if it exists, read it; each entry names a section that fell back
     to verbatim source. Report the count and the most common error string.
4. Report: pages, total sections, sections that needed a retry, sections that fell back to
   verbatim, wall time, and the three pages you read with one sentence on their quality.

**Verify:** the report above. **Revert:** `rm -rf /tmp/neo-smoke`.

---

## Appendix A — Symbols removed vs added (grep checklist for the reviewer)

Removed from `graph/neo/` by the end of WP-7:

```
agent.py (whole file)         HermesConfig  PiConfig  agent_backend  LINK_PROMPT_VERSION
_run_reference_agent  _reference_finish_error  _prepare_agent  _link_all  _link_page
_validate_additive_links  _without_link_markup  _MARKDOWN_LINK  _stage_link_references
_stage_rewrite_references  _page_index  _reference_view  _render_page  _yaml_string
_strip_internal_frontmatter  _INTERNAL_FRONTMATTER  _judge_wiki_plan  _judge_candidate
_judge_reference_enrichment  _missing_research_markers  _researched_ranges  _JudgedCandidate
_REFERENCE_MARKER  reference_selection_prompt  wiki_page_plan_prompt  simple_page_edit_prompt
wiki_plan_judge_prompt  link_page_prompt  reference_enrichment_judge_prompt
artifact_instruction  render_agent_prompt  ReferenceSelection  WikiPlanJudgeResult
```

Added:

```
model.py: ModelPort.text, ChatModelPort.text
page.py: split_sections code_tokens word_tokens verbatim_blocks demote_h1 normalize_draft
         table_row_key check_section assign_facts link_titles REFERENCE_MARKER_RE PLACEHOLDER_RE
wire.py: ReferenceFact.target_line
prompts.py: section_write_prompt intro_prompt
pipeline.py: _select_references _facts_text _verbatim_section _nav_footer _write_section
             _write_intro _SectionCandidate _SectionResult
config.py: section_target_lines section_min_lines write_attempts write_max_output_tokens
           intro_max_output_tokens reference_candidates; REWRITE_PROMPT_VERSION bumped
storage.py: clean_workdir (moved)
tests/test_neo_page.py (new)
```

## Appendix B — Deliberate simplifications (each is a `ponytail:` ceiling, not an oversight)

- **Sections are in source order.** No cross-page or cross-section reordering. Add an
  `order` step only when a real document reads badly because of it.
- **Reference selection is lexical.** No LLM shortlist. Add `reference_selection_prompt`
  back as an *extra* candidate source only if the smoke run shows obviously related pages
  being missed.
- **The lossless token check covers identifiers and hex, not bare numbers.** Bare numbers
  (`256`, `3.2.1`) are too noisy. Add a units-aware number check when the judge misses a
  numeric loss in practice.
- **No enrichment judge.** The marker check plus the judge seeing the facts in its
  "original" covers it. Add a dedicated judge only if markers appear without the fact.
- **Links are exact-title matches + prev/next.** No identifier-owner linking. Add it when
  the API pages are found to be under-linked.
- **The intro is guarded, not judged.** It may only use identifiers the body already has;
  otherwise the seed summary is used. That is cheaper than a judge and cannot fabricate.

## Appendix C — Glossary

- **owner range** — the one contiguous `(start, end)` of source lines a page owns. All
  pages' owner ranges tile `1..N` (`_verify_ranges`).
- **section** — a sub-range of the owner range that the writer rewrites in one call.
- **placeholder** — `[[NEO-IMAGE:<id>]]`, what the model sees instead of base64;
  `restore_images` swaps the original bytes back at the end.
- **fact** — one `ReferenceFact` found by comparing a reference page with the target; it
  carries the reference's `source_start/end` and the target's `target_line`.
- **marker** — `（参照元: 原文 S-E行）`, the visible provenance the writer must put right
  after an imported fact; `REFERENCE_MARKER_RE` finds them.
- **verbatim fallback** — the section published as its exact source lines because every
  attempt failed `check_section`; always listed in `wiki/_review.md`.
