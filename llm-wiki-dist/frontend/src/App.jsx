import { useCallback, useMemo, useRef, useState } from 'react'
import { BookMarked } from 'lucide-react'

import ChatPanel from './components/ChatPanel'
import GraphCanvas from './components/GraphCanvas'
import MarkdownView from './components/MarkdownView'
import ErrorBoundary from './components/ErrorBoundary'
import QueueView from './components/QueueView'
import SettingsView from './components/SettingsView'
import UploadView from './components/UploadView'
import { AppFooter } from './components/layout/AppFooter'
import { LeftSidebar } from './components/layout/LeftSidebar'
import { MarkdownWorkspaceFrame } from './components/layout/MarkdownWorkspaceFrame'
import { RightDocumentRail } from './components/layout/RightDocumentRail'
import { SearchResultsCenter } from './components/layout/SearchResults'
import { SettingsCenter } from './components/layout/SettingsCenter'
import { STR } from './components/layout/strings.js'
import { TopBar } from './components/layout/TopBar'
import { UploadCenter } from './components/layout/UploadCenter'
import { Centered, PlaceholderPage } from './components/layout/Shell'
import { useT } from './i18n.jsx'
import { useAskStream } from './hooks/useAskStream'
import { useAssimilation } from './hooks/useAssimilation'
import { useGraphData } from './hooks/useGraphData'
import { useGraphWrites } from './hooks/useGraphWrites'
import { useOverrides } from './hooks/useOverrides'
import { useSearch } from './hooks/useSearch'
import { useWorkspace } from './hooks/useWorkspace'

const pdfApiBase = import.meta.env.VITE_PDF_API_URL || 'http://10.160.144.101:51023'

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
  const [rightMode, setRightMode] = useState('documents') // documents | topics
  const [rightTabs, setRightTabs] = useState([])
  const [activeRightTabId, setActiveRightTabId] = useState('explorer')

  const [activeAnswerId, setActiveAnswerId] = useState(null)
  const [answerMentionedIdsByAnswerId, setAnswerMentionedIdsByAnswerId] = useState(() => new Map())
  const [focusIds, setFocusIds] = useState(null)
  const [toast, setToast] = useState(null)
  const answerSeq = useRef(0)

  const fireToast = (text) => {
    setToast(text)
    setTimeout(() => setToast(null), 3600)
  }

  const { overrides, applyOverrides } = useOverrides()
  const { rawById, graph, docLibrary, health, loading, error, errorRetryable, reload, retry } =
    useGraphData()
  const { assimPending, startAssimilationPolling } = useAssimilation()

  const ws = useWorkspace({ t, fireToast, setFocusIds })
  const {
    workspace,
    setWorkspace,
    centerView,
    setCenterView,
    centerHistory,
    updateWorkspace,
    closeWorkspace,
    openWorkspace,
    goBackFromWorkspace,
    openNode,
    openNodeById,
    openSearchResult,
    openFullDoc,
    openDraft,
    startEdit,
    cancelEdit,
    changeTitle,
    changeBody,
  } = ws

  const search = useSearch({ t, fireToast, setCenterView })

  const writes = useGraphWrites({
    t,
    fireToast,
    reload,
    startAssimilationPolling,
    workspace,
    setWorkspace,
    updateWorkspace,
    closeWorkspace,
    openNode,
    setFocusIds,
  })

  const chat = useAskStream({
    t,
    overrides,
    fireToast,
    onAskStart: () => setCenterView('chat'),
    onAnswer: (ans, activity, q) => finalizeAnswer(ans, activity, q),
  })

  const openIds = useMemo(() => {
    if (workspace?.kind === 'doc' && workspace.nodeId) {
      return new Set([workspace.nodeId])
    }

    return new Set()
  }, [workspace])

  const activeRightTab = useMemo(() => {
    return rightTabs.find((tab) => tab.id === activeRightTabId) || null
  }, [rightTabs, activeRightTabId])

  const activeSourceIds = useMemo(() => {
    if (activeRightTab?.kind === 'sources') {
      return new Set(activeRightTab.answer?.citedIds || [])
    }

    if (workspace?.kind === 'answer') {
      return new Set(workspace.answer?.citedIds || [])
    }

    return new Set()
  }, [activeRightTab, workspace])

  // ---------------------------------------------------------------------------
  // Answers: chat ↔ workspace ↔ right-rail glue
  // ---------------------------------------------------------------------------

  const refLabel = (id) => {
    const n = rawById.get(id)

    return {
      id,
      label: n?.title || n?.entity || id,
      note: n?.type === 'exogenous' ? t.agentNote : t.sourceNote,
    }
  }

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
      draft: {
        title,
        markdown,
      },
      editing: false,
      busy: false,
      busyMessage: '',
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
    const refs = cited.map(refLabel)
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

      chat.patchLast(() => ({
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

      chat.patchLast(() => ({
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

    if (view === 'documents') {
      setActiveRightTabId('explorer')
      setRightMode('documents')
      setRightOpen(true)
      return
    }

    if (view === 'topics') {
      setActiveRightTabId('explorer')
      setRightMode('topics')
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
    if (loading) {
      return <Centered>{t.loadingGraph}</Centered>
    }

    if (error) {
      return (
        <Centered>
          <div className="max-w-[420px] text-center">
            <p className="font-bold text-red">{t.cannotReach}</p>
            <p className="mt-2 text-[13px] text-muted">{error}</p>
            {errorRetryable && (
              <button
                type="button"
                onClick={retry}
                className="mt-4 border border-line bg-white px-[13px] py-[8px] text-[13px] font-bold text-slate-700 hover:border-line2"
              >
                再試行
              </button>
            )}
          </div>
        </Centered>
      )
    }

    if (centerView === 'graph') {
      return (
        <div className="h-full bg-white">
          <GraphCanvas
            nodes={graph.nodes}
            edges={graph.edges}
            clusters={graph.clusters}
            worldW={graph.worldW}
            worldH={graph.worldH}
            openIds={openIds}
            answerIds={activeSourceIds}
            showConflict={false}
            showStale={false}
            onOpenNode={(n) => openNodeById(n.id)}
          />
        </div>
      )
    }

    if (centerView === 'search') {
      return (
        <SearchResultsCenter
          query={search.searchQuery}
          results={search.searchResults}
          loading={search.searchLoading}
          onOpenNode={openSearchResult}
        />
      )
    }

    if (centerView === 'upload') {
      return (
        <UploadCenter>
          <UploadView
            pdfApiBase={pdfApiBase}
            onOpenDraft={openDraft}
            onUploadMarkdown={writes.uploadMarkdown}
          />
        </UploadCenter>
      )
    }

    if (centerView === 'queue') {
      return <QueueView />
    }

    if (centerView === 'glossary') {
      return (
        <PlaceholderPage
          icon={BookMarked}
          title={t.pages.glossaryTitle}
          text={t.pages.glossaryText}
        />
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
            draft={workspace.draft}
            isEditing={!!workspace.editing}
            dirty={
              workspace.kind !== 'fulldoc' &&
              (workspace.draft.markdown !== workspace.doc.markdown ||
                workspace.draft.title !== workspace.doc.title)
            }
            mode={workspace.kind}
            positions={workspace.positions}
            busy={!!workspace.busy}
            busyMessage={workspace.busyMessage}
            refs={workspace.refs}
            rawById={rawById}
            prevNodeId={
              workspace.kind === 'doc'
                ? docLibrary?.prevOf?.get?.(workspace.nodeId) || null
                : null
            }
            nextNodeId={
              workspace.kind === 'doc'
                ? docLibrary?.nextOf?.get?.(workspace.nodeId) || null
                : null
            }
            onStartEdit={() => startEdit(workspace.id)}
            onCancelEdit={() => cancelEdit(workspace.id)}
            onConfirm={() => {
              if (workspace.kind === 'doc') {
                writes.saveNode(workspace)
              } else {
                writes.addDraftToGraph(workspace)
              }
            }}
            onDelete={() => {
              if (workspace.kind === 'doc') {
                writes.deleteNode(workspace)
              } else {
                closeWorkspace()
              }
            }}
            onAddToGraph={() => writes.addDraftToGraph(workspace)}
            onOpenNode={openNodeById}
            onChangeTitle={(v) => changeTitle(workspace.id, v)}
            onChangeBody={(v) => changeBody(workspace.id, v)}
          />
        </MarkdownWorkspaceFrame>
      )
    }

    return (
      <div className="flex h-full min-h-0 flex-col overflow-hidden bg-gradient-to-b from-white to-[#f8fbff]">
        <div className="flex h-full min-h-0 w-full flex-col px-0 pt-6 pb-0">
          <div className="flex min-h-0 flex-1 flex-col">
            <ChatPanel
              messages={chat.messages}
              health={health}
              onAsk={chat.ask}
              onOpenNode={openNodeById}
              onAddWiki={writes.addWiki}
              onViewAnswer={(answer) => openAnswerTab(answer, true)}
              activeAnswerId={activeAnswerId}
              savedIds={writes.savedIds}
              addingIds={writes.addingIds}
              writeStatuses={writes.answerWriteStatuses}
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
    <div className="flex h-screen w-screen overflow-hidden bg-[#f6f8fc] text-slate-900">
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
        />

        <div className="relative flex min-h-0 flex-1 overflow-hidden">
          <main className="relative min-w-0 flex-1 overflow-hidden bg-white">
            <ErrorBoundary
              resetKey={`${centerView}:${workspace?.id || 'none'}:${search.searchQuery}`}
            >
              {renderCenter()}
            </ErrorBoundary>

            {toast && (
              <div className="absolute bottom-[22px] right-[22px] z-30 max-w-[390px] rounded-xl border border-emerald-200 bg-emerald-50 px-[14px] py-[13px] text-[13px] leading-[1.45] text-emerald-800 shadow-xl">
                {toast}
              </div>
            )}

            {assimPending > 0 && (
              <div
                title={t.assimTitle}
                className="absolute bottom-[22px] left-[22px] z-30 flex items-center gap-2 rounded-full border border-blue-200 bg-blue-50 px-[12px] py-[7px] text-[12px] font-medium text-blue-800 shadow-lg"
              >
                <span className="h-[7px] w-[7px] animate-pulse rounded-full bg-blue-600" />
                {t.assimBadge} {assimPending}
              </div>
            )}
          </main>

          {rightOpen && (
            <RightDocumentRail
              mode={rightMode}
              library={docLibrary}
              workspace={workspace}
              tabs={rightTabs}
              activeTabId={activeRightTabId}
              onActivateTab={setActiveRightTabId}
              onCloseTab={closeRightTab}
              onModeChange={setRightMode}
              onOpenNode={openNode}
              onOpenFullDoc={openFullDoc}
              onDeleteDocument={writes.deleteDocument}
              deletingDocs={writes.deletingDocs}
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
