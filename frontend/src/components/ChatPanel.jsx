import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import MermaidDiagram from './MermaidDiagram'
import {
  BookOpen,
  Check,
  ChevronDown,
  ChevronRight,
  Expand,
  Loader2,
  MessageCircle,
  Plus,
  Send,
  Sparkles,
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
    thinking: '考えています…',
    working: '作業中…',
    answerDone: '回答が完了しました',
    hideSteps: '手順を隠す',
    showSteps: '手順を表示',
    references: '参照',
    viewing: '表示中',
    viewFull: '全画面で表示',
    adding: '追加中...',
    saved: '保存済み',
    addToWiki: 'Wikiに追加',
    savedToGraph: 'グラフに保存しました。',
    addingToGraph: 'グラフに追加中...',
    diagramBuilding: '図を作成中…',
    diagramFailed: '図のレンダリングに失敗しました。',
    suggestWiki: 'Wikiに追記を提案',
    relatedConcepts: '関連概念',
    disclaimer: '回答はAIが生成しています。内容の正確性をご確認ください。',
    emptyTitle: 'LLM Wiki に質問する',
    emptyText: '上部の検索バーまたは下部の入力欄から、ナレッジグラフに質問できます。',
    ai: 'AI',
    you: 'You',
  },
  en: {
    tagline: 'A living knowledge graph, in chat',
    srcNotes: 'source notes',
    agentNotes: 'agent notes',
    links: 'links',
    topics: 'topics',
    askPlaceholder: 'Ask a follow-up question...',
    ask: 'Ask',
    thinking: 'Thinking…',
    working: 'Working…',
    answerDone: 'Answer ready',
    hideSteps: 'Hide steps',
    showSteps: 'Show steps',
    references: 'References',
    viewing: 'Viewing',
    viewFull: 'View full',
    adding: 'Adding...',
    saved: 'Saved',
    addToWiki: 'Add to wiki',
    savedToGraph: 'Saved to the graph.',
    addingToGraph: 'Adding to the graph...',
    diagramBuilding: 'Building diagram…',
    diagramFailed: 'Diagram rendering failed.',
    suggestWiki: 'Suggest adding to Wiki',
    relatedConcepts: 'Related concepts',
    disclaimer: 'AI-generated answer. Please verify critical information.',
    emptyTitle: 'Ask LLM Wiki',
    emptyText: 'Use the top search bar or the input below to ask the knowledge graph.',
    ai: 'AI',
    you: 'You',
  },
}

export default function ChatPanel({
  messages,
  health,
  onAsk,
  onOpenNode,
  onAddWiki,
  onViewAnswer,
  activeAnswerId,
  savedIds,
  addingIds,
  writeStatuses,
}) {
  const t = useT(STR)
  const [question, setQuestion] = useState('')
  const endRef = useRef(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const submit = () => {
    const clean = question.trim()
    if (!clean) return

    onAsk(clean)
    setQuestion('')
  }

  const lastAssistantWithRefs = [...messages]
    .reverse()
    .find((m) => m.role === 'assistant' && m.refs?.length > 0)

  return (
    <section className="flex h-full min-h-0 w-full min-w-0 flex-col overflow-hidden bg-transparent">
      <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-4 pt-1">
        <ChatSummary health={health} />

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
                onAddWiki={onAddWiki}
                onViewAnswer={onViewAnswer}
                activeAnswerId={activeAnswerId}
                saved={!!(m.answer && savedIds?.has(m.answer.id))}
                saving={!!(m.answer && addingIds?.has(m.answer.id))}
                writeStatus={m.answer ? writeStatuses?.get(m.answer.id) : ''}
              />
            ),
          )}
        </div>

        {lastAssistantWithRefs?.refs?.length > 0 && (
          <RelatedConcepts
            refs={lastAssistantWithRefs.refs}
            onOpenNode={onOpenNode}
          />
        )}

        <div ref={endRef} />
      </div>

      <div className="shrink-0 border-t border-blue-100/70 bg-transparent px-6 pb-3 pt-3">
        <div className="flex min-h-[58px] items-center gap-3 rounded-2xl border border-blue-100 bg-blue-50/80 px-4 shadow-none transition focus-within:border-blue-400 focus-within:bg-blue-50 focus-within:ring-4 focus-within:ring-blue-100/70">
          <input
            className="min-w-0 flex-1 bg-transparent text-[14px] font-medium text-slate-800 outline-none placeholder:text-slate-400"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) submit()
            }}
            placeholder={t.askPlaceholder}
          />

          <button
            className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-600 text-white shadow-sm transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
            onClick={submit}
            disabled={!question.trim()}
            title={t.ask}
          >
            <Send size={18} />
          </button>
        </div>

        <p className="mt-2 text-center text-[11px] font-medium text-slate-400">
          {t.disclaimer}
        </p>
      </div>
    </section>
  )
}

function ChatSummary({ health }) {
  const t = useT(STR)

  if (!health) return null

  return (
    <div className="mb-5 w-full rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center gap-3">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-50 text-blue-700">
          <BookOpen size={20} />
        </div>

        <div className="min-w-0 flex-1">
          <div className="text-[14px] font-extrabold text-slate-950">
            LLM Wiki
          </div>
          <div className="mt-0.5 text-[12px] font-medium text-slate-500">
            {t.tagline}
          </div>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-2 text-[12px] md:grid-cols-4">
        <Metric value={health.endogenous_nodes} label={t.srcNotes} />
        <Metric value={health.exogenous_nodes} label={t.agentNotes} />
        <Metric value={health.total_edges} label={t.links} />
        <Metric value={Object.keys(health.clusters || {}).length} label={t.topics} />
      </div>
    </div>
  )
}

function Metric({ value, label }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
      <div className="text-[15px] font-extrabold text-slate-950">
        {value ?? 0}
      </div>
      <div className="mt-0.5 truncate text-[11px] font-medium text-slate-500">
        {label}
      </div>
    </div>
  )
}

function EmptyChatState() {
  const t = useT(STR)

  return (
    <div className="mb-5 w-full rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-10 text-center shadow-sm">
      <div className="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-blue-50 text-blue-700">
        <MessageCircle size={24} />
      </div>

      <h2 className="mt-4 text-[20px] font-extrabold tracking-tight text-slate-950">
        {t.emptyTitle}
      </h2>

      <p className="mx-auto mt-2 max-w-[520px] text-[14px] leading-6 text-slate-500">
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
        <div className="mb-1 flex justify-end text-[11px] font-bold text-slate-400">
          {t.you}
        </div>

        <div className="rounded-2xl rounded-tr-md border border-blue-100 bg-blue-50 px-4 py-3 text-[14px] font-semibold leading-6 text-slate-900 shadow-sm">
          {message.text}
        </div>
      </div>
    </div>
  )
}

function AssistantMessage({
  m,
  onOpenNode,
  onAddWiki,
  onViewAnswer,
  activeAnswerId,
  saved,
  saving,
  writeStatus,
}) {
  const t = useT(STR)
  const [stepsOpen, setStepsOpen] = useState(false)

  const streaming = !!m.streaming
  const activity = m.activity || []
  const hasSteps = !streaming && activity.length > 0

  const diagramState = m.diagramState || m._diagState
  const answerMarkdown =
    m.answer?.markdown ||
    m._diagMd ||
    m.text ||
    ''

  const hasAnswerMarkdown = !!answerMarkdown.trim()
  const isViewing = !!(m.answer && m.answer.id === activeAnswerId)

  return (
    <div className="flex w-full justify-start">
      <div className="w-full">
        <div className="mb-1 flex items-center gap-2 text-[11px] font-bold text-slate-400">
          <span className="grid h-6 w-6 place-items-center rounded-full bg-violet-50 text-violet-700">
            <Sparkles size={14} />
          </span>
          <span>{streaming ? t.working : t.answerDone}</span>
        </div>

        <article className="w-full overflow-hidden rounded-2xl rounded-tl-md border border-slate-200 bg-white shadow-sm">
          <div className="flex items-start gap-3 border-b border-slate-100 bg-gradient-to-r from-white to-slate-50 px-4 py-3">
            <div className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-violet-50 text-violet-700">
              <Sparkles size={19} />
            </div>

            <div className="min-w-0 flex-1">
              <div className="truncate text-[14px] font-extrabold text-slate-950">
                {m.title || t.answerDone}
              </div>

              {hasSteps && (
                <button
                  className="mt-1 inline-flex items-center gap-1 text-[12px] font-bold text-blue-600 hover:text-blue-700"
                  onClick={() => setStepsOpen((v) => !v)}
                >
                  {stepsOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  {stepsOpen ? t.hideSteps : t.showSteps}
                </button>
              )}
            </div>

            {m.answer && (
              <button
                className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg border transition ${
                  isViewing
                    ? 'border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-100'
                    : 'border-slate-200 bg-white text-slate-500 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700'
                }`}
                onClick={() => onViewAnswer?.(m.answer)}
                title={isViewing ? t.viewing : t.viewFull}
              >
                <Expand size={16} />
              </button>
            )}
          </div>

          <div className="px-4 py-4">
            {streaming && (
              <div className="rounded-xl border border-blue-100 bg-blue-50 px-4 py-3">
                <ActivityTray activity={activity} streaming />
              </div>
            )}

            {hasSteps && stepsOpen && (
              <div className="mb-4 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
                <ActivityTray activity={activity} />
              </div>
            )}

            {!streaming && hasAnswerMarkdown && (
              <MarkdownMessage diagramState={diagramState}>
                {answerMarkdown}
              </MarkdownMessage>
            )}

            {m.refs?.length > 0 && (
              <References refs={m.refs} onOpenNode={onOpenNode} />
            )}

            {m.answer && (
              <AnswerActions
                answer={m.answer}
                onAddWiki={onAddWiki}
                saved={saved}
                saving={saving}
                writeStatus={writeStatus}
              />
            )}
          </div>
        </article>
      </div>
    </div>
  )
}

function AnswerActions({ answer, onAddWiki, saved, saving, writeStatus }) {
  const t = useT(STR)

  return (
    <div className="mt-4 border-t border-slate-100 pt-4">
      <div className="flex flex-wrap items-center justify-end gap-2">
        <button
          className="inline-flex items-center gap-1.5 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-[12px] font-extrabold text-blue-700 hover:bg-blue-100 disabled:cursor-default disabled:opacity-50"
          onClick={() => onAddWiki?.(answer)}
          disabled={saved || saving}
        >
          {saving ? (
            <Loader2 size={14} className="animate-spin" />
          ) : saved ? (
            <Check size={14} />
          ) : (
            <Plus size={14} />
          )}

          {saving ? t.adding : saved ? t.saved : t.suggestWiki}
        </button>
      </div>

      {(saving || saved || writeStatus) && (
        <div className="mt-3 flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-[12px] font-medium text-slate-500">
          {saving && <Loader2 size={14} className="animate-spin text-blue-600" />}
          <span>{writeStatus || (saved ? t.savedToGraph : t.addingToGraph)}</span>
        </div>
      )}
    </div>
  )
}

function References({ refs, onOpenNode }) {
  const t = useT(STR)

  return (
    <div className="mt-4 border-t border-slate-100 pt-4">
      <h3 className="mb-2 text-[12px] font-extrabold uppercase tracking-wider text-slate-400">
        {t.references}
      </h3>

      <ol className="space-y-2 pl-5 text-[13px] text-slate-500">
        {refs.map((r) => (
          <li key={r.id} className="pl-1">
            <button
              className="font-bold text-blue-700 underline decoration-blue-200 underline-offset-2 hover:text-blue-800"
              onClick={() => onOpenNode?.(r.id)}
            >
              {r.label}
            </button>
            {r.note && <span> — {r.note}</span>}
          </li>
        ))}
      </ol>
    </div>
  )
}

function RelatedConcepts({ refs, onOpenNode }) {
  const t = useT(STR)

  return (
    <div className="mt-5 w-full rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
      <h3 className="mb-3 text-[14px] font-extrabold text-slate-950">
        {t.relatedConcepts}
      </h3>

      <div className="flex flex-wrap gap-2">
        {refs.slice(0, 8).map((r) => (
          <button
            key={r.id}
            onClick={() => onOpenNode?.(r.id)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-[12px] font-bold text-slate-600 hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700"
          >
            <MessageCircle size={14} />
            {r.label}
          </button>
        ))}
      </div>
    </div>
  )
}

function ActivityTray({ activity, streaming }) {
  const t = useT(STR)

  if (!activity?.length) {
    return streaming ? (
      <div className="flex items-center gap-2 text-[13px] font-medium text-slate-500">
        <Loader2 size={15} className="animate-spin text-blue-600" />
        {t.thinking}
      </div>
    ) : null
  }

  return (
    <ul className="m-0 list-none space-y-2 p-0 text-[13px] font-medium text-slate-500">
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

            <span className={streaming && last ? 'text-slate-900' : ''}>
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
      <div className="my-3 flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3 text-[13px] font-medium text-slate-500">
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
        <pre className="max-w-full overflow-x-auto rounded-xl border border-slate-800 bg-slate-950 p-4 text-white">
          <code className="font-mono text-[12.5px]">{code}</code>
        </pre>
      </div>
    )
  }

  return <MermaidDiagram code={code} />
}

function MarkdownMessage({ children, diagramState }) {
  const isMermaid = (cls) => (cls || '').includes('language-mermaid')

  return (
    <div className="prose prose-slate max-w-none overflow-hidden text-[14px] leading-7 text-slate-800 prose-headings:font-extrabold prose-headings:text-slate-950 prose-a:text-blue-700 prose-strong:text-slate-950">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          pre: ({ children, ...props }) => {
            const child = Array.isArray(children) ? children[0] : children

            if (isMermaid(child?.props?.className)) {
              return <>{children}</>
            }

            return (
              <pre
                className="my-3 max-w-full overflow-x-auto rounded-xl border border-slate-800 bg-slate-950 p-4 text-white"
                {...props}
              >
                {children}
              </pre>
            )
          },

          h1: ({ ...props }) => (
            <h1
              className="mb-3 mt-5 text-[22px] font-extrabold leading-tight text-slate-950 first:mt-0"
              {...props}
            />
          ),

          h2: ({ ...props }) => (
            <h2
              className="mb-2 mt-5 text-[18px] font-extrabold leading-tight text-slate-950"
              {...props}
            />
          ),

          h3: ({ ...props }) => (
            <h3
              className="mb-2 mt-4 text-[16px] font-bold leading-tight text-slate-950"
              {...props}
            />
          ),

          h4: ({ ...props }) => (
            <h4
              className="mb-2 mt-4 text-[14px] font-bold leading-tight text-slate-950"
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
              className="my-4 rounded-r-xl border-l-4 border-blue-300 bg-blue-50 px-4 py-3 text-slate-600"
              {...props}
            />
          ),

          a: ({ ...props }) => (
            <a
              className="font-bold text-blue-700 underline decoration-blue-200 underline-offset-2 hover:text-blue-800"
              target="_blank"
              rel="noreferrer"
              {...props}
            />
          ),

          hr: ({ ...props }) => (
            <hr className="my-5 border-0 border-t border-slate-200" {...props} />
          ),

          strong: ({ ...props }) => (
            <strong className="font-extrabold text-slate-950" {...props} />
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
                  className="rounded-md bg-slate-100 px-1.5 py-0.5 font-mono text-[12.5px] font-semibold text-rose-700"
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
            <div className="my-4 max-w-full overflow-x-auto rounded-xl border border-slate-200">
              <table className="w-full border-collapse text-[13px]" {...props} />
            </div>
          ),

          th: ({ ...props }) => (
            <th
              className="border-b border-r border-slate-200 bg-slate-50 px-3 py-2 text-left font-extrabold text-slate-950 last:border-r-0"
              {...props}
            />
          ),

          td: ({ ...props }) => (
            <td
              className="border-b border-r border-slate-200 px-3 py-2 align-top last:border-r-0"
              {...props}
            />
          ),
        }}
      >
        {children || ''}
      </ReactMarkdown>
    </div>
  )
}
