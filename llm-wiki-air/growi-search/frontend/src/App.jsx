import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import ChatPanel from './components/ChatPanel'
import { DocumentCenter } from './components/layout/DocumentCenter'
import MarkdownView from './components/MarkdownView'
import ErrorBoundary from './components/ErrorBoundary'
import SettingsView from './components/SettingsView'
import { AppFooter } from './components/layout/AppFooter'
import { LeftSidebar } from './components/layout/LeftSidebar'
import { MarkdownWorkspaceFrame } from './components/layout/MarkdownWorkspaceFrame'
import { RightDocumentRail } from './components/layout/RightDocumentRail'
import { SearchResultsCenter } from './components/layout/SearchResults'
import { SettingsCenter } from './components/layout/SettingsCenter'
import { STR } from './components/layout/strings.js'
import { TopBar } from './components/layout/TopBar'
import { Centered } from './components/layout/Shell'
import { useT } from './i18n.jsx'
import { useAskStream } from './hooks/useAskStream'
import { useOverrides } from './hooks/useOverrides'
import { useSearch } from './hooks/useSearch'
import { useWorkspace } from './hooks/useWorkspace'
import { parentOf, useWikiData } from './hooks/useWikiData'
import { api } from './api'

export default function App() {
  const t = useT(STR)

  /**
   * Shell state.
   *
   * centerView (inside useWorkspace) controls the main center area.
   * No tab bar anymore.
   */
  const [leftCollapsed, setLeftCollapsed] = useState(false)
  const [rightOpen, setRightOpen] = useState(true)
  const [rightTabs, setRightTabs] = useState([])
  const [activeRightTabId, setActiveRightTabId] = useState('explorer')

  const [activeAnswerId, setActiveAnswerId] = useState(null)
  const [answerMentionedIdsByAnswerId, setAnswerMentionedIdsByAnswerId] = useState(() => new Map())
  const [, setFocusIds] = useState(null)
  const [toast, setToast] = useState(null)
  const [growiConnection, setGrowiConnection] = useState(null)
  const [growiError, setGrowiError] = useState(null)
  const answerSeq = useRef(0)

  const fireToast = useCallback((text) => {
    setToast(text)
    setTimeout(() => setToast(null), 3600)
  }, [])

  const { overrides, applyOverrides } = useOverrides()
  const loadGrowi = useCallback(() => {
    setGrowiError(null)
    api.growi().then(setGrowiConnection).catch((e) => setGrowiError(String(e.message || e)))
  }, [])
  useEffect(() => {
    loadGrowi()
  }, [loadGrowi])
  const wiki = useWikiData({ rootPath: growiConnection?.root_path, fireToast, t })
  const { rawById, loading, error, retry } = wiki
  const errorRetryable = true
  const ws = useWorkspace({ t, fireToast, setFocusIds, rememberNodes: wiki.rememberNodes })
  const {
    workspace,
    setWorkspace,
    centerView,
    setCenterView,
    centerHistory,
    closeWorkspace,
    openWorkspace,
    goBackFromWorkspace,
    openNodeById,
    openSearchResult,
    openDocument,
  } = ws

  const search = useSearch({ t, fireToast, setCenterView })

  useEffect(() => {
    const path = workspace?.kind === 'doc' ? workspace.node?.path : null
    if (path && !wiki.childrenByPath[parentOf(path)]) wiki.load(parentOf(path))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace?.id])

  const [prevNodeId, nextNodeId] = useMemo(() => {
    if (workspace?.kind !== 'doc') return [null, null]
    const siblings = wiki.siblingsOf(workspace.node)
    const i = siblings.findIndex((p) => p.id === workspace.nodeId)
    return [i > 0 ? siblings[i - 1].id : null, i >= 0 && i < siblings.length - 1 ? siblings[i + 1].id : null]
  }, [workspace, wiki])

  const chat = useAskStream({
    t,
    overrides,
    fireToast,
    onAskStart: () => setCenterView('chat'),
    onAnswer: (ans, activity, q) => finalizeAnswer(ans, activity, q),
  })

  // ---------------------------------------------------------------------------
  // Answers: chat ↔ workspace ↔ right-rail glue
  // ---------------------------------------------------------------------------

  /**
   * Answers are stored as the current workspace, but by default we do not
   * force navigation away from chat when an answer arrives.
   *
   * - Generated answer: stays in chat.
   * - User clicks "view answer": opens full markdown workspace.
   */
  const openAnswerTab = (answer, expand = true) => {
    if (!answer) return

    const id = `answer:${answer.id}`
    const title = answer.title || answer.question || t.answer
    const markdown = answer.markdown || ''

    const next = {
      id,
      kind: 'answer',
      title: title.slice(0, 28),
      sourceType: 'exogenous',
      sourceIds: answer.citedIds || [],
      sourceName: title,
      answer,
      doc: {
        title,
        badge: t.agentNote,
        meta: answer.steps ? t.answerMetaSteps(answer.steps) : t.answerMeta,
        markdown,
      },
      refs: answer.refs || [],
    }

    setActiveAnswerId(answer.id)
    setFocusIds(new Set(answer.citedIds || []))

    if (expand) {
      openWorkspace(next)
    } else {
      setWorkspace(next)
    }
  }

  const openAnswerSourcesTab = (answer) => {
    const citedIds = Array.isArray(answer?.citedIds) ? answer.citedIds : []
    const refs = Array.isArray(answer?.refs) ? answer.refs : []

    if (!answer || (citedIds.length === 0 && refs.length === 0)) return false

    const id = `sources:${answer.id}`
    const title = answer.title || answer.question || t.answer

    setRightTabs((prev) => {
      const nextTab = {
        id,
        kind: 'sources',
        title: title.slice(0, 26),
        answer,
      }
      const existing = prev.findIndex((tab) => tab.id === id)

      if (existing === -1) {
        return [...prev, nextTab]
      }

      const next = prev.slice()
      next[existing] = nextTab
      return next
    })

    setActiveRightTabId(id)
    setRightOpen(true)
    setActiveAnswerId(answer.id)
    setFocusIds(new Set(citedIds))

    return true
  }

  const closeRightTab = (id) => {
    if (id === 'explorer') return

    setRightTabs((prev) => prev.filter((tab) => tab.id !== id))
    setActiveRightTabId((current) => (current === id ? 'explorer' : current))
  }

  const finalizeAnswer = (ans, activity, q) => {
    const cited = ans.cited_node_ids || []
    const citedNodes = Array.isArray(ans.cited_nodes) ? ans.cited_nodes : []
    wiki.rememberNodes(citedNodes)
    const byId = new Map([
      ...rawById,
      ...citedNodes.filter((n) => n?.id).map((n) => [n.id, n]),
    ])
    const refs = cited.map((id) => {
      const n = byId.get(id)
      return { id, label: n?.title || id, note: n?.summary || n?.path || t.sourceNote }
    })
    const hasAnswer = !!(ans.answer && ans.answer.trim())

    setFocusIds(new Set(cited))

    if (hasAnswer) {
      answerSeq.current += 1

      const id = answerSeq.current

      const answer = {
        id,
        question: q,
        title: q,
        markdown: ans.answer,
        refs,
        steps: ans.steps,
        citedIds: cited,
      }

      // Keep map/visitedIds collected while streaming.
      chat.patchLast((m) => ({
        ...m,
        streaming: false,
        role: 'assistant',
        title: t.answerReady(ans.steps),
        text: ans.answer,
        activity,
        answer,
      }))

      // Store as workspace data, but do not force full markdown view.
      openAnswerTab(answer, false)
      openAnswerSourcesTab(answer)
    } else {
      if (cited.length) {
        answerSeq.current += 1
      }

      const sourceOnlyAnswer = cited.length
        ? {
            id: answerSeq.current,
            question: q,
            title: q,
            markdown: '',
            refs,
            steps: ans.steps,
            citedIds: cited,
          }
        : null

      chat.patchLast((m) => ({
        ...m,
        streaming: false,
        role: 'assistant',
        title: t.foundNoBody,
        text: t.foundNoBodyText(cited.length, ans.steps),
        refs,
        activity,
      }))

      if (sourceOnlyAnswer) {
        openAnswerSourcesTab(sourceOnlyAnswer)
      } else if (cited[0]) {
        openNodeById(cited[0])
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Navigation
  // ---------------------------------------------------------------------------

  const handleNav = (view) => {
    if (view === 'explorer') {
      setActiveRightTabId('explorer')
      setRightOpen(true)
      return
    }

    setCenterView(view)
  }

  const handleNewChat = () => {
    chat.resetChat()
    setWorkspace(null)
    setActiveAnswerId(null)
    setRightTabs([])
    setAnswerMentionedIdsByAnswerId(new Map())
    setActiveRightTabId('explorer')
    setCenterView('chat')
  }

  const handleAnswerMentionedIds = useCallback((answerId, ids) => {
    if (!answerId) return

    const cleanIds = Array.from(
      new Set(Array.isArray(ids) ? ids.filter(Boolean) : []),
    )

    setAnswerMentionedIdsByAnswerId((prev) => {
      const prevIds = prev.get(answerId) || []
      const prevKey = prevIds.join('|')
      const nextKey = cleanIds.join('|')

      if (prevKey === nextKey) return prev

      const next = new Map(prev)
      next.set(answerId, cleanIds)
      return next
    })
  }, [])

  const renderCenter = () => {
    if (loading && !growiError) {
      return <Centered>{t.loadingGraph}</Centered>
    }

    if (error || growiError) {
      return (
        <Centered>
          <div className="max-w-[420px] text-center">
            <p className="font-bold text-red">{t.cannotReach}</p>
            <p className="mt-2 text-[13px] text-muted">{growiError || error}</p>
            {errorRetryable && (
              <button
                type="button"
                onClick={growiError ? loadGrowi : retry}
                className="mt-4 border border-line bg-white px-[13px] py-[8px] text-[13px] font-bold text-neutral-700 hover:border-line2"
              >
                {t.retry}
              </button>
            )}
          </div>
        </Centered>
      )
    }

    if (centerView === 'search') {
      return (
        <SearchResultsCenter
          query={search.searchQuery}
          results={search.searchResults}
          loading={search.searchLoading}
          connection={growiConnection}
          onOpenNode={openSearchResult}
        />
      )
    }

    if (centerView === 'document' && workspace?.kind === 'document') {
      return (
        <MarkdownWorkspaceFrame item={workspace} canGoBack={centerHistory.length > 0} onBack={goBackFromWorkspace} onClose={closeWorkspace}>
          <DocumentCenter path={workspace.path} connection={growiConnection} onOpenNode={openSearchResult} />
        </MarkdownWorkspaceFrame>
      )
    }

    if (centerView === 'settings') {
      return (
        <SettingsCenter>
          <SettingsView overrides={overrides} onApply={applyOverrides} />
        </SettingsCenter>
      )
    }

    if (centerView === 'markdown' && workspace) {
      return (
        <MarkdownWorkspaceFrame
          item={workspace}
          canGoBack={centerHistory.length > 0}
          onBack={goBackFromWorkspace}
          onClose={closeWorkspace}
        >
          <MarkdownView
            doc={workspace.doc}
            mode={workspace.kind}
            rawById={rawById}
            growiConnection={growiConnection}
            prevNodeId={prevNodeId}
            nextNodeId={nextNodeId}
            onOpenNode={openNodeById}
          />
        </MarkdownWorkspaceFrame>
      )
    }

    return (
      <div className="flex h-full min-h-0 flex-col overflow-hidden bg-white">
        <div className="flex h-full min-h-0 w-full flex-col px-0 pt-6 pb-0">
          <div className="flex min-h-0 flex-1 flex-col">
            <ChatPanel
              messages={chat.messages}
              onAsk={chat.ask}
              onOpenNode={openNodeById}
              onViewAnswer={(answer) => openAnswerTab(answer, true)}
              activeAnswerId={activeAnswerId}
              agentRunning={chat.agentRunning}
              agentCanStop={!!chat.agentRunId}
              agentStopping={chat.agentStopping}
              onStopAgent={chat.stopAgent}
              rawById={rawById}
              onAnswerMentionedIds={handleAnswerMentionedIds}
            />
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-white text-neutral-900">
      <LeftSidebar
        collapsed={leftCollapsed}
        activeView={centerView}
        activeRightTabId={activeRightTabId}
        rightOpen={rightOpen}
        recentQuestions={chat.recentQuestions}
        onToggle={() => setLeftCollapsed((v) => !v)}
        onNavigate={handleNav}
        onNewChat={handleNewChat}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar
          onSearch={search.onSearch}
          onSearchResults={search.showResults}
          rightOpen={rightOpen}
          onToggleRight={() => setRightOpen((v) => !v)}
          rootPath={growiConnection?.root_path}
        />

        <div className="relative flex min-h-0 flex-1 overflow-hidden">
          <main className="relative min-w-0 flex-1 overflow-hidden bg-white">
            <ErrorBoundary
              resetKey={`${centerView}:${workspace?.id || 'none'}:${search.searchQuery}`}
            >
              {renderCenter()}
            </ErrorBoundary>

            {toast && (
              <div className="absolute bottom-[22px] right-[22px] z-30 max-w-[390px] rounded-xl border border-blue-200 bg-blue-50 px-[14px] py-[13px] text-[13px] leading-[1.45] text-blue-800 shadow-xl">
                {toast}
              </div>
            )}

          </main>

          {rightOpen && (
            <RightDocumentRail
              wiki={wiki}
              rootPath={growiConnection?.root_path || '/'}
              workspace={workspace}
              tabs={rightTabs}
              activeTabId={activeRightTabId}
              onActivateTab={setActiveRightTabId}
              onCloseTab={closeRightTab}
              onOpenNode={(n) => openNodeById(n.id)}
              onOpenDocument={openDocument}
              rawById={rawById}
              onViewAnswer={(answer) => openAnswerTab(answer, true)}
              mentionedNodeIdsByAnswerId={answerMentionedIdsByAnswerId}
              onClose={() => setRightOpen(false)}
            />
          )}
        </div>

        <AppFooter />
      </div>
    </div>
  )
}
