import { useCallback, useEffect, useRef, useState } from 'react'
import {
  BookMarked,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Download,
  ExternalLink,
  FolderTree,
  Loader2,
  MessageCircle,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  Search as SearchIcon,
  Send,
  Settings as SettingsIcon,
  Square,
} from 'lucide-react'

import ErrorBoundary from './components/ErrorBoundary'
import { Centered, PageHeader, PlaceholderPage } from './components/layout/Shell'
import { MarkdownWorkspaceFrame } from './components/layout/MarkdownWorkspaceFrame'
import { MarkdownRenderer, stripCitedNodeIdsBlocks } from './components/markdown/MarkdownRenderer.jsx'
import { STR } from './components/layout/strings.js'
import { downloadMarkdown } from './data/download.js'
import { growiLinkFor, growiPageUrl } from './data/growi.js'
import { faviconUrl } from './data/utils'
import { LangToggle, useT } from './i18n.jsx'
import { useAskStream } from './hooks/useAskStream'
import { useOverrides } from './hooks/useOverrides'
import { useWikiData } from './hooks/useWikiData'
import { api } from './api'

export default function App() {
  const t = useT(STR)

  const [leftCollapsed, setLeftCollapsed] = useState(false)
  const [rightOpen, setRightOpen] = useState(true)
  const [centerView, setCenterView] = useState('chat') // chat | search | page | settings
  const [history, setHistory] = useState([])
  const [workspace, setWorkspace] = useState(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState([])
  const [searchLoading, setSearchLoading] = useState(false)
  const [growi, setGrowi] = useState(null)
  const [toast, setToast] = useState(null)
  const searchSeq = useRef(0)

  const fireToast = useCallback((text) => {
    setToast(text)
    setTimeout(() => setToast(null), 3600)
  }, [])

  const { overrides, persisted, apiKey, applyOverrides, clearOverrides } = useOverrides()

  useEffect(() => {
    api.growi().then(setGrowi).catch((e) => fireToast(e.message))
  }, [fireToast])

  const wiki = useWikiData({ rootPath: growi?.root_path || '/', fireToast, t })

  // ---------------------------------------------------------------------------
  // Page / answer workspaces (history for the markdown frame's back button)
  // ---------------------------------------------------------------------------

  const pushHistory = useCallback(() => {
    setHistory((h) =>
      [...h, centerView === 'page' ? { centerView, workspace } : { centerView }].slice(-20),
    )
  }, [centerView, workspace])

  const openNode = useCallback(
    async (idOrPath) => {
      try {
        const node = await api.node(idOrPath)
        pushHistory()
        setWorkspace({
          id: node.id || idOrPath,
          kind: 'page',
          title: node.title || idOrPath,
          doc: {
            title: node.title || node.path || idOrPath,
            badge: 'GROWI',
            meta: node.path,
            markdown: node.body || '',
            source_path: node.path,
          },
          links: Array.isArray(node.links) ? node.links : [],
        })
        setCenterView('page')
      } catch (e) {
        fireToast(t.couldNotOpen(e.message))
      }
    },
    [pushHistory, fireToast, t],
  )

  const openAnswer = (answer) => {
    pushHistory()
    setWorkspace({
      id: `answer:${answer.id}`,
      kind: 'answer',
      title: answer.question,
      doc: {
        title: answer.question,
        badge: t.app.answerBadge,
        meta: answer.steps ? t.answerReady(answer.steps) : '',
        markdown: answer.markdown,
      },
      links: [],
      citedIds: answer.citedIds || [],
    })
    setCenterView('page')
  }

  const goBack = () => {
    const target = history[history.length - 1]
    if (!target) return
    setHistory((h) => h.slice(0, -1))
    setWorkspace(target.workspace || null)
    setCenterView(target.centerView || 'chat')
  }

  const closeWorkspace = () => {
    setWorkspace(null)
    setHistory([])
    setCenterView('chat')
  }

  // ---------------------------------------------------------------------------
  // Search (fast path: one ES call server-side; no agent, no page bodies)
  // ---------------------------------------------------------------------------

  const onSearch = async (text) => {
    const clean = String(text || '').trim()
    if (!clean) return

    const seq = ++searchSeq.current
    setSearchQuery(clean)
    setSearchResults([])
    setSearchLoading(true)
    setCenterView('search')

    try {
      const results = await api.search(clean, 12)
      if (seq === searchSeq.current) setSearchResults(Array.isArray(results) ? results : [])
    } catch (e) {
      if (seq === searchSeq.current) setSearchResults([])
      fireToast(t.searchFailed(e.message))
    } finally {
      if (seq === searchSeq.current) setSearchLoading(false)
    }
  }

  // ---------------------------------------------------------------------------
  // Chat / agent run (SSE)
  // ---------------------------------------------------------------------------

  const finalize = useCallback(
    (ev, activity, q) => {
      const cited = Array.isArray(ev.cited_node_ids) ? ev.cited_node_ids : []
      const has = !!(ev.answer && ev.answer.trim())

      return chat.patchLast(() => ({
        role: 'assistant',
        streaming: false,
        title: has ? t.answerReady(ev.steps) : t.foundNoBody,
        markdown: has ? ev.answer : '',
        emptyText: has ? '' : t.foundNoBodyText(cited.length, ev.steps),
        activity,
        answer: has ? { id: q, question: q, markdown: ev.answer, steps: ev.steps, citedIds: cited } : null,
      }))
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [t],
  )

  const chat = useAskStream({
    t,
    overrides,
    fireToast,
    onAskStart: () => setCenterView('chat'),
    onAnswer: (ev, activity, q) => finalize(ev, activity, q),
  })

  const parserUrl = growi?.doc_parser_url
    ? new URL(growi.doc_parser_url, window.location.origin).href
    : null

  const navItems = [
    { id: 'chat', label: t.shell.chat, icon: MessageCircle, go: () => setCenterView('chat'), active: centerView === 'chat' },
    {
      id: 'pages',
      label: t.app.pages,
      icon: FolderTree,
      go: () => {
        setRightOpen(true)
        wiki.loadRoot()
      },
      active: false,
    },
    { id: 'parser', label: t.app.parser, icon: BookMarked, href: parserUrl, active: false },
    { id: 'settings', label: t.shell.settings, icon: SettingsIcon, go: () => setCenterView('settings'), active: centerView === 'settings' },
  ]

  // ---------------------------------------------------------------------------
  // Center views
  // ---------------------------------------------------------------------------

  const renderCenter = () => {
    if (centerView === 'search') {
      return (
        <SearchCenter
          query={searchQuery}
          results={searchResults}
          loading={searchLoading}
          connection={growi}
          onOpenNode={openNode}
          t={t}
        />
      )
    }

    if (centerView === 'settings') {
      return (
        <SettingsCenterNew
          persisted={persisted}
          apiKey={apiKey}
          onApply={applyOverrides}
          onClear={clearOverrides}
          fireToast={fireToast}
          t={t}
        />
      )
    }

    if (centerView === 'page' && workspace) {
      return (
        <MarkdownWorkspaceFrame
          item={workspace}
          canGoBack={history.length > 0}
          onBack={goBack}
          onClose={closeWorkspace}
        >
          <PageView workspace={workspace} connection={growi} onOpenNode={openNode} t={t} />
        </MarkdownWorkspaceFrame>
      )
    }

    return <ChatArea chat={chat} t={t} onViewAnswer={openAnswer} />
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-[#f6f8fc] text-slate-900">
      {/* Left sidebar: Chat / Pages / Parser / Settings (read-only service) */}
      <aside
        className={`flex h-full shrink-0 flex-col border-r border-slate-200 bg-white transition-[width] duration-300 ${
          leftCollapsed ? 'w-[76px]' : 'w-[240px]'
        }`}
      >
        <div className="border-b border-slate-100 px-3 py-4">
          <div className="flex w-full justify-center">
            <img src={faviconUrl()} alt="Logo" className="block h-[90px] w-[90px] max-w-full object-contain" />
          </div>
          <button
            onClick={() => setLeftCollapsed((v) => !v)}
            className={`mt-3 grid h-8 place-items-center rounded-lg text-slate-500 hover:bg-slate-100 ${
              leftCollapsed ? 'mx-auto w-10' : 'w-full'
            }`}
            title={leftCollapsed ? t.shell.expandSidebar : t.shell.collapseSidebar}
          >
            {leftCollapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
          </button>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-4">
          <div className="space-y-1">
            {navItems.map((item) => {
              const Icon = item.icon
              const content = (
                <>
                  <Icon size={18} className={item.active ? 'text-blue-600' : 'text-slate-500'} />
                  {!leftCollapsed && <span className="truncate">{item.label}</span>}
                </>
              )
              const cls = `group flex h-10 w-full items-center gap-3 rounded-xl px-3 text-left text-[14px] font-semibold transition ${
                item.active ? 'bg-blue-50 text-blue-700' : 'text-slate-600 hover:bg-slate-100'
              } ${leftCollapsed ? 'justify-center' : ''}`

              return item.href ? (
                <a key={item.id} href={item.href} target="_blank" rel="noreferrer" className={cls} title={item.label}>
                  {content}
                </a>
              ) : (
                <button key={item.id} onClick={item.go} className={cls} title={item.label}>
                  {content}
                </button>
              )
            })}
          </div>
        </nav>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Top bar */}
        <header className="flex h-[70px] shrink-0 items-center gap-4 border-b border-slate-200 bg-white px-5">
          <div className="flex min-w-0 flex-1 items-center">
            <div className="flex h-11 w-full max-w-[760px] items-center rounded-xl border border-slate-300 bg-white shadow-sm focus-within:border-blue-500">
              <SearchIcon size={18} className="ml-4 shrink-0 text-slate-400" />
              <input
                onKeyDown={(e) => e.key === 'Enter' && onSearch(e.currentTarget.value)}
                placeholder={t.topbar.placeholder}
                className="h-full min-w-0 flex-1 bg-transparent px-3 text-[14px] font-medium outline-none"
                aria-label={t.topbar.keywordSearch}
              />
              <button
                onClick={(e) => onSearch(e.currentTarget.previousSibling?.value)}
                className="mr-1.5 rounded-lg bg-blue-600 px-5 py-2 text-[13px] font-extrabold text-white hover:bg-blue-700"
              >
                {t.topbar.searchDocs}
              </button>
            </div>
          </div>

          <div className="ml-auto flex items-center gap-2">
            <span
              className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-1.5 text-[12px] font-bold text-slate-500"
              title={t.app.scopeHint}
            >
              GROWI {growi?.root_path || '/'}
            </span>
            <LangToggle />
            <button
              onClick={() => setRightOpen((v) => !v)}
              className={`grid h-9 w-9 place-items-center rounded-xl border transition ${
                rightOpen ? 'border-blue-200 bg-blue-50 text-blue-700' : 'border-slate-200 text-slate-500 hover:bg-slate-50'
              }`}
              title={rightOpen ? t.shell.collapseDocuments : t.shell.showDocuments}
            >
              {rightOpen ? <ChevronRight size={18} /> : <ChevronLeft size={18} />}
            </button>
          </div>
        </header>

        <div className="relative flex min-h-0 flex-1 overflow-hidden">
          <main className="relative min-w-0 flex-1 overflow-hidden bg-white">
            <ErrorBoundary resetKey={`${centerView}:${workspace?.id || 'none'}:${searchQuery}`}>{renderCenter()}</ErrorBoundary>

            {toast && (
              <div className="absolute bottom-[22px] right-[22px] z-30 max-w-[390px] rounded-xl border border-emerald-200 bg-emerald-50 px-[14px] py-[13px] text-[13px] text-emerald-800 shadow-xl">
                {toast}
              </div>
            )}
          </main>

          {rightOpen && <PageRail wiki={wiki} growi={growi} openNode={openNode} t={t} onClose={() => setRightOpen(false)} />}
        </div>
      </div>
    </div>
  )
}

// -----------------------------------------------------------------------------
// Right rail: lazy GROWI folder tree (one /api/pages/children call per expand)
// -----------------------------------------------------------------------------

function PageRail({ wiki, growi, openNode, t, onClose }) {
  return (
    <aside className="flex h-full w-[300px] shrink-0 flex-col border-l border-slate-200 bg-white">
      <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
        <div className="text-[14px] font-extrabold">{t.app.pages}</div>
        <div className="flex items-center gap-1">
          <button
            onClick={wiki.refresh}
            className="grid h-8 w-8 place-items-center rounded-lg text-slate-500 hover:bg-slate-100"
            title={t.app.refresh}
          >
            <RefreshCw size={15} />
          </button>
          <button
            onClick={onClose}
            className="grid h-8 w-8 place-items-center rounded-lg text-slate-500 hover:bg-slate-100"
            title={t.shell.closeRightSidebar}
          >
            <ChevronRight size={16} />
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto py-2 text-[13px]">
        <RailFolder path={growi?.root_path || '/'} depth={0} wiki={wiki} openNode={openNode} t={t} top />
      </div>
    </aside>
  )
}

function RailFolder({ path, depth, wiki, openNode, t, top = false }) {
  const state = wiki.childrenByPath[path]
  const expanded = wiki.expanded[path]

  useEffect(() => {
    if (top) wiki.loadRoot()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [top, path])

  if (top && !state) return <div className="px-4 py-2 text-slate-400">{t.app.loading}</div>

  const pages = state?.pages
  const label = top ? (path === '/' ? t.app.root : path) : path.split('/').filter(Boolean).pop()

  return (
    <div>
      {top && <div className="px-3 pb-1 text-[11px] font-bold uppercase text-slate-400">{path}</div>}
      {state?.loading && (
        <div className="flex items-center gap-2 py-1 text-slate-400" style={{ paddingLeft: 12 + depth * 14 }}>
          <Loader2 size={13} className="animate-spin" />
        </div>
      )}
      {state?.error && <div className="px-4 py-1 text-red-500">{state.error}</div>}
      {!state?.loading && pages?.length === 0 && !top && (
        <div className="py-1 text-slate-300" style={{ paddingLeft: 34 + depth * 14 }}>
          {t.app.empty}
        </div>
      )}
      {(pages || []).map((child) => {
        const hasKids = (child.descendant_count || 0) > 0
        const childLabel = String(child.path || child.title).split('/').filter(Boolean).pop() || child.title
        return (
          <div key={child.id || child.path}>
            <div className="group flex items-center gap-1 rounded-lg hover:bg-slate-50" style={{ paddingLeft: 6 + depth * 14 }}>
              {hasKids ? (
                <button
                  onClick={() => wiki.toggle(child.path)}
                  className="grid h-6 w-5 shrink-0 place-items-center text-slate-400"
                  aria-label="expand"
                >
                  {wiki.expanded[child.path] ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                </button>
              ) : (
                <span className="w-5 shrink-0" />
              )}
              <button
                onClick={() => openNode(child.id || child.path)}
                className="min-w-0 flex-1 truncate py-1.5 pr-2 text-left text-slate-700 hover:text-blue-700"
                title={child.path}
              >
                {childLabel}
              </button>
            </div>
            {hasKids && wiki.expanded[child.path] && (
              <RailFolder path={child.path} depth={depth + 1} wiki={wiki} openNode={openNode} t={t} />
            )}
          </div>
        )
      })}
      {top && expanded === undefined && null}
    </div>
  )
}

// -----------------------------------------------------------------------------
// Search results
// -----------------------------------------------------------------------------

function SearchCenter({ query, results, loading, connection, onOpenNode, t }) {
  if (loading) {
    return (
      <Centered>
        <span className="flex items-center gap-2 font-bold text-slate-500">
          <Loader2 size={16} className="animate-spin text-blue-600" /> {t.topbar.searching}
        </span>
      </Centered>
    )
  }
  if (!query) return <PlaceholderPage icon={SearchIcon} title={t.pages.searchIdleTitle} text={t.pages.searchIdleText} />
  if (!results.length) return <PlaceholderPage icon={SearchIcon} title={t.pages.searchEmptyTitle} text={t.pages.searchEmptyText(query)} />

  return (
    <div className="h-full overflow-y-auto bg-gradient-to-b from-white to-[#f8fbff]">
      <div className="mx-auto max-w-[960px] px-6 py-6">
        <PageHeader icon={SearchIcon} title={t.pages.searchTitle} text={t.pages.searchText(query, results.length)} />
        <div className="grid gap-3">
          {results.map((node) => {
            const growiUrl = growiPageUrl(connection?.url, node.path)
            const evidence = (node.evidence || []).map((e) => e.text).filter(Boolean)
            return (
              <div key={node.id} className="w-full rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
                <div className="flex items-start justify-between gap-3">
                  <button className="min-w-0 text-left hover:text-blue-700" onClick={() => onOpenNode(node.id)}>
                    <h3 className="text-[15px] font-extrabold text-slate-950">{node.title || node.path || node.id}</h3>
                    <div className="mt-0.5 truncate text-[11px] font-bold text-slate-400">{node.path}</div>
                  </button>
                  <div className="flex shrink-0 items-center gap-2">
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        onOpenNode(node.id)
                      }}
                      className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-[12px] font-extrabold text-blue-700"
                    >
                      {t.searchResults.open}
                    </button>
                    {growiUrl && (
                      <a
                        href={growiUrl}
                        target="_blank"
                        rel="noreferrer"
                        onClick={(e) => e.stopPropagation()}
                        className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-3 py-1.5 text-[12px] font-bold text-slate-600 hover:bg-slate-50"
                      >
                        <ExternalLink size={12} /> {t.app.openInGrowi}
                      </a>
                    )}
                  </div>
                </div>
                {evidence.length > 0 && (
                  <ul className="mt-3 space-y-1 text-[13px] leading-6 text-slate-600">
                    {evidence.slice(0, 3).map((text, i) => (
                      <li key={i} className="line-clamp-2">
                        · {text}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

// -----------------------------------------------------------------------------
// Page / answer view (read-only)
// -----------------------------------------------------------------------------

function PageView({ workspace, connection, onOpenNode, t }) {
  const { doc, links = [], citedIds, kind } = workspace
  const link = kind === 'page' ? growiLinkFor(doc, connection) : null

  return (
    <div className="grid h-full w-full grid-rows-[auto_minmax(0,1fr)] bg-white">
      <div className="border-b border-line bg-white px-[26px] pb-[14px] pt-[18px]">
        <div className="mb-[8px] flex flex-wrap items-center gap-2 text-[12px] text-muted">
          <span className="inline-flex items-center gap-[6px] border border-green/20 bg-green/10 px-[8px] py-[5px] font-bold text-[#08785a]">
            {doc.badge || 'GROWI'}
          </span>
          <span className="truncate">{doc.meta}</span>
          {link && (
            <a
              href={link.view}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 font-semibold text-blue-700 hover:underline"
            >
              <ExternalLink size={12} /> {t.app.openInGrowi}
            </a>
          )}
          <button
            className="inline-flex items-center gap-1 font-semibold text-slate-500 hover:text-slate-800"
            onClick={() => downloadMarkdown(doc.title, stripCitedNodeIdsBlocks(doc.markdown || ''), t.app.untitled)}
            title={t.app.download}
          >
            <Download size={13} /> {t.app.download}
          </button>
        </div>
        <div className="truncate text-[27px] font-extrabold tracking-tight text-ink">{doc.title}</div>
      </div>

      <div className="min-h-0 overflow-auto bg-gradient-to-b from-white to-[#fbfdff]">
        <article className="md w-full max-w-none px-[36px] pb-[90px] pt-[36px]">
          <MarkdownRenderer markdown={doc.markdown || ''} onOpenNode={onOpenNode} rawById={{}} referenceLabel={t.app.cited} />

          {citedIds?.length > 0 && (
            <div className="mt-[28px] border-t border-line pt-[14px] text-[13px]">
              <h3 className="mb-2 text-[12px] font-bold uppercase tracking-wider text-muted">{t.app.cited}</h3>
              <div className="flex flex-wrap gap-2">
                {citedIds.map((id) => (
                  <button
                    key={id}
                    onClick={() => onOpenNode(id)}
                    className="rounded-lg border border-slate-200 px-3 py-1.5 font-bold text-blue-700 hover:bg-blue-50"
                  >
                    {id}
                  </button>
                ))}
              </div>
            </div>
          )}

          {links.length > 0 && (
            <div className="mt-[28px] border-t border-line pt-[14px] text-[13px]">
              <h3 className="mb-2 text-[12px] font-bold uppercase tracking-wider text-muted">{t.app.links}</h3>
              <ul className="space-y-1">
                {links.map((l) => (
                  <li key={l.id}>
                    <button
                      className="text-left font-semibold text-blue-700 hover:underline disabled:font-normal disabled:text-slate-400 disabled:no-underline"
                      disabled={!l.target_node_id && !l.target_path}
                      onClick={() => onOpenNode(l.target_node_id || l.target_path)}
                    >
                      {l.label || l.target_path || l.target_node_id}
                      {l.summary ? <span className="font-normal text-slate-400"> — {l.summary}</span> : null}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </article>
      </div>
    </div>
  )
}

// -----------------------------------------------------------------------------
// Chat
// -----------------------------------------------------------------------------

function ChatArea({ chat, t, onViewAnswer }) {
  const [question, setQuestion] = useState('')
  const endRef = useRef(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chat.messages])

  const submit = () => {
    const clean = question.trim()
    if (!clean || chat.agentRunning) return
    chat.ask(clean)
    setQuestion('')
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-gradient-to-b from-white to-[#f8fbff] px-6 pt-6">
      <div className="min-h-0 flex-1 overflow-y-auto pb-4">
        {chat.messages.length === 0 && (
          <div className="mx-auto mt-10 max-w-[520px] rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-10 text-center shadow-sm">
            <h2 className="text-[20px] font-extrabold">{t.app.chatTitle}</h2>
            <p className="mt-2 text-[14px] leading-6 text-slate-500">{t.app.chatText}</p>
          </div>
        )}

        <div className="w-full space-y-4">
          {chat.messages.map((m, i) =>
            m.role === 'user' ? (
              <div key={i} className="flex justify-end">
                <div className="max-w-[78%] whitespace-pre-wrap rounded-2xl border border-blue-100 bg-blue-50 px-4 py-3 text-[14px] font-semibold">
                  {m.text}
                </div>
              </div>
            ) : (
              <div key={i} className="w-full rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
                <div className="mb-2 text-[12px] font-bold text-slate-400">{m.title || t.working}</div>

                {m.activity?.length > 0 && (
                  <ul className="mb-3 list-none space-y-1 border-l-2 border-slate-100 pl-3 text-[12.5px] text-slate-500">
                    {m.activity.map((line, idx) => (
                      <li key={idx} className={m.streaming && idx === m.activity.length - 1 ? 'text-slate-900' : ''}>
                        {line}
                      </li>
                    ))}
                  </ul>
                )}

                {m.error && <div className="text-[14px] text-red-600">{m.text}</div>}
                {!m.error && m.emptyText && <div className="text-[14px] text-slate-500">{m.emptyText}</div>}
                {m.markdown && (
                  <>
                    <article className="md max-w-none text-[14px]">
                      <MarkdownRenderer markdown={m.markdown} onOpenNode={null} rawById={{}} referenceLabel={t.app.cited} />
                    </article>
                    {m.answer && (
                      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
                        <button
                          onClick={() => onViewAnswer(m.answer)}
                          className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-[12px] font-extrabold text-blue-700"
                        >
                          {t.app.viewFull}
                        </button>
                        {(m.answer.citedIds || []).slice(0, 8).map((id) => (
                          <button
                            key={id}
                            onClick={() => onViewAnswer(m.answer)}
                            className="rounded border border-slate-200 px-2 py-1 text-[11px] text-slate-500 hover:text-blue-700"
                            title={id}
                          >
                            {id.slice(0, 8)}…
                          </button>
                        ))}
                      </div>
                    )}
                  </>
                )}
              </div>
            ),
          )}
        </div>
        <div ref={endRef} />
      </div>

      <div className="shrink-0 border-t border-blue-100/70 px-0 pb-4 pt-3">
        <div className="flex min-h-[58px] items-end gap-3 rounded-2xl border border-blue-100 bg-blue-50/80 px-4 py-3 focus-within:border-blue-400">
          <textarea
            rows={1}
            className="max-h-[180px] min-h-[34px] min-w-0 flex-1 resize-none bg-transparent py-1 text-[14px] outline-none"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                e.preventDefault()
                submit()
              }
            }}
            placeholder={t.app.askPlaceholder}
          />
          {chat.agentRunning ? (
            <button
              className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-red-600 text-white disabled:opacity-50"
              onClick={chat.stopAgent}
              disabled={!chat.agentRunId || chat.agentStopping}
              title={chat.agentStopping ? t.app.stopping : t.app.stop}
            >
              {chat.agentStopping ? <Loader2 size={18} className="animate-spin" /> : <Square size={16} fill="currentColor" />}
            </button>
          ) : (
            <button
              className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-600 text-white disabled:opacity-50"
              onClick={submit}
              disabled={!question.trim()}
              title={t.app.ask}
            >
              <Send size={18} />
            </button>
          )}
        </div>
        <p className="mt-2 text-center text-[11px] text-slate-400">{t.app.disclaimer}</p>
      </div>
    </div>
  )
}

// -----------------------------------------------------------------------------
// Settings: per-request LLM overrides (sessionStorage; key never persisted)
// -----------------------------------------------------------------------------

const FIELDS = [
  ['chat_base_url', 'API Base URL', 'text'],
  ['chat_model', 'Model', 'text'],
  ['chat_temperature', 'Temperature', 'number'],
  ['subagent_count', 'Subagents', 'number'],
  ['subagent_concurrency', 'Subagent concurrency', 'number'],
  ['agent_max_steps', 'Agent max steps', 'number'],
]

function SettingsCenterNew({ persisted, apiKey, onApply, onClear, fireToast, t }) {
  const [values, setValues] = useState(() => ({ chat_base_url: '', chat_model: '', chat_temperature: '', subagent_count: '', subagent_concurrency: '', agent_max_steps: '', ...persisted }))
  const [key, setKey] = useState(apiKey ? '********' : '')

  const apply = () => {
    const out = {}
    for (const [name] of FIELDS) {
      const v = values[name]
      if (v !== '' && v !== null) out[name] = v
    }
    onApply({ values: out, key: key && key !== '********' ? key : undefined })
    fireToast(t.app.settingsApplied)
  }

  return (
    <div className="h-full overflow-y-auto bg-gradient-to-b from-white to-[#f8fbff]">
      <div className="mx-auto max-w-[720px] px-6 py-6">
        <PageHeader icon={SettingsIcon} title={t.pages.settingsTitle} text={t.app.settingsText} />

        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="grid gap-3 sm:grid-cols-2">
            {FIELDS.map(([name, label, type]) => (
              <label key={name} className="text-[12px] font-bold text-slate-500">
                {label}
                <input
                  type={type}
                  value={values[name] ?? ''}
                  onChange={(e) => setValues((v) => ({ ...v, [name]: e.target.value }))}
                  className="mt-1 h-10 w-full rounded-lg border border-slate-200 px-3 text-[14px] font-medium outline-none focus:border-blue-400"
                />
              </label>
            ))}
            <label className="text-[12px] font-bold text-slate-500 sm:col-span-2">
              API Key
              <input
                type="password"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                placeholder={apiKey ? '••••••••' : ''}
                className="mt-1 h-10 w-full rounded-lg border border-slate-200 px-3 text-[14px] font-medium outline-none focus:border-blue-400"
              />
            </label>
          </div>

          <p className="mt-3 text-[12px] text-slate-400">{t.app.settingsNote}</p>

          <div className="mt-4 flex gap-2">
            <button onClick={apply} className="rounded-lg bg-blue-600 px-4 py-2 text-[13px] font-extrabold text-white hover:bg-blue-700">
              {t.app.settingsApply}
            </button>
            <button
              onClick={() => {
                onClear()
                setValues({})
                setKey('')
                fireToast(t.app.settingsCleared)
              }}
              className="rounded-lg border border-slate-200 px-4 py-2 text-[13px] font-bold text-slate-600 hover:bg-slate-50"
            >
              {t.app.settingsClear}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
