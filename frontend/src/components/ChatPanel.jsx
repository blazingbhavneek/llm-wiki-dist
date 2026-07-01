import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import mermaid from 'mermaid'
import { useT } from '../i18n.jsx'

mermaid.initialize({ startOnLoad: false, theme: 'default', securityLevel: 'strict' })

const STR = {
  ja: {
    tagline: '生きたナレッジグラフとチャット',
    uploadProcessed: 'アップロード処理済み',
    searchPlaceholder: 'ノートまたはトピックを検索…',
    srcNotes: '件のソースノート',
    agentNotes: '件のエージェントノート',
    links: '件のリンク',
    topics: '件のトピック。',
    askPlaceholder: 'グラフに質問…',
    ask: '質問',
    thinking: '考えています…',
    working: '作業中…',
    answerDone: '回答が完了しました',
    hideSteps: '▾ 手順を隠す',
    showSteps: '▸ 手順を表示',
    references: '参照',
    viewing: '表示中 →',
    view: '表示',
    adding: '追加中...',
    saved: '保存済み ✓',
    addToWiki: 'wiki に追加',
    savedToGraph: 'グラフに保存しました。',
    addingToGraph: 'グラフに追加中...',
    diagramBuilding: '図を作成中…',
    diagramRendering: '図をレンダリング中…',
  },
  en: {
    tagline: 'A living knowledge graph, in chat',
    uploadProcessed: 'Uploads processed',
    searchPlaceholder: 'Search notes or topics…',
    srcNotes: 'source notes',
    agentNotes: 'agent notes',
    links: 'links',
    topics: 'topics.',
    askPlaceholder: 'Ask the graph…',
    ask: 'Ask',
    thinking: 'Thinking…',
    working: 'Working…',
    answerDone: 'Answer ready',
    hideSteps: '▾ Hide steps',
    showSteps: '▸ Show steps',
    references: 'References',
    viewing: 'Viewing →',
    view: 'View',
    adding: 'Adding...',
    saved: 'Saved ✓',
    addToWiki: 'Add to wiki',
    savedToGraph: 'Saved to the graph.',
    addingToGraph: 'Adding to the graph...',
    diagramBuilding: 'Building diagram…',
    diagramRendering: 'Rendering diagram…',
  },
}

export default function ChatPanel({
  messages,
  health,
  onAsk,
  onSearch,
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
  const [search, setSearch] = useState('')
  const endRef = useRef(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const submit = () => {
    if (!question.trim()) return

    onAsk(question.trim())
    setQuestion('')
  }

  return (
    <aside className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden border border-line bg-white/95 shadow-lg">
      {/* ブランド */}
      <div className="border-b border-line bg-gradient-to-b from-white to-[#fbfdff] p-[20px]">
        <div className="flex items-center justify-between gap-[14px]">
          <div className="flex min-w-0 items-center gap-3">
            <div className="grid h-[42px] w-[42px] shrink-0 place-items-center border border-blue/15 bg-gradient-to-br from-blue/15 to-orange/15 font-extrabold text-[#244a9d]">
              LW
            </div>

            <div className="min-w-0">
              <h1 className="m-0 truncate text-[17px] tracking-tight">LLM-Wiki</h1>
              <p className="mt-[3px] truncate text-[12px] text-muted">
                {t.tagline}
              </p>
            </div>
          </div>

          <div className="shrink-0 whitespace-nowrap border border-green/20 bg-green/10 px-[10px] py-[8px] text-[12px] text-[#08785a]">
            {t.uploadProcessed}
          </div>
        </div>

        <div className="relative mt-[16px]">
          <span className="pointer-events-none absolute left-[14px] top-[9px] text-[18px] text-muted2">
            ⌕
          </span>

          <input
            className="w-full border border-line bg-white py-[12px] pl-[39px] pr-[12px] text-ink outline-none focus:border-blue/45 focus:shadow-[0_0_0_4px_rgba(57,119,246,.1)]"
            placeholder={t.searchPlaceholder}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && onSearch?.(search)}
          />
        </div>

        {health && (
          <div className="mt-[12px] border border-line bg-soft px-[12px] py-[11px] text-[12.5px] leading-[1.45] text-muted">
            <strong className="text-ink">{health.endogenous_nodes}</strong> {t.srcNotes} ·{' '}
            <strong className="text-ink">{health.exogenous_nodes}</strong> {t.agentNotes} ·{' '}
            <strong className="text-ink">{health.total_edges}</strong> {t.links} ·{' '}
            <strong className="text-ink">{Object.keys(health.clusters || {}).length}</strong> {t.topics}
          </div>
        )}
      </div>

      {/* メッセージ */}
      <div className="min-h-0 flex-1 overflow-auto bg-gradient-to-b from-[#fbfdff] to-white p-[18px]">
        {messages.map((m, i) =>
          m.role === 'user' ? (
            <div key={i} className="mb-[16px]">
              <div className="ml-[42px] whitespace-pre-wrap border border-[#cadcff] bg-[#f1f6ff] px-[15px] py-[14px] text-[14px] leading-[1.5] shadow-sm">
                {m.text}
              </div>
            </div>
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

        <div ref={endRef} />
      </div>

      {/* 入力欄 */}
      <div className="shrink-0 border-t border-line bg-white p-[14px]">
        <div className="flex gap-[9px] border border-line bg-soft p-[8px]">
          <input
            className="min-w-0 flex-1 border-0 bg-transparent p-[8px] text-ink outline-none"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
            placeholder={t.askPlaceholder}
          />

          <button className="border-0 bg-blue px-[14px] font-bold text-white" onClick={submit}>
            {t.ask}
          </button>
        </div>
      </div>
    </aside>
  )
}

function ActivityTray({ activity, streaming }) {
  const t = useT(STR)
  if (!activity?.length) {
    return streaming ? (
      <div className="flex items-center gap-2 text-[12.5px] text-muted">
        <Spinner /> {t.thinking}
      </div>
    ) : null
  }

  return (
    <ul className="m-0 list-none space-y-[5px] p-0 text-[12.5px] text-muted">
      {activity.map((line, idx) => {
        const last = idx === activity.length - 1
        return (
          <li key={idx} className="flex items-center gap-2">
            <span className="text-[10px] text-muted2">
              {streaming && last ? <Spinner /> : '✓'}
            </span>
            <span className={streaming && last ? 'text-ink' : ''}>{line}</span>
          </li>
        )
      })}
    </ul>
  )
}

const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']

function Spinner() {
  const [i, setI] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setI((v) => (v + 1) % SPINNER_FRAMES.length), 80)
    return () => clearInterval(id)
  }, [])
  return (
    <span className="inline-block w-[11px] text-center font-mono leading-none text-blue">
      {SPINNER_FRAMES[i]}
    </span>
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
  const [open, setOpen] = useState(false)
  const streaming = !!m.streaming
  const activity = m.activity || []
  const hasSteps = !streaming && activity.length > 0

  return (
    <div className="mb-[16px]">
      <div className="mb-[8px] flex items-center gap-2 text-[12px] text-muted">
        <span className="inline-grid h-[22px] w-[22px] place-items-center bg-blue/10 text-[11px] font-bold text-[#244a9d]">
          AI
        </span>
        {streaming ? t.working : t.answerDone}
      </div>

      <div className="border border-line bg-white px-[15px] py-[14px] text-[14px] leading-[1.5] shadow-sm">
        {m.title && (
          <div className="mb-[8px] flex items-center gap-2 font-bold">
            <span>{m.title}</span>
            {hasSteps && (
              <button
                className="text-[11px] font-normal text-[#244a9d] hover:text-blue"
                onClick={() => setOpen((v) => !v)}
              >
                {open ? t.hideSteps : t.showSteps}
              </button>
            )}
          </div>
        )}

        {streaming && (
          <div className="border-l-[3px] border-blue/30 bg-blue/5 px-[12px] py-[10px]">
            <ActivityTray activity={activity} streaming />
          </div>
        )}

        {hasSteps && open && (
          <div className="mb-[10px] border-l-[3px] border-line bg-soft px-[12px] py-[10px]">
            <ActivityTray activity={activity} />
          </div>
        )}

        {!streaming && <MarkdownMessage diagramState={m.diagramState}>{m.text}</MarkdownMessage>}

        {m.refs?.length > 0 && (
          <div className="mt-[12px] border-t border-line pt-[12px]">
            <h3 className="m-0 mb-[8px] text-[12px] font-bold uppercase tracking-wider text-muted">
              {t.references}
            </h3>

            <ol className="m-0 list-decimal pl-[20px] text-[13px] text-muted">
              {m.refs.map((r) => (
                <li key={r.id} className="my-[6px] pl-[3px]">
                  <button
                    className="border-b border-dotted border-[#244a9d]/40 text-left text-[#244a9d] hover:text-blue"
                    onClick={() => onOpenNode(r.id)}
                  >
                    {r.label}
                  </button>{' '}
                  — {r.note}
                </li>
              ))}
            </ol>
          </div>
        )}

        {m.answer && (
          <div className="mt-[13px]">
            <div className="flex items-center gap-[10px]">
              <button
                className="border border-blue/25 bg-blue/10 px-[13px] py-[10px] text-[13px] font-bold text-[#244a9d] disabled:cursor-default disabled:opacity-45"
                onClick={() => onViewAnswer(m.answer)}
                disabled={m.answer.id === activeAnswerId}
              >
                {m.answer.id === activeAnswerId ? t.viewing : t.view}
              </button>

              <button
                className="border-0 bg-ink px-[13px] py-[10px] text-[13px] font-bold text-white shadow-md disabled:cursor-default disabled:opacity-45"
                onClick={() => onAddWiki(m.answer)}
                disabled={saved || saving}
              >
                {saving ? t.adding : saved ? t.saved : t.addToWiki}
              </button>
            </div>

            {(saving || saved || writeStatus) && (
              <div className="mt-[9px] flex items-center gap-2 border border-line bg-soft px-[10px] py-[8px] text-[12px] text-muted">
                {saving && <Spinner />}
                <span>
                  {writeStatus || (saved ? t.savedToGraph : t.addingToGraph)}
                </span>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function MermaidBlock({ code, state }) {
  const t = useT(STR)
  const [svg, setSvg] = useState('')
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (state === 'pending') return
    let alive = true
    const id = 'mmd-' + Math.random().toString(36).slice(2)
    mermaid
      .render(id, code)
      .then((res) => alive && setSvg(res.svg))
      .catch(() => alive && setFailed(true))
    return () => {
      alive = false
    }
  }, [code, state])

  if (state === 'pending') {
    return (
      <div className="my-[10px] flex items-center gap-2 border border-line bg-soft px-[12px] py-[14px] text-[13px] text-muted">
        <Spinner /> {t.diagramBuilding}
      </div>
    )
  }

  if (state === 'failed' || failed) {
    // レンダリングできなかった場合は、壊れた図ではなく常に生コードにフォールバックします。
    return (
      <pre className="my-[10px] max-w-full overflow-x-auto border border-line bg-[#0f172a] p-[12px] text-white">
        <code className="font-mono text-[12.5px]">{code}</code>
      </pre>
    )
  }

  if (!svg) {
    return (
      <div className="my-[10px] flex items-center gap-2 border border-line bg-soft px-[12px] py-[14px] text-[13px] text-muted">
        <Spinner /> {t.diagramRendering}
      </div>
    )
  }

  return (
    <div
      className="my-[12px] overflow-x-auto border border-line bg-white p-[12px] text-center"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  )
}

function MarkdownMessage({ children, diagramState }) {
  const isMermaid = (cls) => (cls || '').includes('language-mermaid')
  return (
    <div className="max-w-none overflow-hidden text-[14px] leading-[1.55] text-ink">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          pre: ({ node, children, ...props }) => {
            const child = Array.isArray(children) ? children[0] : children
            if (isMermaid(child?.props?.className)) return <>{children}</>
            return (
              <pre
                className="my-[10px] max-w-full overflow-x-auto border border-line bg-[#0f172a] p-[12px] text-white"
                {...props}
              >
                {children}
              </pre>
            )
          },
          h1: ({ node, ...props }) => (
            <h1 className="mb-[10px] mt-[14px] text-[20px] font-extrabold leading-tight" {...props} />
          ),
          h2: ({ node, ...props }) => (
            <h2 className="mb-[8px] mt-[14px] text-[17px] font-bold leading-tight" {...props} />
          ),
          h3: ({ node, ...props }) => (
            <h3 className="mb-[7px] mt-[12px] text-[15px] font-bold leading-tight" {...props} />
          ),
          h4: ({ node, ...props }) => (
            <h4 className="mb-[6px] mt-[10px] text-[14px] font-bold leading-tight" {...props} />
          ),
          p: ({ node, ...props }) => (
            <p className="my-[8px] first:mt-0 last:mb-0" {...props} />
          ),
          ul: ({ node, ...props }) => (
            <ul className="my-[8px] list-disc space-y-[4px] pl-[20px]" {...props} />
          ),
          ol: ({ node, ...props }) => (
            <ol className="my-[8px] list-decimal space-y-[4px] pl-[20px]" {...props} />
          ),
          li: ({ node, ...props }) => (
            <li className="pl-[2px]" {...props} />
          ),
          blockquote: ({ node, ...props }) => (
            <blockquote
              className="my-[10px] border-l-[3px] border-blue/30 bg-blue/5 px-[12px] py-[8px] text-muted"
              {...props}
            />
          ),
          a: ({ node, ...props }) => (
            <a
              className="border-b border-dotted border-[#244a9d]/40 text-[#244a9d] hover:text-blue"
              target="_blank"
              rel="noreferrer"
              {...props}
            />
          ),
          hr: ({ node, ...props }) => (
            <hr className="my-[14px] border-0 border-t border-line" {...props} />
          ),
          strong: ({ node, ...props }) => (
            <strong className="font-bold text-ink" {...props} />
          ),
          code: ({ node, inline, className, children, ...props }) => {
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
                  className="rounded bg-soft2 px-[4px] py-[1px] font-mono text-[12.5px] text-[#7c1230]"
                  {...props}
                >
                  {children}
                </code>
              )
            }

            return (
              <code className={`font-mono text-[12.5px] ${className || ''}`} {...props}>
                {children}
              </code>
            )
          },
          table: ({ node, ...props }) => (
            <div className="my-[10px] max-w-full overflow-x-auto">
              <table className="w-full border-collapse text-[13px]" {...props} />
            </div>
          ),
          th: ({ node, ...props }) => (
            <th className="border border-line bg-soft px-[8px] py-[6px] text-left font-bold" {...props} />
          ),
          td: ({ node, ...props }) => (
            <td className="border border-line px-[8px] py-[6px] align-top" {...props} />
          ),
        }}
      >
        {children || ''}
      </ReactMarkdown>
    </div>
  )
}
