import { useState } from 'react'
import { BookOpen, FileText, ExternalLink } from 'lucide-react'
import { useT } from '../i18n.jsx'

const STR = {
  ja: {
    documents: '参照した知識',
    top: '上位5件',
    all: 'すべて',
    topics: 'トピック',
    filter: 'ドキュメントを絞り込み…',
    empty: 'まだドキュメントがありません。',
    openFull: '完全なドキュメントを開く',
    untitledDocument: 'Untitled document',
    untitledSection: 'Untitled section',
  },
  en: {
    documents: 'Referenced Knowledge',
    top: 'Top 5',
    all: 'All',
    topics: 'Topics',
    filter: 'Filter documents…',
    empty: 'No documents ingested yet.',
    openFull: 'Open full document',
    untitledDocument: 'Untitled document',
    untitledSection: 'Untitled section',
  },
}

export default function DocSidebar({
  library,
  mode = 'documents',
  onModeChange,
  onOpenNode,
  onOpenFullDoc,
  activeTabId,
}) {
  const t = useT(STR)
  const [filter, setFilter] = useState('')

  const documents = Array.isArray(library?.documents) ? library.documents : []
  const topics = Array.isArray(library?.topics) ? library.topics : []

  const q = filter.trim().toLowerCase()

  const matchDoc = (doc) => {
    if (!q) return true

    return (
      String(doc?.name || '').toLowerCase().includes(q) ||
      String(doc?.originalName || '').toLowerCase().includes(q) ||
      String(doc?.documentName || '').toLowerCase().includes(q) ||
      String(doc?.sourceName || '').toLowerCase().includes(q) ||
      String(doc?.filename || '').toLowerCase().includes(q) ||
      String(doc?.fileName || '').toLowerCase().includes(q) ||
      String(doc?.cluster || '').toLowerCase().includes(q)
    )
  }

  const filteredDocuments = documents.filter(matchDoc)

  const filteredTopics = topics
    .map((topic) => ({
      ...topic,
      docs: Array.isArray(topic?.docs) ? topic.docs.filter(matchDoc) : [],
    }))
    .filter((topic) => topic.docs.length > 0)

  const showingTopics = mode === 'topics'

  return (
    <div className="flex h-full min-h-0 flex-col bg-[#f8fafc] px-[12px] py-[14px]">
      <div className="mb-[14px] flex items-center justify-between gap-2">
        <div className="min-w-0 text-[13px] font-extrabold text-slate-800">
          {t.documents}
        </div>

        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={() => onModeChange?.('documents')}
            className={`rounded-md px-[8px] py-[4px] text-[11px] font-bold ${
              mode === 'documents'
                ? 'bg-blue text-white'
                : 'border border-line bg-white text-slate-500'
            }`}
          >
            {t.all}
          </button>

          <button
            type="button"
            onClick={() => onModeChange?.('topics')}
            className={`rounded-md px-[8px] py-[4px] text-[11px] font-bold ${
              mode === 'topics'
                ? 'bg-blue text-white'
                : 'border border-line bg-white text-slate-500'
            }`}
          >
            {t.topics}
          </button>
        </div>
      </div>

      <input
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder={t.filter}
        className="mb-[12px] w-full rounded-md border border-line bg-white px-[10px] py-[8px] text-[12.5px] outline-none focus:border-blue/45"
      />

      <div className="min-h-0 flex-1 overflow-y-auto pr-[2px] pb-[20px]">
        {!showingTopics &&
          filteredDocuments.map((doc, index) => (
            <KnowledgeCard
              key={getDocumentKey(doc, index)}
              doc={doc}
              index={index}
              onOpenNode={onOpenNode}
              onOpenFullDoc={onOpenFullDoc}
              activeTabId={activeTabId}
            />
          ))}

        {showingTopics &&
          filteredTopics.map((topic) => (
            <div key={topic.cluster || topic.name} className="mb-[16px]">
              <div className="mb-[8px] px-[2px] text-[12px] font-extrabold text-slate-600">
                {topic.cluster || topic.name}
              </div>

              {topic.docs.map((doc, index) => (
                <KnowledgeCard
                  key={getDocumentKey(doc, index)}
                  doc={doc}
                  index={index}
                  onOpenNode={onOpenNode}
                  onOpenFullDoc={onOpenFullDoc}
                  activeTabId={activeTabId}
                />
              ))}
            </div>
          ))}

        {documents.length === 0 && (
          <p className="py-[20px] text-[12.5px] text-muted">{t.empty}</p>
        )}
      </div>
    </div>
  )
}

function KnowledgeCard({
  doc,
  index,
  onOpenNode,
  onOpenFullDoc,
  activeTabId,
}) {
  const t = useT(STR)
  const [open, setOpen] = useState(false)

  const nodes = Array.isArray(doc?.nodes) ? doc.nodes : []
  const isAgent = doc.type === 'exogenous'
  const displayName = getOriginalDocName(doc, t)

  const colors = [
    'bg-blue/10 text-blue',
    'bg-emerald-100 text-emerald-700',
    'bg-orange-100 text-orange-700',
    'bg-violet-100 text-violet-700',
  ]

  const color = colors[index % colors.length]

  return (
    <div className="mb-[10px] rounded-xl border border-line bg-white shadow-sm">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full gap-[10px] p-[12px] text-left hover:bg-slate-50"
      >
        <div
          className={`flex h-[36px] w-[36px] shrink-0 items-center justify-center rounded-full ${color}`}
        >
          {isAgent ? <BookOpen size={18} /> : <FileText size={18} />}
        </div>

        <div className="min-w-0 flex-1">
          <div className="whitespace-normal break-words text-[13px] font-semibold leading-[1.35] text-slate-800">
            {displayName}
          </div>

          <div className="mt-[4px] text-[11.5px] text-slate-500">
            {nodes.length} section{nodes.length === 1 ? '' : 's'}
          </div>
        </div>

        <span className="pt-[2px] text-[12px] text-muted2">
          {open ? '▾' : '▸'}
        </span>
      </button>

      {open && (
        <ol className="border-t border-line bg-[#fbfcff] py-[6px]">
          {nodes.map((node, nodeIndex) => {
            const active = isNodeActive(activeTabId, node?.id)
            const title = getNodeDisplayTitle(node, t)

            return (
              <li key={node?.id || `${displayName}:${nodeIndex}`}>
                <button
                  type="button"
                  onClick={() => onOpenNode?.(node)}
                  className={`flex w-full px-[14px] py-[7px] text-left text-[12px] hover:bg-blue/5 ${
                    active
                      ? 'bg-blue/10 font-semibold text-[#244a9d]'
                      : 'text-slate-600'
                  }`}
                >
                  <span className="min-w-0 flex-1 whitespace-normal break-words leading-[1.35]">
                    {title}
                  </span>
                </button>
              </li>
            )
          })}

          <li>
            <button
              type="button"
              title={t.openFull}
              onClick={() => onOpenFullDoc?.(doc)}
              className="mt-[4px] flex w-full items-center justify-end gap-[6px] px-[14px] py-[7px] text-[12px] font-bold text-blue hover:bg-blue/5"
            >
              {t.openFull}
              <ExternalLink size={13} />
            </button>
          </li>
        </ol>
      )}
    </div>
  )
}

function getDocumentKey(doc, index) {
  return (
    doc?.id ||
    doc?.name ||
    doc?.originalName ||
    doc?.documentName ||
    doc?.sourceName ||
    doc?.filename ||
    doc?.fileName ||
    `doc:${index}`
  )
}

function getOriginalDocName(doc, t) {
  const firstNode = Array.isArray(doc?.nodes) ? doc.nodes[0] : null

  return (
    doc?.originalName ||
    doc?.documentName ||
    doc?.sourceName ||
    doc?.filename ||
    doc?.fileName ||
    firstNode?.original_document_name ||
    firstNode?.documentName ||
    firstNode?.sourceName ||
    firstNode?.filename ||
    firstNode?.fileName ||
    doc?.name ||
    t.untitledDocument
  )
}

function getNodeDisplayTitle(node, t) {
  return (
    node?.title ||
    node?.label ||
    node?.entity ||
    node?.name ||
    node?.heading ||
    node?.metadata?.title ||
    node?.metadata?.label ||
    t.untitledSection
  )
}

function isNodeActive(activeTabId, nodeId) {
  const active = String(activeTabId || '').trim()
  const cleanNodeId = String(nodeId || '').trim()

  if (!active || !cleanNodeId) return false

  const activeWithoutDocPrefix = active.replace(/^doc:/, '')

  return normalizeNodeId(activeWithoutDocPrefix) === normalizeNodeId(cleanNodeId)
}

function normalizeNodeId(id) {
  return String(id || '').trim().replace(/^node:/, '')
}
