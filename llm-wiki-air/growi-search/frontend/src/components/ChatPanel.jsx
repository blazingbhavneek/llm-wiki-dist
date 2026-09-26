import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import MermaidDiagram from './MermaidDiagram'
import { SafeImage } from './markdown/MarkdownRenderer.jsx'
import {
  Check,
  ChevronDown,
  ChevronRight,
  Download,
  Expand,
  Loader2,
  MessageCircle,
  Send,
  Sparkles,
  Square,
} from 'lucide-react'
import { useT } from '../i18n.jsx'

const STR = {
  ja: {
    tagline: '生きたナレッジグラフとチャット',
    srcNotes: '件のソースノート',
    agentNotes: '件のエージェントノート',
    links: '件のリンク',
    topics: '件のトピック',
    askPlaceholder: '追加で質問を入力してください…',
    ask: '質問',
    stop: '停止',
    stopping: '停止中…',
    thinking: '考えています…',
    working: '作業中…',
    jevSweep: 'JEV スキャン',
    answerDone: '回答が完了しました',
    requestFailed: 'リクエストに失敗しました',
    hideSteps: '手順を隠す',
    showSteps: '手順を表示',
    references: '参照',
    citations: '引用',
    viewing: '表示中',
    viewFull: '全画面で表示',
    downloadMarkdown: 'Markdown をダウンロード',
    adding: '追加中...',
    saved: '保存済み',
    addToWiki: 'Wikiに追加',
    savedToGraph: 'グラフに保存しました。',
    addingToGraph: 'グラフに追加中...',
    diagramBuilding: '図を作成中…',
    diagramFailed: '図のレンダリングに失敗しました。',
    suggestWiki: 'Wikiに追記を提案',
    relatedConcepts: '関連概念',
    emptyTitle: 'LLM Wiki に質問する',
    emptyText: '上部の検索バーまたは下部の入力欄から、ナレッジグラフに質問できます。',
    researchMap: '調査マップ',
    mapSummary: (d, p, n) => `索引 ${d} 文書 / ${p} ページ から ${n} 件を候補に`,
    mapVisited: '閲覧済み',
    ai: 'AI',
    you: 'ユーザー',
  },
  en: {
    tagline: 'A living knowledge graph, in chat',
    srcNotes: 'source notes',
    agentNotes: 'agent notes',
    links: 'links',
    topics: 'topics',
    askPlaceholder: 'Ask a follow-up question...',
    ask: 'Ask',
    stop: 'Stop',
    stopping: 'Stopping…',
    thinking: 'Thinking…',
    working: 'Working…',
    jevSweep: 'JEV sweep',
    answerDone: 'Answer ready',
    requestFailed: 'Request failed',
    hideSteps: 'Hide steps',
    showSteps: 'Show steps',
    references: 'References',
    citations: 'Citations',
    viewing: 'Viewing',
    viewFull: 'View full',
    downloadMarkdown: 'Download Markdown',
    adding: 'Adding...',
    saved: 'Saved',
    addToWiki: 'Add to wiki',
    savedToGraph: 'Saved to the graph.',
    addingToGraph: 'Adding to the graph...',
    diagramBuilding: 'Building diagram…',
    diagramFailed: 'Diagram rendering failed.',
    suggestWiki: 'Suggest adding to Wiki',
    relatedConcepts: 'Related concepts',
    emptyTitle: 'Ask LLM Wiki',
    emptyText: 'Use the top search bar or the input below to ask the knowledge graph.',
    researchMap: 'Research map',
    mapSummary: (d, p, n) => `${n} candidates from ${d} documents / ${p} indexed pages`,
    mapVisited: 'visited',
    ai: 'AI',
    you: 'User',
  },
}

export default function ChatPanel({
  messages,
  onAsk,
  onOpenNode,
  onViewAnswer,
  activeAnswerId,
  agentRunning,
  agentCanStop,
  agentStopping,
  onStopAgent,

  // Optional but strongly recommended.
  // Expected shape: Map<nodeId, node> or plain object keyed by nodeId.
  rawById,
  growiUrl,

  // Tells App / right document rail which chunks were explicitly mentioned
  // in the final answer text by raw node ID.
  onAnswerMentionedIds,
}) {
  const t = useT(STR)
  const [question, setQuestion] = useState('')
  const endRef = useRef(null)
  const inputRef = useRef(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    autoResizeTextarea(inputRef.current)
  }, [question])

  const submit = () => {
    const clean = question.trim()
    if (!clean || agentRunning) return

    onAsk(clean)
    setQuestion('')
  }


  return (
    <section className="flex h-full min-h-0 w-full min-w-0 flex-col overflow-hidden bg-transparent">
      <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-4 pt-1">
        {messages.length === 0 && <EmptyChatState />}

        <div className="w-full space-y-4">
          {messages.map((m, i) =>
            m.role === 'user' ? (
              <UserMessage key={i} message={m} />
            ) : (
              <AssistantMessage
                key={i}
                m={m}
                onOpenNode={onOpenNode}
                onViewAnswer={onViewAnswer}
                activeAnswerId={activeAnswerId}
                rawById={rawById}
                growiUrl={growiUrl}
                onAnswerMentionedIds={onAnswerMentionedIds}
              />
            ),
          )}
        </div>


        <div ref={endRef} />
      </div>

      <div
        style={{ height: 'var(--bottom-controls-height)' }}
        className="shrink-0 border-t border-neutral-200 bg-transparent px-6 pb-3 pt-3"
      >
        <div className="flex min-h-[58px] items-end gap-3 rounded-2xl border border-neutral-200 bg-neutral-50 px-4 py-3 shadow-none transition focus-within:border-blue-400 focus-within:bg-blue-50/40 focus-within:ring-4 focus-within:ring-blue-50">
          <textarea
            ref={inputRef}
            rows={1}
            className="max-h-[180px] min-h-[34px] min-w-0 flex-1 resize-none overflow-y-auto bg-transparent py-1 text-[14px] font-medium leading-6 text-neutral-800 outline-none placeholder:text-neutral-400"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                e.preventDefault()
                submit()
              }
            }}
            placeholder={t.askPlaceholder}
          />

          {agentRunning ? (
            <button
              className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-red-600 text-white shadow-sm transition hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50"
              onClick={onStopAgent}
              disabled={!agentCanStop || agentStopping}
              title={agentStopping ? t.stopping : agentCanStop ? t.stop : t.working}
              aria-label={agentStopping ? t.stopping : agentCanStop ? t.stop : t.working}
            >
              {agentStopping || !agentCanStop ? (
                <Loader2 size={18} className="animate-spin" />
              ) : (
                <Square size={16} fill="currentColor" />
              )}
            </button>
          ) : (
            <button
              className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-600 text-white shadow-sm transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
              onClick={submit}
              disabled={!question.trim()}
              title={t.ask}
              aria-label={t.ask}
            >
              <Send size={18} />
            </button>
          )}
        </div>

      </div>
    </section>
  )
}

function EmptyChatState() {
  const t = useT(STR)

  return (
    <div className="mb-5 w-full rounded-2xl border border-dashed border-neutral-300 bg-white px-6 py-10 text-center shadow-sm">
      <div className="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-blue-50 text-blue-700">
        <MessageCircle size={24} />
      </div>

      <h2 className="mt-4 text-[20px] font-semibold tracking-tight text-neutral-950">
        {t.emptyTitle}
      </h2>

      <p className="mx-auto mt-2 max-w-[520px] text-[14px] leading-6 text-neutral-500">
        {t.emptyText}
      </p>
    </div>
  )
}

function UserMessage({ message }) {
  const t = useT(STR)

  return (
    <div className="flex w-full justify-end">
      <div className="max-w-[78%]">
        <div className="mb-1 flex justify-end text-[11px] font-bold text-neutral-400">
          {t.you}
        </div>

        <div className="whitespace-pre-wrap rounded-2xl rounded-tr-md border border-blue-100 bg-blue-50 px-4 py-3 text-[14px] font-semibold leading-6 text-neutral-900 shadow-sm">
          {message.text}
        </div>
      </div>
    </div>
  )
}

function AssistantMessage({
  m,
  onOpenNode,
  onViewAnswer,
  activeAnswerId,
  rawById,
  growiUrl,
  onAnswerMentionedIds,
}) {
  const t = useT(STR)
  const [stepsOpen, setStepsOpen] = useState(false)

  const streaming = !!m.streaming
  const errored = !!m.error
  const activity = m.activity || []
  const hasSteps = !streaming && activity.length > 0

  const diagramState = m.diagramState || m._diagState
  const answerMarkdown = getAnswerMarkdown(m)

  const refs = useMemo(() => {
    return collectAnswerRefs(m, rawById, answerMarkdown)
  }, [m, rawById, answerMarkdown])

  const hasAnswerMarkdown = !!answerMarkdown.trim()
  const isViewing = !!(m.answer && m.answer.id === activeAnswerId)

  const mentionedNodeIds = useMemo(() => {
    if (!hasAnswerMarkdown || streaming) return []
    return findMentionedNodeIds(answerMarkdown, refs, rawById)
  }, [answerMarkdown, refs, rawById, hasAnswerMarkdown, streaming])

  const mentionedNodeKey = mentionedNodeIds.join('|')

  useEffect(() => {
    if (!m.answer?.id || streaming) return
    onAnswerMentionedIds?.(m.answer.id, mentionedNodeIds)
  }, [m.answer?.id, streaming, mentionedNodeKey, onAnswerMentionedIds])

  return (
    <div className="flex w-full justify-start">
      <div className="w-full">
        <div className="mb-1 flex items-center gap-2 text-[11px] font-bold text-neutral-400">
          <span className="grid h-6 w-6 place-items-center rounded-full bg-blue-50 text-blue-700">
            <Sparkles size={14} />
          </span>
          <span>{streaming ? t.working : errored ? t.requestFailed : t.answerDone}</span>
        </div>

        <article className="w-full overflow-hidden rounded-2xl rounded-tl-md border border-neutral-200 bg-white shadow-sm">
          <div className="flex items-start gap-3 border-b border-neutral-100 bg-white px-4 py-3">
            <div className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-blue-50 text-blue-700">
              <Sparkles size={19} />
            </div>

            <div className="min-w-0 flex-1">
              <div className="truncate text-[14px] font-semibold text-neutral-950">
                {m.title || t.answerDone}
              </div>

              {hasSteps && (
                <button
                  type="button"
                  className="mt-1 inline-flex items-center gap-1 text-[12px] font-bold text-blue-600 hover:text-blue-700"
                  onClick={() => setStepsOpen((v) => !v)}
                >
                  {stepsOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  {stepsOpen ? t.hideSteps : t.showSteps}
                </button>
              )}
            </div>

            {hasAnswerMarkdown && !streaming && (
              <button
                type="button"
                className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-neutral-200 bg-white text-neutral-500 transition hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700"
                onClick={() =>
                  downloadMarkdown(
                    answerMarkdown,
                    m.answer?.title || m.answer?.question || m.title || 'llm-answer',
                    rawById,
                  )
                }
                title={t.downloadMarkdown}
                aria-label={t.downloadMarkdown}
              >
                <Download size={16} />
              </button>
            )}

            {m.answer && (
              <button
                type="button"
                className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg border transition ${
                  isViewing
                    ? 'border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-100'
                    : 'border-neutral-200 bg-white text-neutral-500 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700'
                }`}
                onClick={() => onViewAnswer?.(m.answer)}
                title={isViewing ? t.viewing : t.viewFull}
                aria-label={isViewing ? t.viewing : t.viewFull}
              >
                <Expand size={16} />
              </button>
            )}
          </div>

          <div className="px-4 py-4">
            {m.map?.nodes?.length > 0 && (
              <ResearchMap map={m.map} visitedIds={m.visitedIds} onOpenNode={onOpenNode} />
            )}

            {streaming && (
              <div className="rounded-xl border border-blue-100 bg-blue-50/40 px-4 py-3">
                {m.jev && <JevBar jev={m.jev} />}
                <ActivityTray activity={activity} streaming />
              </div>
            )}

            {hasSteps && stepsOpen && (
              <div className="mb-4 rounded-xl border border-neutral-200 bg-neutral-50 px-4 py-3">
                {m.jev && <JevBar jev={m.jev} />}
                <ActivityTray activity={activity} />
              </div>
            )}

            {!streaming && hasAnswerMarkdown && (
              <MarkdownMessage
                diagramState={diagramState}
                refs={refs}
                onOpenNode={onOpenNode}
                rawById={rawById}
                growiUrl={growiUrl}
              >
                {answerMarkdown}
              </MarkdownMessage>
            )}

            {/*
              Removed the old full References list here.

              The right document rail already shows all cited/used source nodes,
              so repeating the same 20 links in the chat response is redundant.
            */}

          </div>
        </article>
      </div>
    </div>
  )
}


function References({ refs, onOpenNode, rawById }) {
  const t = useT(STR)

  return (
    <div className="mt-4 border-t border-neutral-100 pt-4">
      <h3 className="mb-2 text-[12px] font-semibold uppercase tracking-wider text-neutral-400">
        {t.references}
      </h3>

      <ol className="space-y-2 pl-5 text-[13px] text-neutral-500">
        {refs.map((r) => (
          <li key={r.id} className="pl-1">
            <button
              type="button"
              className="font-bold text-blue-700 underline decoration-blue-200 underline-offset-2 hover:text-blue-800"
              onClick={() => openNodeFromId(r.id, rawById, onOpenNode)}
            >
              {r.label || r.id}
            </button>
            {r.note && <span> — {r.note}</span>}
          </li>
        ))}
      </ol>
    </div>
  )
}

function RelatedConcepts({ refs, onOpenNode, rawById }) {
  const t = useT(STR)

  return (
    <div className="mt-5 w-full rounded-2xl border border-neutral-200 bg-white p-4 shadow-sm">
      <h3 className="mb-3 text-[14px] font-semibold text-neutral-950">
        {t.relatedConcepts}
      </h3>

      <div className="flex flex-wrap gap-2">
        {refs.slice(0, 8).map((r) => (
          <button
            type="button"
            key={r.id}
            onClick={() => openNodeFromId(r.id, rawById, onOpenNode)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-neutral-200 bg-white px-3 py-2 text-[12px] font-bold text-neutral-600 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700"
          >
            <MessageCircle size={14} />
            {r.label || r.id}
          </button>
        ))}
      </div>
    </div>
  )
}

/** Which 00-目次 index cards the researcher picked for this question, grouped by document. */
function ResearchMap({ map, visitedIds, onOpenNode }) {
  const t = useT(STR)
  const visited = new Set(Array.isArray(visitedIds) ? visitedIds : [])
  const groups = new Map()
  for (const node of map.nodes) {
    const key = node.document || '—'
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(node)
  }

  return (
    <div className="mb-4 rounded-xl border border-neutral-200 bg-neutral-50 px-4 py-3">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-[12px] font-semibold uppercase tracking-wider text-blue-700">{t.researchMap}</span>
        <span className="text-[11px] font-medium text-neutral-500">{t.mapSummary(map.documents, map.pages, map.nodes.length)}</span>
      </div>

      <div className="space-y-2">
        {[...groups.entries()].map(([document, nodes]) => (
          <div key={document}>
            <div className="mb-1 truncate text-[11.5px] font-bold text-neutral-600" title={document}>{document}</div>
            <div className="flex flex-wrap gap-1.5">
              {nodes.map((node) => {
                const seen = visited.has(node.id)
                return (
                  <button
                    key={node.id}
                    type="button"
                    onClick={() => onOpenNode?.(node.id)}
                    title={seen ? `${node.title} · ${t.mapVisited}` : node.title}
                    className={`max-w-[260px] truncate rounded-lg border px-2 py-1 text-[11.5px] font-semibold transition ${
                      seen
                        ? 'border-blue-300 bg-blue-600 text-white hover:bg-blue-700'
                        : 'border-neutral-200 bg-white text-neutral-600 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700'
                    }`}
                  >
                    {node.title}
                  </button>
                )
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

const JEV_BAR_WIDTH = 250

/** Fixed-width live sweep bar: one slot per message, filled in place by jev_progress. */
function JevBar({ jev }) {
  const t = useT(STR)
  const percent = Math.max(0, Math.min(100, Number(jev?.percent) || 0))

  return (
    <div className="mb-2 flex items-center gap-2">
      <span className="shrink-0 text-[11px] font-bold tracking-wide text-neutral-400">
        {t.jevSweep}
      </span>

      <div
        role="progressbar"
        aria-label={t.jevSweep}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(percent)}
        className="h-2 shrink-0 overflow-hidden rounded-full bg-neutral-200"
        style={{ width: JEV_BAR_WIDTH, maxWidth: '100%' }}
      >
        <div
          className={`h-full rounded-full transition-[width] duration-200 ease-linear ${jev?.complete ? 'bg-neutral-400' : 'bg-blue-600'}`}
          style={{ width: `${percent}%` }}
        />
      </div>

      <span className="shrink-0 font-mono text-[11px] tabular-nums text-neutral-500">
        {`${percent.toFixed(0)}%${jev?.total ? ` ${jev.done}/${jev.total}` : ''}` +
          // avg is the body-stage mean: with yeses only from the card stage it is
          // legitimately empty, and printing 0.00 there would read as zero confidence.
          `${typeof jev?.yes === 'number'
            ? jev.yes
              ? ` yes ${jev.yes}${jev.mean ? ` avg p=${jev.mean.toFixed(2)}` : ''}`
              : ' yes 0'
            : ''}`}
      </span>
    </div>
  )
}

function ActivityTray({ activity, streaming }) {
  const t = useT(STR)

  if (!activity?.length) {
    return streaming ? (
      <div className="flex items-center gap-2 text-[13px] font-medium text-neutral-500">
        <Loader2 size={15} className="animate-spin text-blue-600" />
        {t.thinking}
      </div>
    ) : null
  }

  return (
    <ul className="m-0 list-none space-y-2 p-0 text-[13px] font-medium text-neutral-500">
      {activity.map((line, idx) => {
        const last = idx === activity.length - 1

        return (
          <li key={idx} className="flex items-start gap-2">
            <span className="mt-[2px] grid h-4 w-4 shrink-0 place-items-center text-blue-600">
              {streaming && last ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <Check size={14} />
              )}
            </span>

            <span className={streaming && last ? 'text-neutral-900' : ''}>
              {line}
            </span>
          </li>
        )
      })}
    </ul>
  )
}

function MermaidBlock({ code, state }) {
  const t = useT(STR)

  if (state === 'pending') {
    return (
      <div className="my-3 flex items-center gap-2 rounded-xl border border-neutral-200 bg-neutral-50 px-4 py-3 text-[13px] font-medium text-neutral-500">
        <Loader2 size={15} className="animate-spin text-blue-600" />
        {t.diagramBuilding}
      </div>
    )
  }

  if (state === 'failed') {
    return (
      <div className="my-3">
        <div className="mb-2 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-[13px] font-medium text-red-700">
          {t.diagramFailed}
        </div>
        <pre className="max-w-full overflow-x-auto rounded-xl border border-neutral-800 bg-neutral-950 p-4 text-white">
          <code className="font-mono text-[12.5px]">{code}</code>
        </pre>
      </div>
    )
  }

  return <MermaidDiagram code={code} />
}

function MarkdownMessage({
  children,
  diagramState,
  refs,
  onOpenNode,
  rawById,
  growiUrl,
}) {
  const t = useT(STR)
  const isMermaid = (cls) => (cls || '').includes('language-mermaid')

  const markdownWithCitationBlock = useMemo(() => {
    return rewriteCitedNodeIdsBlock(children || '', rawById, t.citations)
  }, [children, rawById, t.citations])

  const linkedMarkdown = useMemo(() => {
    return linkifyNodeIdsInMarkdown(markdownWithCitationBlock || '', refs || [], rawById)
  }, [markdownWithCitationBlock, refs, rawById])

  return (
    <div className="prose prose-slate max-w-none overflow-hidden text-[14px] leading-7 text-neutral-800 prose-headings:font-semibold prose-headings:text-neutral-950 prose-a:text-blue-700 prose-strong:text-neutral-950">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          img: ({ src = '', alt = '', title, width, height }) => (
            <SafeImage src={src} alt={alt} title={title} width={width} height={height} growiUrl={growiUrl} />
          ),

          pre: ({ children, ...props }) => {
            const child = Array.isArray(children) ? children[0] : children

            if (isMermaid(child?.props?.className)) {
              return <>{children}</>
            }

            return (
              <pre
                className="my-3 max-w-full overflow-x-auto rounded-xl border border-neutral-800 bg-neutral-950 p-4 text-white"
                {...props}
              >
                {children}
              </pre>
            )
          },

          h1: ({ ...props }) => (
            <h1
              className="mb-3 mt-5 text-[22px] font-semibold leading-tight text-neutral-950 first:mt-0"
              {...props}
            />
          ),

          h2: ({ ...props }) => (
            <h2
              className="mb-2 mt-5 text-[18px] font-semibold leading-tight text-neutral-950"
              {...props}
            />
          ),

          h3: ({ ...props }) => (
            <h3
              className="mb-2 mt-4 text-[16px] font-bold leading-tight text-neutral-950"
              {...props}
            />
          ),

          h4: ({ ...props }) => (
            <h4
              className="mb-2 mt-4 text-[14px] font-bold leading-tight text-neutral-950"
              {...props}
            />
          ),

          p: ({ ...props }) => (
            <p className="my-3 first:mt-0 last:mb-0" {...props} />
          ),

          ul: ({ ...props }) => (
            <ul className="my-3 list-disc space-y-1 pl-5" {...props} />
          ),

          ol: ({ ...props }) => (
            <ol className="my-3 list-decimal space-y-1 pl-5" {...props} />
          ),

          li: ({ ...props }) => (
            <li className="pl-1" {...props} />
          ),

          blockquote: ({ ...props }) => (
            <blockquote
              className="my-4 rounded-r-xl border-l-4 border-blue-200 bg-blue-50/40 px-4 py-3 text-neutral-600"
              {...props}
            />
          ),

          a: ({ href, children, ...props }) => {
            const rawHref = String(href || '')

            if (rawHref.startsWith('#llm-wiki-node:')) {
              const id = decodeURIComponent(rawHref.slice('#llm-wiki-node:'.length))

              return (
                <button
                  type="button"
                  className="inline font-bold text-blue-700 underline decoration-blue-200 underline-offset-2 hover:text-blue-800"
                  onClick={(e) => {
                    e.preventDefault()
                    e.stopPropagation()
                    openNodeFromId(id, rawById, onOpenNode)
                  }}
                >
                  {children}
                </button>
              )
            }

            const knownPathId = findKnownPathId(rawHref, rawById)
            if (knownPathId) {
              return (
                <button
                  type="button"
                  className="inline font-bold text-blue-700 underline decoration-blue-200 underline-offset-2 hover:text-blue-800"
                  onClick={(e) => {
                    e.preventDefault()
                    e.stopPropagation()
                    openNodeFromId(knownPathId, rawById, onOpenNode)
                  }}
                >
                  {children}
                </button>
              )
            }

            return (
              <a
                className="font-bold text-blue-700 underline decoration-blue-200 underline-offset-2 hover:text-blue-800"
                target="_blank"
                rel="noreferrer"
                href={href}
                {...props}
              >
                {children}
              </a>
            )
          },

          hr: ({ ...props }) => (
            <hr className="my-5 border-0 border-t border-neutral-200" {...props} />
          ),

          strong: ({ ...props }) => (
            <strong className="font-semibold text-neutral-950" {...props} />
          ),

          code: ({ inline, className, children, ...props }) => {
            if (!inline && isMermaid(className)) {
              return (
                <MermaidBlock
                  code={String(children).replace(/\n$/, '')}
                  state={diagramState}
                />
              )
            }

            if (inline) {
              return (
                <code
                  className="rounded-md bg-neutral-100 px-1.5 py-0.5 font-mono text-[12.5px] font-semibold text-rose-700"
                  {...props}
                >
                  {children}
                </code>
              )
            }

            return (
              <code
                className={`font-mono text-[12.5px] ${className || ''}`}
                {...props}
              >
                {children}
              </code>
            )
          },

          table: ({ ...props }) => (
            <div className="my-4 max-w-full overflow-x-auto rounded-xl border border-neutral-200">
              <table className="w-full border-collapse text-[13px]" {...props} />
            </div>
          ),

          th: ({ ...props }) => (
            <th
              className="border-b border-r border-neutral-200 bg-neutral-50 px-3 py-2 text-left font-semibold text-neutral-950 last:border-r-0"
              {...props}
            />
          ),

          td: ({ ...props }) => (
            <td
              className="border-b border-r border-neutral-200 px-3 py-2 align-top last:border-r-0"
              {...props}
            />
          ),
        }}
      >
        {linkedMarkdown}
      </ReactMarkdown>
    </div>
  )
}

function rewriteCitedNodeIdsBlock(markdown, rawById, citationsLabel) {
  const text = String(markdown || '')
  if (!text.trim()) return text
  return text
}

function autoResizeTextarea(el) {
  if (!el) return

  el.style.height = 'auto'
  const next = Math.min(el.scrollHeight, 180)
  el.style.height = `${next}px`
}

function downloadMarkdown(markdown, title, rawById) {
  if (typeof document === 'undefined') return

  const filename = `${sanitizeFilename(title || 'llm-answer')}.md`
  const cleaned = stripCitedNodeIdsBlocks(markdown || '', rawById)
  const blob = new Blob([cleaned], {
    type: 'text/markdown;charset=utf-8',
  })

  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')

  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()

  URL.revokeObjectURL(url)
}

function sanitizeFilename(value) {
  return String(value || 'llm-answer')
    .trim()
    .replace(/[\\/:*?"<>|]+/g, '-')
    .replace(/\s+/g, ' ')
    .slice(0, 90) || 'llm-answer'
}

function stripCitedNodeIdsBlocks(markdown, rawById) {
  const NODE_ID_EXPORT_RE =
    /(^|[^A-Za-z0-9_:\\-])?((?:node:)?[A-Za-z0-9](?:[A-Za-z0-9_.\\-]*[A-Za-z0-9])?(?::[A-Za-z0-9](?:[A-Za-z0-9_.\\-]*[A-Za-z0-9])?)+)(?=$|[^A-Za-z0-9_:\\-])/g

  return String(markdown || '').replace(
    NODE_ID_EXPORT_RE,
    (full, prefix, candidate) => {
      const left = prefix || ''
      const rawCandidate = String(candidate || '').trim()
      if (!rawCandidate) return full

      const canonicalId = resolveCanonicalNodeId(rawCandidate, rawById)
      if (!hasRawNode(rawById, canonicalId)) return full

      const docName = getOriginalDocumentName(canonicalId, rawById)
      if (!docName) return full

      return `${left}${docName}`
    },
  )
}

function getAnswerMarkdown(message) {
  return (
    message?.answer?.markdown ||
    message?._diagMd ||
    message?.text ||
    ''
  )
}

function collectAnswerRefs(message, rawById, markdown) {
  const byId = new Map()
  let syntheticIndex = 1

  const addRef = (ref) => {
    if (!ref) return

    const originalId = String(ref.id || ref.nodeId || ref.key || '').trim()
    if (!originalId) return

    const canonicalId = resolveCanonicalNodeId(originalId, rawById)
    if (!canonicalId) return

    const prev = byId.get(canonicalId) || {}

    byId.set(canonicalId, {
      ...prev,
      ...ref,
      id: canonicalId,
      originalId,
      label:
        getReferenceLabel(canonicalId, rawById, ref) ||
        getReferenceLabel(originalId, rawById, ref) ||
        prev.label ||
        `Source chunk ${syntheticIndex++}`,
    })
  }

  for (const ref of Array.isArray(message?.answer?.refs) ? message.answer.refs : []) {
    addRef(ref)
  }

  for (const ref of Array.isArray(message?.refs) ? message.refs : []) {
    addRef(ref)
  }

  for (const id of Array.isArray(message?.answer?.citedIds) ? message.answer.citedIds : []) {
    addRef({ id })
  }

  for (const id of extractExplicitNodeIdsFromMarkdown(markdown || getAnswerMarkdown(message))) {
    addRef({ id })
  }

  return Array.from(byId.values())
}

function linkifyNodeIdsInMarkdown(markdown, refs, rawById) {
  const text = String(markdown || '')
  if (!text.trim()) return text

  const explicitIds = extractExplicitNodeIdsFromMarkdown(text)

  const catalog = buildNodeLinkCatalog(
    [
      ...(Array.isArray(refs) ? refs : []),
      ...explicitIds.map((id) => ({ id })),
    ],
    rawById,
  )

  if (catalog.size === 0 && explicitIds.length === 0) return text

  return protectMarkdownSpecialRegions(text, (plainText) => {
    return replaceNodeIdTextWithChunkLinks(plainText, catalog, rawById)
  })
}

function protectMarkdownSpecialRegions(markdown, replacer) {
  return String(markdown || '')
    .split(/(```[\s\S]*?```|`[^`\n]*`|!?\[[^\]]*\]\([^)]+\))/g)
    .map((part) => {
      if (!part) return part

      if (part.startsWith('```')) return part
      if (part.startsWith('`') && part.endsWith('`')) return part
      if (/^!?\[[^\]]*\]\([^)]+\)$/.test(part)) return part

      return replacer(part)
    })
    .join('')
}

function replaceNodeIdTextWithChunkLinks(text, catalog, rawById) {
  const NODE_ID_CANDIDATE_RE =
    /(^|[^A-Za-z0-9_:\\-])?((?:node:)?(?:[0-9a-fA-F]{24}|[A-Za-z0-9](?:[A-Za-z0-9_.\\-]*[A-Za-z0-9])?(?::[A-Za-z0-9](?:[A-Za-z0-9_.\\-]*[A-Za-z0-9])?)+))(?=$|[^A-Za-z0-9_:\\-])/g

  let syntheticIndex = 1

  return String(text || '').replace(
    NODE_ID_CANDIDATE_RE,
    (full, prefix, candidate) => {
      const left = prefix || ''
      const rawCandidate = String(candidate || '').trim()
      if (!rawCandidate) return full

      const canonicalId = resolveCanonicalNodeId(rawCandidate, rawById)

      const known =
        catalog.get(canonicalId) ||
        catalog.get(rawCandidate) ||
        catalog.get(stripNodePrefix(rawCandidate)) ||
        catalog.get(addNodePrefix(stripNodePrefix(rawCandidate)))

      if (!known && !looksLikeExplicitNodeId(rawCandidate, rawById)) {
        return full
      }

      const label =
        known?.label ||
        getReferenceLabel(canonicalId, rawById) ||
        getReferenceLabel(rawCandidate, rawById) ||
        `Source chunk ${syntheticIndex++}`

      const href = `#llm-wiki-node:${encodeURIComponent(canonicalId || rawCandidate)}`

      return `${left}[${escapeMarkdownLinkText(label)}](${href})`
    },
  )
}

function buildNodeLinkCatalog(refs, rawById) {
  const catalog = new Map()
  let syntheticIndex = 1

  const add = (ref) => {
    const originalId = String(ref?.id || ref?.nodeId || ref?.key || '').trim()
    if (!originalId) return

    const canonicalId = resolveCanonicalNodeId(originalId, rawById)
    if (!canonicalId) return

    const label =
      getReferenceLabel(canonicalId, rawById, ref) ||
      getReferenceLabel(originalId, rawById, ref) ||
      ref?.label ||
      ref?.title ||
      ref?.entity ||
      ref?.name ||
      `Source chunk ${syntheticIndex++}`

    const item = {
      id: canonicalId,
      originalId,
      label,
    }

    catalog.set(canonicalId, item)
    catalog.set(originalId, item)
    catalog.set(stripNodePrefix(originalId), item)
    catalog.set(addNodePrefix(stripNodePrefix(originalId)), item)
    catalog.set(stripNodePrefix(canonicalId), item)
    catalog.set(addNodePrefix(stripNodePrefix(canonicalId)), item)
  }

  for (const ref of refs || []) {
    add(ref)
  }

  return catalog
}

function findMentionedNodeIds(markdown, refs, rawById) {
  const text = stripMarkdownCode(String(markdown || ''))
  const explicitIds = extractExplicitNodeIdsFromMarkdown(text)

  const catalog = buildNodeLinkCatalog(
    [
      ...(Array.isArray(refs) ? refs : []),
      ...explicitIds.map((id) => ({ id })),
    ],
    rawById,
  )

  const found = new Set()

  const NODE_ID_CANDIDATE_RE =
    /(^|[^A-Za-z0-9_:\\-])?((?:node:)?(?:[0-9a-fA-F]{24}|[A-Za-z0-9](?:[A-Za-z0-9_.\\-]*[A-Za-z0-9])?(?::[A-Za-z0-9](?:[A-Za-z0-9_.\\-]*[A-Za-z0-9])?)+))(?=$|[^A-Za-z0-9_:\\-])/g

  let match

  while ((match = NODE_ID_CANDIDATE_RE.exec(text)) !== null) {
    const rawCandidate = String(match[2] || '').trim()
    if (!rawCandidate) continue

    const canonicalId = resolveCanonicalNodeId(rawCandidate, rawById)

    const known =
      catalog.get(canonicalId) ||
      catalog.get(rawCandidate) ||
      catalog.get(stripNodePrefix(rawCandidate)) ||
      catalog.get(addNodePrefix(stripNodePrefix(rawCandidate)))

    if (known?.id) {
      found.add(known.id)
      continue
    }

    if (looksLikeExplicitNodeId(rawCandidate, rawById)) {
      found.add(canonicalId || rawCandidate)
    }
  }

  return Array.from(found)
}

function extractExplicitNodeIdsFromMarkdown(markdown) {
  const text = stripMarkdownCode(String(markdown || ''))
  const found = new Set()

  const NODE_ID_CANDIDATE_RE =
    /(^|[^A-Za-z0-9_:\\-])?((?:node:)?(?:[0-9a-fA-F]{24}|[A-Za-z0-9](?:[A-Za-z0-9_.\\-]*[A-Za-z0-9])?(?::[A-Za-z0-9](?:[A-Za-z0-9_.\\-]*[A-Za-z0-9])?)+))(?=$|[^A-Za-z0-9_:\\-])/g

  let match

  while ((match = NODE_ID_CANDIDATE_RE.exec(text)) !== null) {
    const candidate = String(match[2] || '').trim()
    if (!candidate) continue

    if (looksLikeExplicitNodeId(candidate)) {
      found.add(candidate)
    }
  }

  return Array.from(found)
}

function looksLikeExplicitNodeId(value, rawById) {
  const id = String(value || '').trim()
  if (!id) return false

  const canonicalId = resolveCanonicalNodeId(id, rawById)

  if (id.startsWith('node:')) return true

  if (hasRawNode(rawById, id)) return true
  if (hasRawNode(rawById, canonicalId)) return true
  if (hasRawNode(rawById, stripNodePrefix(id))) return true
  if (hasRawNode(rawById, addNodePrefix(stripNodePrefix(id)))) return true

  const lastSegment = id.split(':').pop() || ''
  if (/^[0-9a-fA-F]{8,}$/.test(lastSegment)) return true

  return false
}

function resolveCanonicalNodeId(id, rawById) {
  const clean = String(id || '').trim()
  if (!clean) return clean

  const withoutNode = stripNodePrefix(clean)
  const withNode = addNodePrefix(withoutNode)

  if (hasRawNode(rawById, clean)) return clean
  if (hasRawNode(rawById, withoutNode)) return withoutNode
  if (hasRawNode(rawById, withNode)) return withNode

  return withoutNode || clean
}

function openNodeFromId(id, rawById, onOpenNode) {
  const canonicalId = resolveCanonicalNodeId(id, rawById)

  if (canonicalId) {
    onOpenNode?.(canonicalId)
    return
  }

  onOpenNode?.(id)
}

function getNodeLabel(id, rawById, fallbackRef) {
  const node =
    getRawNode(rawById, id) ||
    getRawNode(rawById, stripNodePrefix(id)) ||
    getRawNode(rawById, addNodePrefix(stripNodePrefix(id)))

  return (
    node?.title ||
    node?.label ||
    node?.entity ||
    node?.name ||
    node?.heading ||
    node?.metadata?.title ||
    node?.metadata?.label ||
    fallbackRef?.label ||
    fallbackRef?.title ||
    fallbackRef?.entity ||
    fallbackRef?.name ||
    ''
  )
}

function getReferenceLabel(id, rawById, fallbackRef) {
  return (
    getOriginalDocumentName(id, rawById) ||
    getNodeLabel(id, rawById, fallbackRef)
  )
}

function getOriginalDocumentName(id, rawById) {
  const node =
    getRawNode(rawById, id) ||
    getRawNode(rawById, stripNodePrefix(id)) ||
    getRawNode(rawById, addNodePrefix(stripNodePrefix(id)))

  return (
    node?.original_document_name ||
    node?.document_name ||
    node?.documentName ||
    node?.sourceName ||
    node?.source_name ||
    node?.source_path ||
    node?.metadata?.original_document_name ||
    node?.metadata?.document_name ||
    node?.metadata?.documentName ||
    node?.metadata?.sourceName ||
    node?.metadata?.source ||
    ''
  )
}

function getRawNode(rawById, id) {
  const clean = String(id || '').trim()
  if (!clean || !rawById) return null

  if (typeof rawById.get === 'function') {
    return rawById.get(clean) || null
  }

  return rawById[clean] || null
}

function hasRawNode(rawById, id) {
  const clean = String(id || '').trim()
  if (!clean || !rawById) return false

  if (typeof rawById.has === 'function') {
    return rawById.has(clean)
  }

  return Object.prototype.hasOwnProperty.call(rawById, clean)
}

function findKnownPathId(href, rawById) {
  const raw = String(href || '').split('#', 1)[0].trim()
  if (!raw || raw.startsWith('#') || /^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(raw)) return ''

  let path = raw.startsWith('/') ? raw : `/${raw}`
  try {
    path = decodeURIComponent(path)
  } catch {
    return ''
  }
  path = path.replace(/\/+$/, '') || '/'
  if (path.toLowerCase().endsWith('.md')) path = path.slice(0, -3) || '/'

  const nodes = rawById instanceof Map ? rawById.values() : Object.values(rawById || {})
  return [...nodes].find((node) => {
    let nodePath = String(node?.path || node?.source_path || '').replace(/\/+$/, '') || '/'
    if (nodePath.toLowerCase().endsWith('.md')) nodePath = nodePath.slice(0, -3) || '/'
    return node?.id && nodePath === path
  })?.id || ''
}

function stripNodePrefix(id) {
  return String(id || '').replace(/^node:/, '')
}

function addNodePrefix(id) {
  const clean = String(id || '').trim()
  if (!clean) return clean
  return clean.startsWith('node:') ? clean : `node:${clean}`
}

function stripMarkdownCode(markdown) {
  return String(markdown || '')
    .replace(/```[\s\S]*?```/g, '')
    .replace(/`[^`\n]*`/g, '')
}

function escapeMarkdownLinkText(value) {
  return String(value || '')
    .replace(/\\/g, '\\\\')
    .replace(/\[/g, '\\[')
    .replace(/\]/g, '\\]')
}
