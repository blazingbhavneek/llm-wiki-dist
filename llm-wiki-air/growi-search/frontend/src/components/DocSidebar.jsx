import { useState } from 'react'

import { api } from '../api'
import { buildDocumentMarkdown, downloadMarkdown } from '../data/download.js'
import {
  ChevronDown,
  ChevronRight,
  Download,
  ExternalLink,
  FileChartColumn,
  FileCode,
  FileSpreadsheet,
  FileText,
  FileType,
  Folder,
  FolderOpen,
  Presentation,
} from 'lucide-react'

import { useT } from '../i18n.jsx'

const STR = {
  ja: {
    app: { documentPages: (n) => `${n} ページ` },
    filter: 'ドキュメントを絞り込み…',
    openFull: '文書のページ一覧',
    untitledSection: '無題のページ',
    downloadDocument: 'ダウンロード',
    downloadDocumentTitle: '全ページを結合した Markdown をダウンロード',
    downloading: '取得中…',
    downloadFailed: (m) => `ダウンロードに失敗しました: ${m}`,
  },
  en: {
    app: { documentPages: (n) => `${n} pages` },
    filter: 'Filter documents…',
    openFull: 'Document pages',
    untitledSection: 'Untitled page',
    downloadDocument: 'Download',
    downloadDocumentTitle: 'Download every page combined into one Markdown file',
    downloading: 'Fetching…',
    downloadFailed: (m) => `Download failed: ${m}`,
  },
}

export default function DocSidebar({ wiki, rootPath, onOpenNode, onOpenDocument, activeTabId }) {
  const t = useT(STR)
  const [filter, setFilter] = useState('')
  const q = filter.trim().toLowerCase()
  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col bg-white px-[12px] py-[14px]">
      <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder={t.filter}
             className="mb-[12px] w-full rounded-md border border-line bg-white px-[10px] py-[8px] text-[12.5px] outline-none focus:border-blue/45" />
      <div className="min-h-0 min-w-0 flex-1 overflow-x-auto overflow-y-auto pr-[2px] pb-[20px]">
        <div className="min-w-max">
          <LazyFolder path={rootPath} wiki={wiki} query={q} onOpenNode={onOpenNode} onOpenDocument={onOpenDocument} activeTabId={activeTabId} />
        </div>
      </div>
    </div>
  )
}

const leaf = (p) => String(p || '').split('/').filter(Boolean).pop() || ''
const isFolder = (page) => (page.descendant_count || 0) > 0

// A wiki document folder is named after its source file (wiki_folder_name: <stem>.<ext>),
// so the extension tells us it is a document and which icon to use.
const DOC_EXT_RE = /\.(pdf|docx?|pptx?|xlsx|xlsm|csv|tsv|md|markdown|txt)$/i
const isDocumentFolder = (page) => isFolder(page) && DOC_EXT_RE.test(leaf(page.path))

function docIcon(name) {
  const ext = (leaf(name).match(DOC_EXT_RE)?.[1] || '').toLowerCase()
  const tile = 'bg-neutral-100'
  if (ext === 'pdf') return { Icon: FileText, className: `${tile} text-red-600` }
  if (ext === 'doc' || ext === 'docx') return { Icon: FileType, className: `${tile} text-sky-700` }
  if (ext === 'xlsx' || ext === 'xlsm') return { Icon: FileSpreadsheet, className: `${tile} text-green-700` }
  if (ext === 'csv' || ext === 'tsv') return { Icon: FileChartColumn, className: `${tile} text-green-700` }
  if (ext === 'ppt' || ext === 'pptx') return { Icon: Presentation, className: `${tile} text-orange-600` }
  if (ext === 'md' || ext === 'markdown' || ext === 'txt') return { Icon: FileCode, className: `${tile} text-purple-700` }
  return { Icon: FileText, className: `${tile} text-neutral-600` }
}

function LazyFolder({ path, wiki, query, onOpenNode, onOpenDocument, activeTabId }) {
  const state = wiki.childrenByPath[path]
  if (!state) return null
  if (state.loading && !state.pages) return <p className="px-[7px] py-[8px] text-[12px] text-muted">…</p>
  const pages = (state.pages || []).filter((p) => !query || leaf(p.path).toLowerCase().includes(query))
  const folders = pages.filter(isFolder)
  const leaves = pages.filter((p) => !isFolder(p))
  return (
    <>
      {folders.map((folder) => {
        const open = !!wiki.expanded[folder.path]
        const kids = wiki.childrenByPath[folder.path]?.pages
        if (isDocumentFolder(folder)) {
          return (
            <KnowledgeCard key={folder.id || folder.path}
                           doc={{ name: leaf(folder.path), path: folder.path, nodes: kids || [] }}
                           open={open} loading={!!wiki.childrenByPath[folder.path]?.loading}
                           onToggle={() => wiki.toggle(folder.path)}
                           onOpenNode={onOpenNode} onOpenFullDoc={onOpenDocument} activeTabId={activeTabId} />
          )
        }
        return (
          <div key={folder.id || folder.path}>
            <button type="button" onClick={() => wiki.toggle(folder.path)}
                    className="mb-[3px] flex w-full items-center gap-[7px] rounded-lg px-[7px] py-[8px] text-left text-[12.5px] font-bold text-neutral-700 hover:bg-blue/5">
              {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              {open ? <FolderOpen size={17} className="shrink-0 text-amber-500" /> : <Folder size={17} className="shrink-0 text-amber-500" />}
              <span className="min-w-0 flex-1 break-words">{leaf(folder.path)}</span>
            </button>
            {open && (
              <div className="ml-[16px] border-l border-neutral-200 pl-[14px]">
                <LazyFolder path={folder.path} wiki={wiki} query={query} onOpenNode={onOpenNode} onOpenDocument={onOpenDocument} activeTabId={activeTabId} />
              </div>
            )}
          </div>
        )
      })}
      {leaves.map((page) => (
        <button key={page.id} type="button" onClick={() => onOpenNode?.(page)}
                className={`flex w-full items-center gap-[7px] rounded-lg px-[7px] py-[7px] text-left text-[12px] hover:bg-blue/5 ${isNodeActive(activeTabId, page.id) ? 'bg-blue/10 font-semibold text-blue-700' : 'text-neutral-600'}`}>
          <FileText size={15} className="shrink-0 text-neutral-400" />
          <span className="min-w-0 flex-1 break-words">{page.title || leaf(page.path)}</span>
        </button>
      ))}
    </>
  )
}

function KnowledgeCard({ doc, open, loading, onToggle, onOpenNode, onOpenFullDoc, activeTabId }) {
  const t = useT(STR)
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState('')
  const nodes = (Array.isArray(doc?.nodes) ? doc.nodes : []).filter((n) => !isFolder(n))
  const { Icon, className } = docIcon(doc.name)

  // Page listings carry no bodies: fetch each page once, then export them as one file.
  const handleDownload = async (e) => {
    e.stopPropagation()
    if (downloading || nodes.length === 0) return
    setDownloading(true)
    setDownloadError('')
    try {
      const pages = await Promise.all(nodes.map((n) => api.node(n.id)))
      downloadMarkdown(doc.name, buildDocumentMarkdown(doc.name, pages), t.untitledSection)
    } catch (err) {
      setDownloadError(t.downloadFailed(err.message))
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div className="mb-[6px] rounded-xl border border-line bg-white shadow-sm">
      <button type="button" onClick={onToggle} className="flex w-full items-center gap-[8px] px-[8px] py-[5px] text-left hover:bg-neutral-50">
        <div className={`flex h-[36px] w-[36px] shrink-0 items-center justify-center rounded-full ${className}`}><Icon size={18} /></div>
        <div className="min-w-0 flex-1">
          <div className="whitespace-normal break-words text-[12.5px] font-semibold leading-[1.3] text-neutral-800">{doc.name}</div>
          <div className="mt-[1px] text-[11px] text-neutral-500">{loading ? '…' : open || nodes.length ? t.app.documentPages(nodes.length) : leaf(doc.name).match(DOC_EXT_RE)?.[1]?.toUpperCase()}</div>
        </div>
        <span className="self-center text-muted2">{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
      </button>
      {open && !loading && (
        <div className="border-t border-line bg-[#fafafa] px-[10px] py-[8px]">
          <button type="button" title={t.downloadDocumentTitle} onClick={handleDownload} disabled={downloading || nodes.length === 0}
                  className="flex w-full items-center justify-center gap-[6px] rounded-md border border-line bg-white px-[8px] py-[6px] text-[11.5px] font-bold text-neutral-600 hover:border-blue/40 hover:text-blue disabled:cursor-not-allowed disabled:opacity-50">
            <Download size={13} />
            {downloading ? t.downloading : t.downloadDocument}
          </button>
          {downloadError && <p className="mt-[6px] text-[11px] text-red">{downloadError}</p>}
        </div>
      )}
      {open && !loading && (
        <ol className="border-t border-line bg-[#fafafa] py-[6px]">
          {nodes.map((node, nodeIndex) => (
            <li key={node?.id || `${doc.name}:${nodeIndex}`}>
              <button type="button" onClick={() => onOpenNode?.(node)}
                      className={`flex w-full px-[14px] py-[7px] text-left text-[12px] hover:bg-blue/5 ${isNodeActive(activeTabId, node?.id) ? 'bg-blue/10 font-semibold text-blue-700' : 'text-neutral-600'}`}>
                <span className="min-w-0 flex-1 whitespace-normal break-words leading-[1.35]">{getNodeDisplayTitle(node, t)}</span>
              </button>
            </li>
          ))}
          <li>
            <button type="button" title={t.openFull} onClick={() => onOpenFullDoc?.(doc)} className="mt-[4px] flex w-full items-center justify-end gap-[6px] px-[14px] py-[7px] text-[12px] font-bold text-blue hover:bg-blue/5">
              {t.openFull}<ExternalLink size={13} />
            </button>
          </li>
        </ol>
      )}
    </div>
  )
}

function getNodeDisplayTitle(node, t) {
  return node?.title || node?.label || node?.entity || node?.name || node?.heading || node?.metadata?.title || node?.metadata?.label || t.untitledSection
}

function isNodeActive(activeTabId, nodeId) {
  const active = String(activeTabId || '').trim()
  const cleanNodeId = String(nodeId || '').trim()
  if (!active || !cleanNodeId) return false
  return normalizeNodeId(active.replace(/^doc:/, '')) === normalizeNodeId(cleanNodeId)
}

function normalizeNodeId(id) {
  return String(id || '').trim().replace(/^node:/, '')
}
