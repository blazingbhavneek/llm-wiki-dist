"""Cascade mode: route, verify, select evidence, run packed subagents, synthesize."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import unicodedata
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Event
from typing import Any, Callable

import markdown as md
from gateway import (JevQuestion, jev_cascade_profile_questions,
                     jev_cascade_section_question, jev_page_question)
from jev.types import JevQuestion as EngineQuestion, JevRequest, JevUnavailable
from models import AgentAnswer
from prompts import JEV_QUERY_REWRITE_PROMPT, PACKED_SUBAGENT_PROMPT, SYNTHESIS_PROMPT
from researcher import AgentStopped, IndexMap, _check_stop, sanitize_text

log = logging.getLogger("growi_search_cascade")


def _p(result: Any) -> float:
    if isinstance(result, (float, int)):
        return float(result)
    return float(getattr(result, "p_yes", 0.0))


def pack_evidence(evidence: list[dict[str, Any]], bins: int, context_tokens: int,
                  engine: Any = None) -> tuple[list[list[dict[str, Any]]], int]:
    """Greedily keep documents whole; split oversized documents by page."""
    bins = max(1, int(bins))
    budget = bins * context_tokens
    page_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in evidence:
        page_groups.setdefault((item["document"], item["page"].id), []).append(item)
    selected = []
    used_tokens = 0
    dropped = 0
    for page_group in sorted(page_groups.values(),
                             key=lambda group: max(x["p"] for x in group), reverse=True):
        cost = sum(item["tokens"] for item in page_group)
        if used_tokens + cost <= budget:
            selected.extend(page_group)
            used_tokens += cost
        else:
            dropped += len(page_group)
    documents: dict[str, list[dict[str, Any]]] = {}
    for item in selected:
        documents.setdefault(item["document"], []).append(item)
    ordered = sorted(documents.values(), key=lambda group: max(x["p"] for x in group), reverse=True)
    packed: list[list[dict[str, Any]]] = [[] for _ in range(bins)]
    used = [0] * bins
    for group in ordered:
        group.sort(key=lambda x: (-x["p"], x["page_order"], x["section_order"]))
        cost = sum(x["tokens"] for x in group)
        slots = [i for i in range(bins) if used[i] + cost <= context_tokens]
        if slots:
            slot = min(slots, key=lambda i: used[i])
            packed[slot].extend(group); used[slot] += cost
            continue
        pages: dict[str, list[dict[str, Any]]] = {}
        for item in group:
            pages.setdefault(item["page"].id, []).append(item)
        for page_group in pages.values():
            cost = sum(x["tokens"] for x in page_group)
            slots = [i for i in range(bins) if used[i] + cost <= context_tokens]
            if slots:
                slot = min(slots, key=lambda i: used[i])
                packed[slot].extend(page_group); used[slot] += cost
            else:
                dropped += len(page_group)
    for group in packed:
        group.sort(key=lambda x: (-x["p"], x["page_order"], x["section_order"]))
    return [group for group in packed if group], dropped


def _cache_path(session: Any, question: str) -> Path | None:
    mirror = getattr(session, "mirror", None)
    directory = getattr(mirror, "directory", None)
    if not directory or not getattr(mirror, "ready", False):
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", question).casefold().split())
    key = hashlib.sha1(f"{normalized}\0{mirror.version}".encode()).hexdigest()
    return Path(directory) / "answers" / (key + ".json")


def _read_cache(session: Any, question: str) -> AgentAnswer | None:
    if not getattr(session.settings, "answer_cache", False) or getattr(session, "has_overrides", False):
        return None
    mirror = getattr(session, "mirror", None)
    if mirror is None or not getattr(mirror, "ready", False):
        return None
    path = _cache_path(session, question)
    try:
        row = json.loads(path.read_text(encoding="utf-8")) if path else {}
        if row.get("version") != mirror.version:
            return None
        return AgentAnswer.model_validate(row["answer"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _rewrite(session: Any, question: str) -> str:
    state = session.index_map.snapshot()
    root = (session.settings.growi_root_path.rstrip("/") + "/" + session.settings.index_page_name).strip("/")
    cards = list(state.children.get(root, []))
    material_cards = list(cards)
    for card in cards:
        if getattr(card, "kind", "") == "folder":
            material_cards.extend(state.children.get(card.target.strip("/"), []))
    material = "\n".join(IndexMap.card_text(card) for card in material_cards[:100])
    if not material:
        material = "\n".join(IndexMap.card_text(card) for card in state.folders[:50])
    material = material[:20000]
    if not material:
        return question
    try:
        payload = json.dumps({"質問": question, "この Wiki の目次": material}, ensure_ascii=False)
        llm = getattr(session, "deterministic_llm", None) or session.llm
        result = llm.complete(JEV_QUERY_REWRITE_PROMPT, sanitize_text(payload))
        session._record_usage()
        return sanitize_text(result).replace("\n", " ").strip()[:400] or question
    except Exception as exc:  # noqa: BLE001
        log.info("cascade rewrite failed: %s", exc)
        return question


def _profile(session: Any, question: str) -> dict[str, Any]:
    answers = session.jev.score_many({"question": question}, [
        JevQuestion(key="list", text=listing) for listing in jev_cascade_profile_questions()[:1]
    ] + [JevQuestion(key="fact", text=jev_cascade_profile_questions()[1])])
    if len(answers) != 2:
        raise JevUnavailable("cascade shape questions returned the wrong number of answers")
    listing, fact = (_p(answer) >= .5 for answer in answers)
    st = session.settings
    if listing:
        profile = {"name": "list", "subagents": 20, "context_tokens": 96000, "max_docs": 100, "early_stop": False, "list": True}
    elif fact:
        profile = {"name": "fact", "subagents": 3, "context_tokens": 32000, "max_docs": 10, "early_stop": True, "list": False}
    else:
        subagents = getattr(session, "cascade_subagents_override", None)
        if subagents is None:
            subagents = getattr(st, "cascade_subagents", 15)
        profile = {"name": "default", "subagents": subagents,
                   "context_tokens": getattr(st, "cascade_context_tokens", 48000), "max_docs": getattr(st, "cascade_max_docs", 40),
                   "early_stop": True, "list": False}
    if getattr(session, "cascade_subagents_override", None) is not None:
        profile["subagents"] = session.cascade_subagents_override
    profile["subagents"] = min(32, max(1, int(profile["subagents"])))
    return profile


def _team_scope(path: str, root: str, state: Any = None) -> str:
    path = "/" + (path or "").strip("/")
    root = "/" + (root or "").strip("/")
    folders = []
    for folder in getattr(state, "folders", []) if state is not None else []:
        ref = "/" + folder.target.strip("/")
        if path == ref or path.startswith(ref + "/"):
            children = getattr(state, "children", {}).get(ref.strip("/"), [])
            if any(getattr(child, "kind", "") in {"document", ""} for child in children):
                folders.append(ref)
    if folders:
        return min(folders, key=len)
    root_ref = (root.rstrip("/") + "/00-目次").strip("/")
    root_children = getattr(state, "children", {}).get(root_ref, []) if state is not None else []
    if root != "/" and (path == root or path.startswith(root + "/")) and any(
        getattr(child, "kind", "") in {"document", ""} for child in root_children
    ):
        return root
    return ""


def _document_key(card: Any, path: str) -> str:
    return str(getattr(card, "doc_ref", "") or
               (path.rsplit("/", 1)[0] if "/" in path else path))


def _render_bin(group: list[dict[str, Any]], dropped: int,
                next_reads: list[tuple[Any, float]]) -> str:
    blocks = []
    for item in group:
        blocks.append(f"### {item['page'].title} › {item['section'].heading} "
                      f"(page_id: {item['page'].id}, path: {item['page'].path}, p={item['p']:.2f})\n{item['section'].body}")
    if dropped:
        blocks.append(f"関連度の低い節を {dropped} 件省略しました。")
    if next_reads:
        blocks.append("次に読む候補:\n" + "\n".join(
            f"- {page.id} : {page.title} (p={score:.2f})" for page, score in next_reads[:10]))
    return "\n\n".join(blocks)


def _run_cascade(session: Any, question: str, emit: Callable,
                 stop_event: Event | None = None) -> AgentAnswer:
    cached = _read_cache(session, question)
    if cached:
        return cached
    profile = _profile(session, question)
    rewritten = _rewrite(session, question)
    try:
        hits = session.walker.collect(rewritten, start_ref=None, max_docs=profile["max_docs"],
                                      page_threshold=session.settings.walker_threshold,
                                      stop_event=stop_event, emit=emit)
    except Exception as exc:  # noqa: BLE001
        raise JevUnavailable(f"cascade route failed: {exc}") from exc
    state = session.index_map.snapshot()
    cards = {card.target.strip("/"): card for card in state.cards}
    grouped: dict[str, list[Any]] = {}
    for hit in hits:
        card = cards.get((hit.path or hit.page_id).strip("/"))
        document = _document_key(card, hit.path) if card else hit.path.rsplit("/", 1)[0]
        grouped.setdefault(document or "(unknown)", []).append(hit)
    # Entity-definer rescue can cross a folder the walker pruned, but stays inside
    # the team root represented by this IndexMap.
    rescued = set()
    routed_refs = {(hit.path or hit.page_id).strip("/") for hit in hits}
    rescue_queue = [(hit, cards.get((hit.path or hit.page_id).strip("/")), hit.path, 0)
                    for hit in hits]
    while rescue_queue:
        origin, source_card, source_path, depth = rescue_queue.pop(0)
        if depth >= 2 or source_card is None:
            continue
        for entity in getattr(source_card, "entities", []):
            for definer in session.index_map.definers_for(entity):
                ref = definer.target.strip("/")
                root_path = session.settings.growi_root_path
                definer_scope = _team_scope(definer.target, root_path, state)
                source_scope = _team_scope(source_path, root_path, state)
                if not definer_scope or not source_scope or definer_scope != source_scope:
                    continue
                if ref in rescued or ref in routed_refs:
                    continue
                rescued.add(ref)
                path_like = not (len(ref) == 24 and all(c in "0123456789abcdefABCDEF" for c in ref))
                rescue = type(origin)(page_id="" if path_like else ref,
                                      path=definer.target if path_like else "",
                                      title=definer.title, summary=definer.summary,
                                      p=1.0, trail=[])
                doc = _document_key(definer, definer.target)
                grouped.setdefault(doc, []).append(rescue)
                emit({"type": "route", "mode": "cascade", "stage": "rescue",
                      "kept": True, "rescued": True, "document": doc,
                      "node": {"id": rescue.page_id, "title": rescue.title}})
                next_card = cards.get(ref) or definer
                rescue_queue.append((rescue, next_card, source_path, depth + 1))
    reserve = getattr(session, "_reserve_cascade_pages", None)
    if callable(reserve):
        reserve(ref for doc_hits in grouped.values() for hit in doc_hits
                for ref in (hit.page_id, hit.path) if ref)
    if not grouped:
        raise JevUnavailable("cascade found no routed pages")

    def verify(document: str, doc_hits: list[Any]):
        verified = []
        checked = []
        for hit in doc_hits:
            _check_stop(stop_event)
            page = session._fetch_page(page_id=hit.page_id or None, path=hit.path or None)
            if page is None:
                continue
            probability, _ = session._jev_score_body(rewritten, document, page, [], stop_event)
            checked.append((page, probability))
            if probability >= session.settings.jev_seed_threshold:
                verified.append((page, probability))
        return document, verified, checked

    workers = min(max(1, session.settings.subagent_concurrency), len(grouped))
    reports: list[str] = []
    cited: list[str] = []
    seeds: list[tuple[str, Any, float]] = []
    pending: dict[Any, tuple[int, list[dict[str, Any]]]] = {}
    next_agent = 0
    runner = getattr(session, "_run_packed_subagent", None)
    if not callable(runner):
        raise JevUnavailable("packed subagent runner is not wired")
    claim = getattr(session, "_claim_cascade_pages", None)
    deferred_evidence: list[dict[str, Any]] = []
    early_started = False

    def schedule_packed(pool, packed, dropped) -> None:
        nonlocal next_agent
        tasks = []
        for group in packed:
            index = next_agent + 1
            next_agent = index
            next_reads = []
            page_refs = {item["page"].path.strip("/") for item in group}
            candidates = {}
            for item in group:
                for link in md.extract_links(item["page"].body):
                    target = md.resolve_target(item["page"].path, link.raw_target,
                                               session.settings.growi_root_path)
                    if target and target.path.strip("/") not in page_refs:
                        card = cards.get(target.path.strip("/"))
                        if card:
                            candidates[card.target] = card
            if candidates:
                request_list = [JevRequest({"card": {"title": card.title, "summary": card.summary,
                                                      "keywords": card.keywords, "entities": card.entities,
                                                      "path": card.target}}, EngineQuestion(
                    text=jev_page_question(rewritten), key=card.target)) for card in candidates.values()]
                for card, result in zip(candidates.values(), session.jev.decide_batch(request_list)):
                    next_reads.append((IndexMap.card_page(card), _p(result)))
                next_reads.sort(key=lambda pair: pair[1], reverse=True)
            prompt = (f"質問: {question}\n\n{PACKED_SUBAGENT_PROMPT}\n\n"
                      f"担当する証拠:\n{_render_bin(group, dropped, next_reads)}")
            if callable(claim):
                claim(index, prompt)
            emit({"type": "subagent_start", "agent": index,
                  "node": {"id": group[0]["page"].id, "title": group[0]["page"].title}})
            tasks.append((index, group, prompt))
        for index, group, prompt in tasks:
            pending[pool.submit(runner, index, prompt, rewritten, emit, stop_event)] = (index, group)

    def submit_document_bins(pool, document: str, doc_seeds: list[tuple[Any, float]]) -> None:
        nonlocal early_started
        evidence = []
        for page_order, (page, _score) in enumerate(doc_seeds):
            sections = md.split_sections(page.body, max_chars=3000)
            qtext = jev_cascade_section_question(rewritten, profile["list"])
            requests = [JevRequest({"page": {"title": page.title, "path": page.path,
                                             "heading": section.heading, "text": section.body}},
                                   EngineQuestion(text=qtext, key=f"{page.id}:{i}"))
                        for i, section in enumerate(sections)]
            results = session.jev.decide_batch(requests) if requests else []
            scores = [(section, _p(result), i) for i, (section, result) in enumerate(zip(sections, results))]
            kept = [row for row in scores if row[1] >= getattr(session.settings, "cascade_section_threshold", .3)]
            if not kept and scores:
                kept = [max(scores, key=lambda row: row[1])]
            if not hasattr(session, "_cascade_sections"):
                session._cascade_sections = {}
            session._cascade_sections[(page.id, page.revision_id, rewritten)] = [item[0] for item in kept]
            for section, probability, section_index in kept:
                evidence.append({"document": document, "page": page, "section": section,
                                 "p": probability, "page_order": page_order,
                                 "section_order": section_index,
                                 "tokens": max(1, session.jev.count_tokens(section.body))})
        if not evidence:
            return
        if early_started or profile["subagents"] <= 1:
            deferred_evidence.extend(evidence)
            return
        packed, dropped = pack_evidence(evidence, 1, profile["context_tokens"])
        selected = {(item["page"].id, item["section_order"]) for group in packed for item in group}
        deferred_evidence.extend(item for item in evidence
                                 if (item["page"].id, item["section_order"]) not in selected)
        if packed:
            early_started = True
            schedule_packed(pool, packed, 0)

    stopped_early = False

    def collect_report(future) -> None:
        """Record one finished bin; Jev's "reports suffice" check cancels queued bins."""
        nonlocal stopped_early
        index, _group = pending.pop(future)
        if future.cancelled():
            return
        try:
            report = future.result()
        except AgentStopped:
            raise
        except Exception as exc:  # noqa: BLE001 - one failed bin must not discard the others
            log.info("cascade subagent %s failed: %s", index, exc)
            return
        text = str(report.get("answer", "") if isinstance(report, dict) else report)
        ids = report.get("cited", []) if isinstance(report, dict) else []
        reports.append(text); cited.extend(map(str, ids))
        emit({"type": "subagent_done", "agent": index, "cited": ids, "report": text})
        if profile["early_stop"] and not stopped_early:
            sufficient = session.jev.score_many(
                {"question": question, "reports": reports},
                [JevQuestion(text="これらの報告だけで、質問に完全に答えられますか？\n選択肢: はい / いいえ", key="suffices")])[0]
            if _p(sufficient) >= getattr(session.settings, "cascade_early_stop", .9):
                stopped_early = True
                for queued in pending:
                    queued.cancel()
                emit({"type": "cascade_early_stop"})

    with ThreadPoolExecutor(max_workers=max(1, session.settings.subagent_concurrency),
                            thread_name_prefix="cascade-agent") as pool:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cascade-verify") as verify_pool:
            verify_futures = {verify_pool.submit(verify, doc, pages): doc for doc, pages in grouped.items()}
            while verify_futures or pending:
                done, _ = wait([*verify_futures, *pending], return_when=FIRST_COMPLETED)
                for future in done:
                    if future in verify_futures:
                        document, doc_seeds, checked = future.result()
                        del verify_futures[future]
                        seeds.extend((document, page, score) for page, score in doc_seeds)
                        emit({"type": "cascade_doc_done", "document": document, "seeds": len(doc_seeds)})
                        for page, probability in checked:
                            emit({"type": "jev_gate", "stage": "full",
                                  "status": "confirmed" if probability >= session.settings.jev_seed_threshold else "pruned",
                                  "node": {"id": page.id, "title": page.title},
                                  "document": document, "probability": round(probability, 4),
                                  "threshold": session.settings.jev_seed_threshold, "chunk_count": 1})
                        if not stopped_early:
                            submit_document_bins(pool, document, doc_seeds)
                        continue
                    if future in pending:
                        collect_report(future)
        if not stopped_early and deferred_evidence and next_agent < profile["subagents"]:
            packed, dropped = pack_evidence(
                deferred_evidence, profile["subagents"] - next_agent, profile["context_tokens"])
            schedule_packed(pool, packed, dropped)
            while pending:
                done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for future in done:
                    collect_report(future)
        if not seeds:
            raise JevUnavailable("cascade found no verified seeds")
        if not next_agent:
            raise JevUnavailable("cascade found no selected sections")
    seed_ids = [page.id for _doc, page, _score in seeds]
    seed_evidence = []
    for _document, page, score in seeds:
        selected = getattr(session, "_cascade_sections", {}).get((page.id, page.revision_id, rewritten), [])
        seed_evidence.append({"id": page.id, "title": page.title, "p": score,
                              "sections": [{"heading": section.heading, "text": section.body}
                                           for section in selected]})
    payload = json.dumps({"question": question, "seeds": seed_evidence, "reports": reports}, ensure_ascii=False)
    deltas = []
    answer = session.llm.stream(SYNTHESIS_PROMPT, payload,
                                lambda delta: (deltas.append(delta), emit({"type": "answer_delta", "text": delta})))
    session._record_usage()
    answer = sanitize_text(answer or "".join(deltas))
    known = set(seed_ids) | set(cited)
    citation_block = answer.split("引用:", 1)[-1] if "引用:" in answer else ""
    answer_ids = re.findall(r"^\s*([0-9a-fA-F]{24})\s*:", citation_block, flags=re.M)
    cited = [page_id for page_id in dict.fromkeys(answer_ids) if page_id in known]
    if "引用:" in answer:
        answer_head = answer.split("引用:", 1)[0].rstrip()
        allowed_lines = [line for line in citation_block.splitlines()
                         if (match := re.match(r"^\s*([0-9a-fA-F]{24})\s*:", line))
                         and match.group(1) in known]
        answer = answer_head + "\n\n引用:" + ("\n" + "\n".join(allowed_lines) if allowed_lines else "")
    result = AgentAnswer(question=question, answer=answer, cited_node_ids=cited, steps=next_agent)
    if (getattr(session.settings, "answer_cache", False)
            and getattr(session.mirror, "ready", False)
            and not getattr(session, "has_overrides", False)):
        path = _cache_path(session, question)
        if path:
            data = json.dumps({"version": session.mirror.version, "answer": result.model_dump()},
                              ensure_ascii=False).encode("utf-8")
            session.mirror._atomic(path, data)
    return result


def run_cascade(session: Any, question: str, emit: Callable,
                stop_event: Event | None = None) -> AgentAnswer:
    """Synchronous worker entry point; model failures route to the ES fallback."""
    try:
        return _run_cascade(session, question, emit, stop_event)
    except (AgentStopped, JevUnavailable):
        raise
    except Exception as exc:  # noqa: BLE001 - classifier/LLM failures use exhaustive routing
        raise JevUnavailable(f"cascade failed: {exc}") from exc


__all__ = ["pack_evidence", "run_cascade"]
