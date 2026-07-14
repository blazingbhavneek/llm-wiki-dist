import { FileText, FolderTree, X } from 'lucide-react'

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
    },
  },
  en: {
    ...(STR.en || {}),
    rightRail: {
      ...((STR.en || {}).rightRail || {}),
      mentionedInAnswer: 'Mentioned in answer',
      mentionedCount: (count) => `${count} mentioned`,
    },
  },
}

export function RightDocumentRail({
  mode,
  library,
  workspace,
  tabs,
  activeTabId,
  onActivateTab,
  onCloseTab,
  onModeChange,
  onOpenNode,
  onOpenFullDoc,
  onDeleteDocument,
  deletingDocs,
  rawById,
  onViewAnswer,

  // Map(answer.id -> Array<string>)
  mentionedNodeIdsByAnswerId,
}) {
  const t = useT(RIGHT_RAIL_STR)

  const allTabs = [
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
    <aside className="flex h-full min-h-0 w-[380px] shrink-0 flex-col border-l border-line bg-[#f8fafc]">
      <div className="flex h-[46px] shrink-0 items-center gap-1 overflow-x-auto border-b border-line bg-white px-2">
        {allTabs.map((tab) => {
          const active = tab.id === activeTab.id
          const closable = tab.kind !== 'explorer'

          return (
            <div
              key={tab.id}
              className={`group flex h-8 min-w-0 shrink-0 items-center rounded-lg border text-[12px] font-extrabold transition ${
                active
                  ? 'border-blue-200 bg-blue-50 text-blue-700'
                  : 'border-transparent bg-white text-slate-500 hover:bg-slate-50 hover:text-slate-900'
              }`}
            >
              <button
                type="button"
                onClick={() => onActivateTab?.(tab.id)}
                className="flex h-full min-w-0 items-center gap-1.5 px-2"
                title={tab.title}
              >
                {tab.kind === 'explorer' ? (
                  <FolderTree size={14} />
                ) : (
                  <FileText size={14} />
                )}

                <span className="max-w-[120px] truncate">
                  {tab.kind === 'explorer' ? tab.title : tab.title || t.rightRail.sourcesTab}
                </span>
              </button>

              {closable && (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation()
                    onCloseTab?.(tab.id)
                  }}
                  className="mr-1 grid h-6 w-6 place-items-center rounded-md text-slate-400 hover:bg-white hover:text-slate-700"
                  title={t.closeTab}
                  aria-label={t.closeTab}
                >
                  <X size={13} />
                </button>
              )}
            </div>
          )
        })}
      </div>

      <div className="min-h-0 flex-1">
        {activeTab.kind === 'sources' ? (
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
            mode={mode}
            library={library}
            activeTabId={workspace?.id}
            onModeChange={onModeChange}
            onOpenNode={onOpenNode}
            onOpenFullDoc={onOpenFullDoc}
            onDeleteDocument={onDeleteDocument}
            deletingDocs={deletingDocs}
          />
        )}
      </div>
    </aside>
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
    <div className="flex h-full min-h-0 flex-col bg-[#f8fafc] px-[12px] py-[14px]">
      <div className="mb-3 border-b border-line pb-3">
        <div className="flex items-start gap-2">
          <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-emerald-100 text-emerald-700">
            <FileText size={17} />
          </div>

          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-extrabold text-slate-900">
              {t.rightRail.sourcesTitle}
            </div>

            <div className="mt-0.5 line-clamp-2 text-[12px] font-semibold leading-5 text-slate-500">
              {answer?.title || answer?.question || t.answer}
            </div>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[11px] font-extrabold text-emerald-700">
            {t.rightRail.sourcesSubtitle(rows.length)}
          </span>

          {mentionedCount > 0 && (
            <span className="rounded-full border border-red-200 bg-red-50 px-2.5 py-1 text-[11px] font-extrabold text-red-700">
              {mentionedCountLabel}
            </span>
          )}

          {answer?.markdown && (
            <button
              type="button"
              onClick={() => onViewAnswer?.(answer)}
              className="rounded-lg border border-blue-200 bg-blue-50 px-2.5 py-1.5 text-[11px] font-extrabold text-blue-700 hover:bg-blue-100"
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
                : 'border-emerald-200 bg-white hover:border-emerald-300 hover:bg-emerald-50/40'

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
                    <span className="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-extrabold uppercase tracking-wide text-red-700">
                      {t.rightRail.mentionedInAnswer}
                    </span>
                  ) : (
                    <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-extrabold uppercase tracking-wide text-emerald-700">
                      {t.rightRail.usedSource}
                    </span>
                  )}

                  <span className="shrink-0 text-[10.5px] font-bold text-slate-400">
                    {row.typeLabel}
                  </span>
                </div>

                <div
                  className={`text-[13px] font-extrabold leading-5 ${
                    row.mentioned ? 'text-red-950' : 'text-slate-900'
                  }`}
                >
                  {row.title}
                </div>

                {row.sourceName && (
                  <div className="mt-1 line-clamp-1 text-[11px] font-semibold text-slate-400">
                    {row.sourceName}
                  </div>
                )}

                {row.summary ? (
                  <p
                    className={`mt-2 line-clamp-3 text-[12px] leading-5 ${
                      row.mentioned ? 'text-red-900/75' : 'text-slate-600'
                    }`}
                  >
                    {row.summary}
                  </p>
                ) : (
                  disabled && (
                    <p className="mt-2 text-[12px] leading-5 text-slate-500">
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
