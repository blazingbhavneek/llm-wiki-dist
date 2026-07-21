# Non-Trivial Knowledge-Gap Detection with Agents

Research date: 2026-07-21

## Problem definition

The goal is to extend `llm-wiki` beyond answering user-supplied questions. Given a large document corpus and its knowledge graph, the system should identify important unresolved structure and formulate useful questions about it.

This is not ordinary missing-sentence detection. The system should find gaps such as:

- a missing link between important claims or concepts;
- a generalization that is documented only for a narrow scope;
- conflicting descriptions that are not reconciled;
- an undocumented cross-module contract or dependency;
- a missing boundary, threshold, interaction, or tradeoff;
- a central workflow whose lifecycle, ownership, or version semantics are unclear;
- a claim or relationship whose provenance is insufficient.

The system must distinguish “not documented in this corpus” from “unknown in the world.” It should not claim that something does not exist merely because retrieval did not find it.

## Main finding

No single recent paper cleanly combines all of the desired properties: corpus-internal gap detection, non-trivial question formation, multi-agent reasoning, very large corpora, and arbitrary technical documentation. The best design is a combination of recent work on implicit gap inference, multi-agent limitation analysis, auditable research-question formation, graph-based literature discovery, and large-corpus synthesis.

## Papers, ordered by relevance and proximity to July 2026

### FirstResearch: Auditable Question Formation for LLM Scientific Discovery Agents

Yufeng Wang, arXiv, submitted 2026-07-06.

https://arxiv.org/abs/2607.05682

This is the strongest reference for preventing shallow or generic questions. It introduces a structured Research Question Certificate containing:

- primitive definitions and assumptions;
- a mechanism model;
- a tension or contradiction;
- a research question and falsifiable hypothesis;
- a minimal decisive test;
- expected observations;
- a failure-update rule.

It also uses a novelty-boundary gate to repair questions toward thresholds, interactions, tradeoffs, and failure regimes. The reported evaluation is preliminary: ten LLM-agent topics, prompt-level baselines, and LLM judges rather than domain experts. Its certificate idea is nevertheless directly applicable to `llm-wiki`.

### Generating Literature-Driven Scientific Theories at Scale

Peter Jansen, Peter Clark, Doug Downey, and Daniel S. Weld, ACL 2026.

https://aclanthology.org/2026.acl-long.669/

This is not a multi-agent gap detector, but it is the strongest recent evidence for corpus-scale synthesis. The work uses 13,700 source papers to synthesize 2,900 theories and evaluates whether the resulting theories predict findings in 4,600 subsequently published papers.

The useful lesson is that corpus-level discovery should eventually be evaluated against later evidence or downstream outcomes, not only against LLM-judge plausibility.

### PROPER Agents: Proactivity Driven Personalized Agents for Advancing Knowledge Gap Navigation

Kirandeep Kaur et al., Findings of ACL 2026.

https://aclanthology.org/2026.findings-acl.2082/

PROPER separates gap discovery from response generation. Its Dimension Generating Agent identifies explicit and implicit task dimensions, and a calibrated activation/reranking layer chooses only relevant, timely, non-redundant dimensions.

This is mainly about user-specific knowledge gaps rather than gaps in a document corpus, so it is adjacent rather than a direct solution. Its selective-activation idea is useful for avoiding a flood of unnecessary questions.

### Multi-Agent LLMs for Generating Research Limitations (LimAgents)

Ibrahim Al Azher, Zhishuai Guo, and Hamed Alhoori, arXiv, submitted 2025-12-30 and revised 2026-03-16.

https://arxiv.org/abs/2601.11578

This is the closest practical reference for a small-model multi-agent architecture. It uses specialized agents for:

- explicit limitation extraction;
- implicit methodological-gap analysis;
- external peer-review analysis;
- citation-network analysis;
- judging and feedback;
- final merging and deduplication.

The system was built around 2,700 NeurIPS papers and 51,300 limitation statements. It uses cited and citing papers through hybrid BM25/FAISS retrieval and an LLM reranker. A focused three-agent Llama 3 8B configuration improved over zero-shot generation, while GPT-4o mini benefited from adding citation-aware retrieval.

The important lesson is to use small models for bounded specialist tasks and reserve stronger models for adjudication and synthesis.

### GAPMAP: Mapping Scientific Knowledge Gaps in Biomedical Literature Using Large Language Models

Nourah M. Salem et al., arXiv, submitted 2025-10-29; NeurIPS 2025 AI-for-Science poster.

https://arxiv.org/abs/2510.25055

GAPMAP distinguishes:

- explicit gaps, directly signaled by phrases such as “remains unknown” or “further research is needed”;
- implicit gaps, inferred from context rather than stated directly.

Its implicit categories include missing links in a chain of claims, generalization gaps caused by narrow scope, and conflicting findings without reconciliation. The proposed TABI method uses a Toulmin-style Claim–Grounds–Warrant structure and buckets candidate conclusions for validation.

The work evaluates nearly 1,500 documents and tests models ranging from Llama 3.1 8B and Gemma 2 9B to larger closed models. It is biomedical-focused, but its implicit-gap representation transfers well to technical documentation.

### Agentic Workflows for Gap-Aware Literature Reviews

Movina Moses et al., AGU 2025 conference paper, 2025-12-15.

https://research.ibm.com/publications/agentic-workflows-for-gap-aware-literature-reviews

This is conceptually very close to the requested direction. It combines:

- structured multi-agent synthesis;
- topic-specific knowledge graphs;
- graph traversal;
- contrastive retrieval;
- perspective-guided questioning;
- gap detection for reasoning, coverage, and evidence.

The public source is an abstract-level publication record, so it should be treated as architectural inspiration rather than strong implementation evidence.

### HypER: Literature-grounded Hypothesis Generation and Distillation with Provenance

Rosni Vasu et al., EMNLP 2025.

https://aclanthology.org/2025.emnlp-main.1292/

HypER trains a small language model for literature-guided reasoning and evidence-based hypothesis generation. It trains the model to distinguish valid and invalid reasoning chains in the presence of controlled distractions. The paper reports a 22% absolute F1 improvement for reasoning-chain discrimination and better evidence-grounded hypotheses than its base model.

This supports using smaller models as evidence and reasoning auditors, rather than asking one large model to perform every stage.

### ResearchBench: Benchmarking LLMs in Scientific Discovery via Inspiration-Based Task Decomposition

Yujie Liu et al., ACL 2026 Findings; arXiv version revised 2026-04-20.

https://arxiv.org/abs/2503.21248

ResearchBench decomposes scientific discovery into inspiration retrieval, hypothesis composition, and hypothesis ranking across twelve disciplines and recent papers. It is primarily an evaluation benchmark, not a gap detector, but it provides a useful way to evaluate whether generated questions are grounded, novel, and well-ranked instead of merely fluent.

### Enriched Knowledge Representation in Biological Fields: A Case Study of Literature-Based Discovery in Alzheimer’s Disease

Journal of Biomedical Semantics, 2025.

https://link.springer.com/article/10.1186/s13326-025-00328-3

This paper warns that simple pairwise knowledge graphs can create oversimplified or spurious discoveries. In its analysis, only about 20% of discovery statements were perfectly represented by pairwise relations alone; many required nested relations or higher-order structures.

This matters for `llm-wiki`: a graph containing only entity-to-entity edges may not represent the conditions, multi-party interactions, or nested processes needed to detect meaningful gaps.

## Anti-triviality requirements

A candidate question should be rejected unless it passes most of these tests:

1. **Multiple grounded premises**: it should arise from at least two relevant nodes, claims, sections, or graph paths rather than one obvious sentence.
2. **Explicit missing structure**: identify the missing relation, boundary, comparison, condition, mechanism, or provenance.
3. **Centrality or impact**: the gap must affect an important workflow, invariant, dependency, decision, or cluster—not an arbitrary edge node.
4. **Non-obvious resolution**: answering it should require synthesis, cross-document comparison, or additional evidence, not one trivial lookup.
5. **Corpus novelty**: targeted retrieval must fail to find an adequate answer under alternate wording and neighboring concepts.
6. **Mechanism or boundary**: prefer contradictions, scope transitions, interactions, thresholds, tradeoffs, version differences, and cross-component contracts.
7. **Resolution test**: define what evidence would resolve or invalidate the candidate gap.
8. **Question usefulness**: the answer should change understanding, implementation, maintenance, or decision-making.

For example, “What happens if `open_file()` fails?” is usually too generic. A stronger candidate would concern an undocumented cross-API invariant such as ownership, closing responsibility, encoding state, error propagation, concurrency, or version compatibility—provided the graph shows that multiple documented components depend on the relationship.

## Proposed architecture for `llm-wiki`

### 1. Offline candidate generation

Do not run an expensive agent over every node. Use graph and index statistics to find candidate regions:

- high-centrality concepts with incomplete relation types;
- multi-hop paths with an important missing link;
- clusters with strong semantic proximity but weak explanatory connections;
- conflicting claims or incompatible metadata;
- workflows with missing transitions, preconditions, outputs, or ownership;
- concepts mentioned across documents but never compared or scoped;
- claims with weak or absent provenance.

### 2. Specialist agents

Use small models for bounded passes:

- **Coverage agent**: compare extracted entities and relations against expected document/workflow dimensions.
- **Conflict agent**: locate incompatible claims, versions, conditions, or terminology.
- **Bridge agent**: examine multi-hop paths and nearby clusters for missing explanations.
- **Boundary agent**: find scope, version, threshold, interaction, and tradeoff gaps.
- **Evidence agent**: verify that a proposed gap is supported by actual source passages.

### 3. Targeted validation loop

Every candidate should trigger focused searches such as:

- Is this already answered under another term?
- Is the apparent gap only caused by bad chunking or entity resolution?
- Does a neighboring document provide the missing relation?
- Is this a real contradiction or merely a difference in scope/version?
- Does the candidate depend on an unsupported assumption?

Only unresolved candidates should reach the expensive judge.

### 4. Certificate and adjudication

Represent each surviving candidate as a structured gap certificate:

```text
Grounded premises
Missing structure
Why the gap matters
Candidate question
Corpus search performed
Evidence that the answer is absent or incomplete
Confidence
Impact
Novelty within the corpus
Resolution or falsification test
```

The final judge should reject generic, one-hop, common-sense, redundant, or low-impact questions and deduplicate semantically similar candidates.

### 5. Ranking

Rank candidates using a mixture of deterministic and model-based signals:

```text
priority = impact
         * unresolvedness
         * structural salience
         * cross-document support
         * non-triviality
         * actionability
         * diversity
```

The exact formula should be calibrated against human judgments from the target documentation domain.

## Important scope limitation

Scientific literature papers often use domain-specific categories such as study design, sample size, statistical power, and generalizability. These should not be copied literally into library documentation. For software material, the corresponding high-value categories are behavioral contracts, lifecycle/state transitions, version boundaries, concurrency, security, performance, module interactions, migration effects, and end-to-end workflows.

The system should report “not established in the indexed material” rather than “unknown” unless the corpus and external comparison support the stronger claim.

## Recommended reading order

1. GAPMAP — gap taxonomy and implicit-gap inference.
2. LimAgents — small-model multi-agent decomposition and critique.
3. FirstResearch — certificate and anti-generic question gate.
4. Agentic Workflows for Gap-Aware Literature Reviews — graph traversal and contrastive retrieval architecture.
5. Enriched Knowledge Representation for Literature-Based Discovery — limitations of pairwise graphs.
6. Generating Literature-Driven Scientific Theories at Scale — large-corpus evaluation against later evidence.
7. HypER — small-model evidence-grounded reasoning.

## What is implemented in `proto.py`

`proto.py` is a prototype of the architecture above, run against a single seed chunk rather than the whole corpus. Its regions map onto the section numbers of this document: Stage 0 → §1, Stage 1 → §2, Stage 2 → §3, Stage 3 → §4, Stage 4 → anti-triviality requirements, Stage 5 → §5.

Four of the nine papers contributed implemented mechanisms. The rest are recorded below with the reason they were not implemented, so the omissions stay deliberate rather than accidental.

### Implemented

**GAPMAP — gap taxonomy and explicit/implicit distinction**

Implemented as the `GapKind` literal in the *Taxonomy and schemas* region, retargeted from biomedical to software categories per the scope limitation above: `missing_link`, `narrow_generalization`, `unreconciled_conflict`, `undocumented_contract`, `missing_boundary`, `unclear_lifecycle`, `weak_provenance`. Every candidate must select one, so the gap type is a typed field rather than free prose.

The explicit/implicit split is the `is_explicit` flag on `GapCandidate` and `GapCertificate`. A specialist may set it only when the document itself signals the gap (「未処理」「別途規定」and similar), matching GAPMAP's explicit-signal definition; everything else is treated as an inferred implicit gap.

Not implemented: the TABI Claim–Grounds–Warrant structure and its bucketing procedure. `GroundedPremise` captures claim plus source node, which is roughly Claim–Grounds, but there is no separate warrant field and no bucketing step.

**LimAgents — small-model specialist decomposition**

Implemented as the four lenses in `SPECIALIST_LENSES` and the *Stage 1* region: coverage, conflict, bridge, boundary. Each lens runs as its own bounded call over an 8-node batch (`SPECIALIST_NODE_BUDGET`), all batches and lenses dispatched in parallel. No call ever receives the whole region or a general "find all the gaps" instruction, which is the paper's central operational claim about small models.

The paper's division between cheap specialists and a stronger adjudicator is preserved in shape: specialists and the stage 2 auditor are single structured calls, whereas stage 3 invokes the full multi-subagent `ask()` agent.

Not implemented: LimAgents' external peer-review agent and citation-network agent, both of which assume a citation graph that documentation corpora do not have. Also not implemented: a merge/dedup agent — `run_all_specialists` deduplicates on exact question string only, so near-duplicate phrasings from different lenses still survive.

**FirstResearch — certificate and anti-generic gate**

The certificate is the `GapCertificate` model, and the report writer emits it field by field. Field correspondence to the paper's Research Question Certificate:

| FirstResearch field | `GapCertificate` field |
|---|---|
| primitive definitions / assumptions | `grounded_premises` (node id, claim, quote) |
| tension or contradiction | `missing_structure` |
| research question | `question` |
| minimal decisive test | `resolution_test` |
| expected observations | `evidence_of_absence` |

The novelty-boundary gate is the `gate()` function plus `TrivialityJudgement`. All eight anti-triviality tests from this document are adjudicated as individual booleans and written into the audit JSON, so a rejection is inspectable rather than a silent threshold. Tests 1, 2 and 7 are non-waivable preconditions; the remainder must reach 6 of 8 to satisfy "most of these tests". Test 1 additionally has a deterministic pre-check in `adjudicate()`, which skips the expensive adversary call for candidates that structurally cannot pass.

Not implemented: the paper's mechanism model, falsifiable hypothesis, and failure-update rule. `resolution_test` covers what evidence would settle the question, but there is no hypothesis to falsify and no rule for updating the system when a candidate is refuted. Also not implemented: question *repair* — the gate only rejects, where FirstResearch repairs a question toward thresholds and interactions and retries.

**HypER — verification as a separate auditor pass**

Implemented as the separation between generation and verification. The specialist that proposes a candidate never judges it. Stage 2 (`validate_candidate`) is a distinct cheap auditor over freshly retrieved evidence, and stage 3 is a separate adversarial agent. This follows the paper's finding that a small model used as a dedicated reasoning auditor outperforms asking one model to both generate and self-verify.

The stage 3 adversary prompt encodes the paper's distractor-resistance idea in prompt form: chaining several passages into an inference explicitly does not count as coverage, only a decisive direct statement does. This closes the failure mode where a fluent synthesized answer reads as though the gap were already documented.

Not implemented: HypER's actual contribution, which is *training* a small model on valid versus invalid reasoning chains. `proto.py` uses an off-the-shelf model with prompt constraints only, so there is no learned discrimination between sound and unsound chains.

### Partially implemented

**Agentic Workflows for Gap-Aware Literature Reviews**

Two of its six listed components appear: graph traversal (stage 0 expands one hop through real edges via `follow_link`) and perspective-guided questioning (the four lenses). Contrastive retrieval is not implemented — stage 2 searches alternate phrasings, but does not contrast retrieved sets against each other. The source is abstract-level, so there was no method to implement more faithfully.

### Not implemented

**PROPER Agents.** Its selective-activation layer is the useful idea, and stage 2 serves the same purpose — suppressing a flood of candidates before they reach an expensive judge — but it is implemented as disqualifying searches rather than PROPER's calibrated activation and reranking. PROPER's core subject, per-user knowledge gaps, does not apply to a corpus-internal task.

**Generating Literature-Driven Scientific Theories at Scale.** Its lesson is evaluative: judge discoveries against later evidence, not LLM-judge plausibility. This cannot be implemented against a static documentation snapshot with no future corpus to check against. It remains the standing critique of the current output, which is validated only by adversarial agreement.

**ResearchBench.** An evaluation benchmark, not a detector. Nothing to implement; relevant if the ranking formula is ever calibrated against human judgement, as §5 of this document recommends.

**Enriched Knowledge Representation (Alzheimer's LBD).** A warning rather than a method: pairwise entity-to-entity graphs cannot represent conditions, multi-party interactions, or nested processes, and only about 20% of discovery statements were adequately captured by pairwise relations alone. The `llm-wiki` graph is pairwise, so this limitation applies in full and acting on it would require a graph schema change, not a change to `proto.py`.

### Known gaps in the implementation itself

- **§1 is only partially implemented.** Stage 0 seeds from one chunk by search plus one-hop expansion. The graph-statistical candidate selection §1 describes — centrality with incomplete relation types, clusters with weak explanatory connections, claims with absent provenance — is not implemented. `GraphStore` and `RESEARCHER.health()` expose the data needed for it.
- **§5 diversity term is missing.** `GapCertificate.priority` multiplies six factors; the diversity factor is absent because there is no dedup or clustering step over surviving certificates to compute it from.
- **No semantic deduplication.** §4 requires deduplicating semantically similar candidates. Only exact question-string matching exists.
- **Confidence is not a certificate field.** The §4 certificate lists Confidence; `proto.py` carries `unresolvedness` inside `PriorityScores` instead, which is related but not the same quantity.

