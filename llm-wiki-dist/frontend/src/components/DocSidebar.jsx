import { useState } from 'react'
import {
  BookOpen,
  ChevronDown,
  ChevronRight,
  Download,
  ExternalLink,
  FileText,
  Folder,
  FolderOpen,
  Trash2,
} from 'lucide-react'
import { useT } from '../i18n.jsx'
import { buildDocumentMarkdown, downloadMarkdown } from '../data/download.js'

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
    downloadDocument: 'ダウンロード',
    downloadDocumentTitle: 'ドキュメント全体を Markdown でダウンロード',
    deleteDocument: '削除',
    deleteDocumentTitle: 'ドキュメント全体をデータベースから削除',
    deleting: '削除中...',
    confirmDeleteDocument: (name, count) =>
      `「${name}」（${count} セクション）とその派生エージェントノートをすべて削除します。元に戻せません。`,
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
    downloadDocument: 'Download',
    downloadDocumentTitle: 'Download the whole document as Markdown',
    deleteDocument: 'Delete',
    deleteDocumentTitle: 'Delete the whole document from the database',
    deleting: 'Deleting...',
    confirmDeleteDocument: (name, count) =>
      `Delete "${name}" (${count} section${count === 1 ? '' : 's'}) and every agent note derived from it? This cannot be undone.`,
  },
}

export default function DocSidebar({
  library,
  mode = 'documents',
  onModeChange,
  onOpenNode,
  onOpenFullDoc,
  onDeleteDocument,
  deletingDocs,
  activeTabId,
}) {
  const t = useT(STR)
  const [filter, setFilter] = useState('')

  const documents = Array.isArray(library?.documents) ? library.documents : []
  const topics = Array.isArray(library?.topics) ? library.topics : []
  const scope = getCurrentScope()

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

  const filteredDocuments = documents
    .filter((doc) => getWikiDocumentParts(doc, scope))
    .filter(matchDoc)
  const documentTree = buildDocumentTree(filteredDocuments, scope)

  const filteredTopics = topics
    .map((topic) => ({
      ...topic,
      docs: Array.isArray(topic?.docs)
        ? topic.docs.filter(
            (doc) => getWikiDocumentParts(doc, scope) && matchDoc(doc),
          )
        : [],
    }))
    .filter((topic) => topic.docs.length > 0)

  const showingTopics = mode === 'topics'

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col bg-[#f8fafc] px-[12px] py-[14px]">
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

      <div className="min-h-0 min-w-0 flex-1 overflow-x-auto overflow-y-auto pr-[2px] pb-[20px]">
        <div className="min-w-max">
          {!showingTopics && filteredDocuments.length > 0 && (
            <DocumentTree
              node={documentTree}
              query={q}
              onOpenNode={onOpenNode}
              onOpenFullDoc={onOpenFullDoc}
              onDeleteDocument={onDeleteDocument}
              deletingDocs={deletingDocs}
              activeTabId={activeTabId}
            />
          )}

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
                    displayName={getWikiDocumentLabel(doc, scope)}
                    onOpenNode={onOpenNode}
                    onOpenFullDoc={onOpenFullDoc}
                    onDeleteDocument={onDeleteDocument}
                    deleting={isDocDeleting(deletingDocs, doc)}
                    activeTabId={activeTabId}
                  />
                ))}
              </div>
            ))}

          {((!showingTopics && filteredDocuments.length === 0) ||
            (showingTopics && filteredTopics.length === 0)) && (
            <p className="py-[20px] text-[12.5px] text-muted">{t.empty}</p>
          )}
        </div>
      </div>
    </div>
  )
}

function DocumentTree({
  node,
  query,
  onOpenNode,
  onOpenFullDoc,
  onDeleteDocument,
  deletingDocs,
  activeTabId,
}) {
  const [expanded, setExpanded] = useState(() => new Set())
  const folders = [...node.folders.values()].sort(sortTreeItems)
  const documents = [...node.documents].sort((a, b) =>
    a.label.localeCompare(b.label, undefined, { numeric: true, sensitivity: 'base' }),
  )

  return (
    <>
      {folders.map((folder) => {
        const open = query || expanded.has(folder.path)

        return (
          <div key={folder.path}>
            <button
              type="button"
              onClick={() =>
                setExpanded((current) => {
                  const next = new Set(current)
                  if (next.has(folder.path)) next.delete(folder.path)
                  else next.add(folder.path)
                  return next
                })
              }
              className="mb-[3px] flex w-full items-center gap-[7px] rounded-lg px-[7px] py-[8px] text-left text-[12.5px] font-bold text-slate-700 hover:bg-blue/5"
            >
              {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              {open ? (
                <FolderOpen size={17} className="shrink-0 text-amber-500" />
              ) : (
                <Folder size={17} className="shrink-0 text-amber-500" />
              )}
              <span className="min-w-0 flex-1 break-words">{folder.name}</span>
            </button>

            {open && (
              <div className="ml-[11px] border-l border-slate-200 pl-[8px]">
                <DocumentTree
                  node={folder}
                  query={query}
                  onOpenNode={onOpenNode}
                  onOpenFullDoc={onOpenFullDoc}
                  onDeleteDocument={onDeleteDocument}
                  deletingDocs={deletingDocs}
                  activeTabId={activeTabId}
                />
              </div>
            )}
          </div>
        )
      })}

      {documents.map(({ doc, label }, index) => (
        <KnowledgeCard
          key={getDocumentKey(doc, index)}
          doc={doc}
          index={index}
          displayName={label}
          onOpenNode={onOpenNode}
          onOpenFullDoc={onOpenFullDoc}
          onDeleteDocument={onDeleteDocument}
          deleting={isDocDeleting(deletingDocs, doc)}
          activeTabId={activeTabId}
        />
      ))}
    </>
  )
}

function buildDocumentTree(documents, scope) {
  const root = makeTreeFolder('', '')

  documents.forEach((doc) => {
    const parts = getWikiDocumentParts(doc, scope)
    if (!parts?.length) return

    let folder = root

    parts.slice(0, -1).forEach((part) => {
      const path = folder.path ? `${folder.path}/${part}` : part
      if (!folder.folders.has(part)) {
        folder.folders.set(part, makeTreeFolder(part, path))
      }
      folder = folder.folders.get(part)
    })

    folder.documents.push({ doc, label: parts[parts.length - 1] })
  })

  return root
}

function makeTreeFolder(name, path) {
  return { name, path, folders: new Map(), documents: [] }
}

function sortTreeItems(a, b) {
  return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })
}

function getCurrentScope() {
  if (typeof window === 'undefined') return 'all'
  return window.location.pathname.replace(/\/+$/, '').split('/').pop() || 'all'
}

function getWikiDocumentParts(doc, scope) {
  const name = getDocumentPath(doc).replaceAll('\\', '/')
  if (!name.startsWith('/wiki/')) return null

  const parts = name.slice('/wiki/'.length).split('/').filter(Boolean)
  if (scope !== 'all' && parts[0] === scope) parts.shift()
  return parts.length ? parts : null
}

function getWikiDocumentLabel(doc, scope) {
  const parts = getWikiDocumentParts(doc, scope)
  return parts?.[parts.length - 1] || getDocumentPath(doc)
}

function getDocumentPath(doc) {
  const firstNode = Array.isArray(doc?.nodes) ? doc.nodes[0] : null

  return String(
    doc?.name ||
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
      '',
  )
}

function KnowledgeCard({
  doc,
  index,
  onOpenNode,
  onOpenFullDoc,
  onDeleteDocument,
  deleting,
  activeTabId,
  displayName: displayNameOverride,
}) {
  const t = useT(STR)
  const [open, setOpen] = useState(false)

  const nodes = Array.isArray(doc?.nodes) ? doc.nodes : []
  const isAgent = doc.type === 'exogenous'
  const displayName = displayNameOverride || getOriginalDocName(doc, t)

  const handleDownload = () => {
    downloadMarkdown(
      displayName,
      buildDocumentMarkdown(displayName, nodes),
      t.untitledDocument,
    )
  }

  const handleDelete = () => {
    if (!window.confirm(t.confirmDeleteDocument(displayName, nodes.length))) {
      return
    }

    onDeleteDocument?.(doc)
  }

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
        <div className="flex items-center gap-[6px] border-t border-line bg-[#fbfcff] px-[10px] py-[8px]">
          <button
            type="button"
            title={t.downloadDocumentTitle}
            onClick={handleDownload}
            disabled={nodes.length === 0}
            className="flex flex-1 items-center justify-center gap-[6px] rounded-md border border-line bg-white px-[8px] py-[6px] text-[11.5px] font-bold text-slate-600 hover:border-blue/40 hover:text-blue disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Download size={13} />
            {t.downloadDocument}
          </button>

          <button
            type="button"
            title={t.deleteDocumentTitle}
            onClick={handleDelete}
            disabled={deleting}
            className="flex flex-1 items-center justify-center gap-[6px] rounded-md border border-red-200 bg-red-50 px-[8px] py-[6px] text-[11.5px] font-bold text-red-700 hover:bg-red-100 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Trash2 size={13} />
            {deleting ? t.deleting : t.deleteDocument}
          </button>
        </div>
      )}

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

function isDocDeleting(deletingDocs, doc) {
  if (!deletingDocs || !doc?.name) return false

  return typeof deletingDocs.has === 'function'
    ? deletingDocs.has(doc.name)
    : false
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
