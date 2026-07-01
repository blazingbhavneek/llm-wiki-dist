import { useMemo, useState } from 'react'
import { buildLibrary } from '../data/docs'
import { useT } from '../i18n.jsx'

const STR = {
  ja: {
    documents: 'ドキュメント',
    all: 'すべて',
    topics: 'トピック',
    filter: 'ドキュメントを絞り込み…',
    empty: 'まだドキュメントがありません。',
    viewFull: '完全なドキュメントを表示',
    fullDoc: '完全版',
  },
  en: {
    documents: 'Documents',
    all: 'All',
    topics: 'Topics',
    filter: 'Filter documents…',
    empty: 'No documents ingested yet.',
    viewFull: 'View full document',
    fullDoc: 'Full doc',
  },
}

// Collapsible VS Code-style document explorer.
// Two views: flat list of ingested documents, or grouped by topic (cluster).
// Each document expands to its ordered pages (chain-linked nodes) with
// prev/next hints. Clicking a page opens that node; "View full doc" opens the
// reconstructed original.
export default function DocSidebar({ nodes, edges, onOpenNode, onOpenFullDoc, activeTabId }) {
  const t = useT(STR)
  const [mode, setMode] = useState('topic') // 'flat' | 'topic'
  const [filter, setFilter] = useState('')

  const lib = useMemo(() => buildLibrary(nodes, edges), [nodes, edges])

  const q = filter.trim().toLowerCase()
  const matchDoc = (d) => !q || d.name.toLowerCase().includes(q) || d.cluster.toLowerCase().includes(q)

  return (
    <div className="flex h-full min-h-0 flex-col border-r border-line bg-[#f8fafc]">
      <div className="flex items-center justify-between gap-2 border-b border-line px-[12px] py-[10px]">
        <span className="text-[11px] font-extrabold uppercase tracking-wider text-muted">{t.documents}</span>
        <div className="flex overflow-hidden rounded border border-line">
          <SegBtn active={mode === 'flat'} onClick={() => setMode('flat')}>{t.all}</SegBtn>
          <SegBtn active={mode === 'topic'} onClick={() => setMode('topic')}>{t.topics}</SegBtn>
        </div>
      </div>

      <div className="px-[10px] py-[8px]">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder={t.filter}
          className="w-full rounded border border-line bg-white px-[10px] py-[7px] text-[12.5px] outline-none focus:border-blue/45"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto pb-[20px]">
        {mode === 'flat'
          ? lib.documents.filter(matchDoc).map((d) => (
              <DocRow key={d.name} doc={d} onOpenNode={onOpenNode} onOpenFullDoc={onOpenFullDoc} activeTabId={activeTabId} />
            ))
          : lib.topics.map((t) => {
              const docs = t.docs.filter(matchDoc)
              if (!docs.length) return null
              return <TopicGroup key={t.cluster} topic={t.cluster} docs={docs} onOpenNode={onOpenNode} onOpenFullDoc={onOpenFullDoc} activeTabId={activeTabId} />
            })}
        {!lib.documents.length && (
          <p className="px-[14px] py-[20px] text-[12.5px] text-muted">{t.empty}</p>
        )}
      </div>
    </div>
  )
}

function TopicGroup({ topic, docs, onOpenNode, onOpenFullDoc, activeTabId }) {
  const [open, setOpen] = useState(true)
  const pages = docs.reduce((s, d) => s + d.nodes.length, 0)
  return (
    <div className="mb-[2px]">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-[6px] px-[10px] py-[7px] text-left text-[12.5px] font-bold text-ink hover:bg-soft"
      >
        <Caret open={open} />
        <span className="min-w-0 flex-1 truncate">{topic}</span>
        <span className="text-[11px] font-normal text-muted2">{docs.length}·{pages}</span>
      </button>
      {open && (
        <div className="pl-[8px]">
          {docs.map((d) => (
            <DocRow key={d.name} doc={d} onOpenNode={onOpenNode} onOpenFullDoc={onOpenFullDoc} activeTabId={activeTabId} />
          ))}
        </div>
      )}
    </div>
  )
}

function DocRow({ doc, onOpenNode, onOpenFullDoc, activeTabId }) {
  const t = useT(STR)
  const [open, setOpen] = useState(false)
  const isAgent = doc.type === 'exogenous'
  return (
    <div>
      <div className="group flex items-center gap-[4px] pr-[8px] hover:bg-soft">
        <button
          onClick={() => setOpen((v) => !v)}
          className="flex min-w-0 flex-1 items-center gap-[6px] px-[10px] py-[6px] text-left text-[12.5px] text-slate-700"
        >
          <Caret open={open} />
          <span className={`h-[7px] w-[7px] shrink-0 rounded-full ${isAgent ? 'bg-orange' : 'bg-blue'}`} />
          <span className="min-w-0 flex-1 truncate">{doc.name}</span>
          <span className="text-[11px] text-muted2">{doc.nodes.length}</span>
        </button>
        <button
          title={t.viewFull}
          onClick={() => onOpenFullDoc(doc)}
          className="shrink-0 rounded border border-transparent px-[6px] py-[3px] text-[11px] text-muted opacity-0 hover:border-line hover:text-ink group-hover:opacity-100"
        >
          {t.fullDoc}
        </button>
      </div>
      {open && (
        <ol className="list-none pb-[4px]">
          {doc.nodes.map((n, i) => (
            <li key={n.id}>
              <button
                onClick={() => onOpenNode(n)}
                className={`flex w-full items-center gap-[6px] py-[4px] pl-[38px] pr-[10px] text-left text-[12px] hover:bg-blue/5 ${
                  activeTabId === `doc:${n.id}` ? 'bg-blue/10 font-semibold text-[#244a9d]' : 'text-slate-600'
                }`}
              >
                <span className="w-[18px] shrink-0 text-right font-mono text-[10px] text-muted2">{i + 1}</span>
                <span className="min-w-0 flex-1 truncate">{n.title || n.entity || n.id}</span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

function SegBtn({ active, onClick, children }) {
  return (
    <button
      onClick={onClick}
      className={`px-[9px] py-[4px] text-[11px] font-bold ${active ? 'bg-blue text-white' : 'bg-white text-muted hover:text-ink'}`}
    >
      {children}
    </button>
  )
}

function Caret({ open }) {
  return <span className="w-[10px] shrink-0 text-[10px] text-muted2">{open ? '▾' : '▸'}</span>
}
