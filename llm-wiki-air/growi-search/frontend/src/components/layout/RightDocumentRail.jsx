import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  ChevronsLeftRight,
  FileText,
  FolderTree,
  MessagesSquare,
  PlusCircle,
  Settings,
  Trash2,
  X,
} from 'lucide-react'

import { faviconUrl } from '../../data/utils'
import { useT } from '../../i18n.jsx'
import DocSidebar from '../DocSidebar'
import { STR } from './strings.js'

const RIGHT_RAIL_STR = {
  ja: {
    ...(STR.ja || {}),
    rightRail: {
      ...((STR.ja || {}).rightRail || {}),
      mentionedInAnswer: '回答内で言及',
      mentionedCount: (count) => `${count} 件の言及`,
      resizeSidebar: '知識サイドバーの幅を変更',
      dragToResize: 'ドラッグして幅を変更',
    },
  },
  en: {
    ...(STR.en || {}),
    rightRail: {
      ...((STR.en || {}).rightRail || {}),
      mentionedInAnswer: 'Mentioned in answer',
      mentionedCount: (count) => `${count} mentioned`,
      resizeSidebar: 'Resize knowledge sidebar',
      dragToResize: 'Drag to resize',
    },
  },
}

const DEFAULT_RAIL_WIDTH = 440
const MIN_RAIL_WIDTH = 76
const COLLAPSE_RAIL_WIDTH = 180

export function RightDocumentRail({
  wiki,
  rootPath,
  workspace,
  tabs,
  activeTabId,
  onActivateTab,
  onCloseTab,
  onOpenNode,
  onOpenDocument,
  rawById,
  onViewAnswer,
  onNewChat,
  onOpenSettings,
  settingsActive,
  chats,
  activeChatId,
  onOpenChat,
  onDeleteChat,
  onClearChats,
  // Map(answer.id -> Array<string>)
  mentionedNodeIdsByAnswerId,
}) {
  const t = useT(RIGHT_RAIL_STR)
  const railRef = useRef(null)
  const actionsRef = useRef(null)
  const [railWidth, setRailWidth] = useState(() => clampRailWidth(DEFAULT_RAIL_WIDTH))
  const [resizing, setResizing] = useState(false)
  const collapsed = railWidth < COLLAPSE_RAIL_WIDTH

  useLayoutEffect(() => {
    const actions = actionsRef.current
    const shell = railRef.current?.parentElement
    if (!actions || !shell) return undefined

    const syncHeight = () => {
      shell.style.setProperty(
        '--bottom-controls-height',
        `${actions.getBoundingClientRect().height}px`,
      )
    }
    const observer = new ResizeObserver(syncHeight)
    syncHeight()
    observer.observe(actions)

    return () => {
      observer.disconnect()
      shell.style.removeProperty('--bottom-controls-height')
    }
  }, [collapsed])

  useEffect(() => {
    if (!resizing) return undefined

    const stop = () => setResizing(false)
    const move = (event) => {
      setRailWidth(clampRailWidth(event.clientX))
    }

    const previousCursor = document.body.style.cursor
    const previousUserSelect = document.body.style.userSelect
    document.body.style.cursor = 'ew-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', stop)
    window.addEventListener('pointercancel', stop)

    return () => {
      document.body.style.cursor = previousCursor
      document.body.style.userSelect = previousUserSelect
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', stop)
      window.removeEventListener('pointercancel', stop)
    }
  }, [resizing])

  useEffect(() => {
    const fitToViewport = () => setRailWidth((width) => clampRailWidth(width))
    window.addEventListener('resize', fitToViewport)
    return () => window.removeEventListener('resize', fitToViewport)
  }, [])

  const startResize = (event) => {
    if (event.button !== 0) return
    event.preventDefault()
    setResizing(true)
  }

  const resizeWithKeyboard = (event) => {
    const delta = event.key === 'ArrowRight' ? 24 : event.key === 'ArrowLeft' ? -24 : 0
    if (!delta && event.key !== 'Home' && event.key !== 'End') return

    event.preventDefault()
    setRailWidth((width) => {
      if (event.key === 'Home') return clampRailWidth(MIN_RAIL_WIDTH)
      if (event.key === 'End') return maxRailWidth()
      return clampRailWidth(width + delta)
    })
  }

  const allTabs = [
    {
      id: 'chats',
      kind: 'chats',
      title: t.rightRail.chatsTab,
    },
    {
      id: 'explorer',
      kind: 'explorer',
      title: t.rightRail.explorerTab,
    },
    ...(Array.isArray(tabs) ? tabs : []),
  ]

  const activeTab =
    allTabs.find((tab) => tab.id === activeTabId) ||
    allTabs[0]

  const mentionedIdsForActiveAnswer =
    activeTab.kind === 'sources'
      ? getMentionedIdsForAnswer(mentionedNodeIdsByAnswerId, activeTab.answer?.id)
      : []

  return (
    <aside
      ref={railRef}
      style={{ width: railWidth }}
      className="relative flex h-full min-h-0 shrink-0 flex-col border-r border-line bg-white"
    >
      <div className="border-b border-neutral-100 px-3 py-4">
        <div className={`flex items-center gap-3 ${collapsed ? 'justify-center' : ''}`}>
          <div className="flex w-full justify-center">
            <img
              src={faviconUrl()}
              alt="Logo"
              className="block h-[100px] w-[100px] max-w-full object-contain"
            />
          </div>
        </div>
      </div>

      <div
        role="separator"
        tabIndex={0}
        aria-label={t.rightRail.resizeSidebar}
        aria-orientation="vertical"
        aria-valuemin={MIN_RAIL_WIDTH}
        aria-valuemax={maxRailWidth()}
        aria-valuenow={Math.round(railWidth)}
        onPointerDown={startResize}
        onKeyDown={resizeWithKeyboard}
        className={`group absolute -right-[5px] top-0 z-30 flex h-full w-[10px] cursor-ew-resize items-center justify-center ${resizing ? 'bg-blue-50/70' : 'hover:bg-neutral-50'}`}
        title={t.rightRail.dragToResize}
      >
        <span className="h-10 w-px bg-neutral-300" />
        <ChevronsLeftRight
          size={16}
          className="absolute rounded bg-white text-neutral-500 opacity-0 shadow-sm transition-opacity group-hover:opacity-100 group-focus:opacity-100"
        />
      </div>

      {!collapsed && <div className="flex h-[46px] shrink-0 items-center gap-1 overflow-x-auto border-b border-line bg-white px-2">
        {allTabs.map((tab) => {
          const active = tab.id === activeTab.id
          const closable = tab.kind === 'sources'

          return (
            <div
              key={tab.id}
              className={`group flex h-8 min-w-0 shrink-0 items-center rounded-lg border text-[12px] font-semibold transition ${
                active
                  ? 'border-blue-200 bg-blue-50 text-blue-700'
                  : 'border-transparent bg-white text-neutral-500 hover:bg-neutral-50 hover:text-neutral-900'
              }`}
            >
              <button
                type="button"
                onClick={() => onActivateTab?.(tab.id)}
                className="flex h-full min-w-0 items-center gap-1.5 px-2"
                title={tab.title}
              >
                {tab.kind === 'chats' ? (
                  <MessagesSquare size={14} />
                ) : tab.kind === 'explorer' ? (
                  <FolderTree size={14} />
                ) : (
                  <FileText size={14} />
                )}

                <span className="max-w-[120px] truncate">
                  {tab.kind === 'sources' ? tab.title || t.rightRail.sourcesTab : tab.title}
                </span>
              </button>

              {closable && (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation()
                    onCloseTab?.(tab.id)
                  }}
                  className="mr-1 grid h-6 w-6 place-items-center rounded-md text-neutral-400 hover:bg-white hover:text-neutral-700"
                  title={t.closeTab}
                  aria-label={t.closeTab}
                >
                  <X size={13} />
                </button>
              )}
            </div>
          )
        })}
      </div>}

      {!collapsed && <div className="min-h-0 flex-1">
        {activeTab.kind === 'chats' ? (
          <ChatHistory
            chats={chats}
            activeChatId={activeChatId}
            onOpenChat={onOpenChat}
            onDeleteChat={onDeleteChat}
            onClearChats={onClearChats}
          />
        ) : activeTab.kind === 'sources' ? (
          <AnswerSourcesSidebar
            answer={activeTab.answer}
            rawById={rawById}
            activeNodeId={workspace?.kind === 'doc' ? workspace.nodeId : null}
            mentionedIds={mentionedIdsForActiveAnswer}
            onOpenNode={onOpenNode}
            onViewAnswer={onViewAnswer}
          />
        ) : (
          <DocSidebar
            wiki={wiki}
            rootPath={rootPath}
            activeTabId={workspace?.id}
            onOpenNode={onOpenNode}
            onOpenDocument={onOpenDocument}
          />
        )}
      </div>}

      {!collapsed && <div ref={actionsRef} className="border-t border-line p-3">
        <button
          type="button"
          onClick={onNewChat}
          className="flex h-10 w-full items-center justify-center gap-2 rounded-xl border border-blue-200 bg-blue-50 px-3 text-[13px] font-bold text-blue-700 transition hover:bg-blue-100"
        >
          <PlusCircle size={17} />
          <span>{t.shell.newChat}</span>
        </button>

        <button
          type="button"
          onClick={onOpenSettings}
          className={`mt-2 flex h-10 w-full items-center justify-center gap-2 rounded-xl text-[13px] font-semibold text-neutral-500 transition hover:bg-neutral-100 hover:text-neutral-900 ${settingsActive ? 'bg-blue-50 text-blue-700' : ''}`}
        >
          <Settings size={17} />
          <span>{t.shell.settings}</span>
        </button>
      </div>}
    </aside>
  )
}

function clampRailWidth(width) {
  const value = Number(width)
  const clamped = Math.max(
    MIN_RAIL_WIDTH,
    Math.min(maxRailWidth(), Number.isFinite(value) ? value : DEFAULT_RAIL_WIDTH),
  )
  return clamped < COLLAPSE_RAIL_WIDTH ? MIN_RAIL_WIDTH : clamped
}

function maxRailWidth() {
  return typeof window === 'undefined'
    ? DEFAULT_RAIL_WIDTH
    : Math.max(MIN_RAIL_WIDTH, window.innerWidth)
}

function ChatHistory({ chats = [], activeChatId, onOpenChat, onDeleteChat, onClearChats }) {
  const t = useT(RIGHT_RAIL_STR)

  return (
    <div className="flex h-full min-h-0 flex-col bg-white p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-[12px] font-semibold text-neutral-500">
          {t.rightRail.savedChats}
        </span>
        {chats.length > 0 && (
          <button
            type="button"
            onClick={() => window.confirm(t.rightRail.clearChatsConfirm) && onClearChats?.()}
            className="text-[11px] font-semibold text-neutral-400 hover:text-red-600"
          >
            {t.rightRail.clearChats}
          </button>
        )}
      </div>

      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto">
        {chats.length === 0 && (
          <p className="px-2 py-6 text-center text-[12px] text-neutral-400">
            {t.rightRail.noChats}
          </p>
        )}
        {chats.map((chat) => (
          <div
            key={chat.id}
            className={`group flex w-full items-center rounded-lg text-[12px] font-semibold ${chat.id === activeChatId ? 'bg-blue-50 text-blue-700' : 'text-neutral-600 hover:bg-neutral-50'}`}
          >
            <button
              type="button"
              onClick={() => onOpenChat?.(chat.id)}
              className="flex min-w-0 flex-1 items-center gap-2 px-3 py-2 text-left"
            >
              <MessagesSquare size={14} className="shrink-0" />
              <span className="min-w-0 flex-1 truncate">{chat.title}</span>
            </button>
            <button
              type="button"
              aria-label={t.rightRail.deleteChat}
              title={t.rightRail.deleteChat}
              onClick={() => onDeleteChat?.(chat.id)}
              className="mr-2 grid h-6 w-6 shrink-0 place-items-center rounded text-neutral-400 opacity-0 hover:bg-red-50 hover:text-red-600 group-hover:opacity-100 focus:opacity-100"
            >
              <Trash2 size={13} />
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}

function AnswerSourcesSidebar({
  answer,
  rawById,
  activeNodeId,
  mentionedIds,
  onOpenNode,
  onViewAnswer,
}) {
  const t = useT(RIGHT_RAIL_STR)

  const mentionedSet = new Set(
    (Array.isArray(mentionedIds) ? mentionedIds : [])
      .map(normalizeNodeId)
      .filter(Boolean),
  )

  const refs = Array.isArray(answer?.refs) ? answer.refs : []

  const refByNormalizedId = new Map(
    refs
      .filter((ref) => ref?.id)
      .map((ref) => [normalizeNodeId(ref.id), ref]),
  )

  const citedIds = Array.from(
    new Map(
      [
        ...(Array.isArray(answer?.citedIds) ? answer.citedIds : []),
        ...refs.map((ref) => ref.id).filter(Boolean),
        ...(Array.isArray(mentionedIds) ? mentionedIds : []),
      ]
        .filter(Boolean)
        .map((id) => [normalizeNodeId(id), id]),
    ).values(),
  )

  const rows = citedIds
    .map((id, originalIndex) => {
      const normalizedId = normalizeNodeId(id)
      const node = getRawNode(rawById, id)
      const ref = getRefByAnyId(refByNormalizedId, id)

      const title =
        node?.title ||
        node?.label ||
        node?.entity ||
        node?.name ||
        node?.heading ||
        node?.metadata?.title ||
        node?.metadata?.label ||
        ref?.label ||
        ref?.title ||
        ref?.entity ||
        ref?.name ||
        'Untitled chunk'

      const summary =
        node?.summary ||
        node?.abstract ||
        node?.text ||
        node?.body ||
        node?.markdown ||
        node?.content ||
        node?.metadata?.summary ||
        ref?.note ||
        ''

      const sourceName =
        node?.original_document_name ||
        node?.documentName ||
        node?.sourceName ||
        node?.source_path ||
        node?.source ||
        node?.metadata?.sourceName ||
        node?.metadata?.source ||
        ''

      const typeLabel =
        node?.type === 'exogenous'
          ? t.searchResults.agentNote
          : t.searchResults.sourceNote

      return {
        id,
        normalizedId,
        node,
        ref,
        title,
        summary,
        sourceName,
        typeLabel,
        mentioned: mentionedSet.has(normalizedId),
        originalIndex,
      }
    })
    .sort((a, b) => {
      if (a.mentioned !== b.mentioned) return a.mentioned ? -1 : 1
      return a.originalIndex - b.originalIndex
    })

  const mentionedCount = rows.filter((row) => row.mentioned).length
  const mentionedCountLabel =
    typeof t.rightRail.mentionedCount === 'function'
      ? t.rightRail.mentionedCount(mentionedCount)
      : `${mentionedCount} mentioned`

  return (
    <div className="flex h-full min-h-0 flex-col bg-white px-[12px] py-[14px]">
      <div className="mb-3 border-b border-line pb-3">
        <div className="flex items-start gap-2">
          <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-blue-50 text-blue-700">
            <FileText size={17} />
          </div>

          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-semibold text-neutral-900">
              {t.rightRail.sourcesTitle}
            </div>

            <div className="mt-0.5 line-clamp-2 text-[12px] font-semibold leading-5 text-neutral-500">
              {answer?.title || answer?.question || t.answer}
            </div>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <span className="rounded-full border border-neutral-300 bg-neutral-100 px-2.5 py-1 text-[11px] font-semibold text-neutral-900">
            {t.rightRail.sourcesSubtitle(rows.length)}
          </span>

          {mentionedCount > 0 && (
            <span className="rounded-full border border-red-200 bg-red-50 px-2.5 py-1 text-[11px] font-semibold text-red-700">
              {mentionedCountLabel}
            </span>
          )}

          {answer?.markdown && (
            <button
              type="button"
              onClick={() => onViewAnswer?.(answer)}
              className="rounded-lg border border-blue-200 bg-blue-50 px-2.5 py-1.5 text-[11px] font-semibold text-blue-700 hover:bg-blue-100"
            >
              {t.rightRail.viewAnswer}
            </button>
          )}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto pb-5">
        {rows.length === 0 && (
          <p className="py-[20px] text-[12.5px] text-muted">
            {t.rightRail.noSources}
          </p>
        )}

        <div className="space-y-2">
          {rows.map((row) => {
            const active =
              normalizeNodeId(row.id) === normalizeNodeId(activeNodeId) ||
              normalizeNodeId(row.node?.id) === normalizeNodeId(activeNodeId)

            const disabled = !row.node

            const className = row.mentioned
              ? active
                ? 'border-red-400 bg-red-50 ring-2 ring-red-100'
                : 'border-red-300 bg-red-50 hover:border-red-400 hover:bg-red-100/60'
              : active
                ? 'border-blue-300 bg-blue-50'
                : 'border-neutral-200 bg-white hover:border-blue-200 hover:bg-blue-50/40'

            return (
              <button
                key={row.normalizedId || row.id}
                type="button"
                disabled={disabled}
                onClick={() => {
                  if (!row.node) return
                  onOpenNode?.(row.node)
                }}
                className={`w-full rounded-xl border p-3 text-left shadow-sm transition ${className} ${
                  disabled ? 'cursor-not-allowed opacity-60' : ''
                }`}
              >
                <div className="mb-2 flex items-center justify-between gap-2">
                  {row.mentioned ? (
                    <span className="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-red-700">
                      {t.rightRail.mentionedInAnswer}
                    </span>
                  ) : (
                    <span className="rounded-full bg-neutral-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-neutral-900">
                      {t.rightRail.usedSource}
                    </span>
                  )}

                  <span className="shrink-0 text-[10.5px] font-bold text-neutral-400">
                    {row.typeLabel}
                  </span>
                </div>

                <div
                  className={`text-[13px] font-semibold leading-5 ${
                    row.mentioned ? 'text-red-950' : 'text-neutral-900'
                  }`}
                >
                  {row.title}
                </div>

                {row.sourceName && (
                  <div className="mt-1 line-clamp-1 text-[11px] font-semibold text-neutral-400">
                    {row.sourceName}
                  </div>
                )}

                {row.summary ? (
                  <p
                    className={`mt-2 line-clamp-3 text-[12px] leading-5 ${
                      row.mentioned ? 'text-red-900/75' : 'text-neutral-600'
                    }`}
                  >
                    {row.summary}
                  </p>
                ) : (
                  disabled && (
                    <p className="mt-2 text-[12px] leading-5 text-neutral-500">
                      {t.rightRail.missingSource}
                    </p>
                  )
                )}
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}

function getMentionedIdsForAnswer(mentionedNodeIdsByAnswerId, answerId) {
  if (!answerId) return []

  if (mentionedNodeIdsByAnswerId && typeof mentionedNodeIdsByAnswerId.get === 'function') {
    return mentionedNodeIdsByAnswerId.get(answerId) || []
  }

  return mentionedNodeIdsByAnswerId?.[answerId] || []
}

function normalizeNodeId(id) {
  return String(id || '').trim().replace(/^node:/, '')
}

function addNodePrefix(id) {
  const clean = String(id || '').trim()
  if (!clean) return clean
  return clean.startsWith('node:') ? clean : `node:${clean}`
}

function getRawNode(rawById, id) {
  const clean = String(id || '').trim()
  if (!clean || !rawById) return null

  const normalized = normalizeNodeId(clean)
  const prefixed = addNodePrefix(normalized)

  if (typeof rawById.get === 'function') {
    return (
      rawById.get(clean) ||
      rawById.get(normalized) ||
      rawById.get(prefixed) ||
      null
    )
  }

  return (
    rawById[clean] ||
    rawById[normalized] ||
    rawById[prefixed] ||
    null
  )
}

function getRefByAnyId(refByNormalizedId, id) {
  const normalized = normalizeNodeId(id)
  return refByNormalizedId.get(normalized) || null
}
