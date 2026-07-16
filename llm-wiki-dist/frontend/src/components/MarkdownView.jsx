import { ChevronLeft, ChevronRight, Download } from 'lucide-react'

import { useT } from '../i18n.jsx'
import { downloadMarkdown } from '../data/download.js'
import { STR } from './markdown/strings.js'
import {
  MarkdownRenderer,
  stripCitedNodeIdsBlocks,
} from './markdown/MarkdownRenderer.jsx'
import { RichMarkdownEditor } from './markdown/RichMarkdownEditor.jsx'
import { InlineSpinner, SmallBtn } from './markdown/ui.jsx'
import {
  getPreviewInteractionProps,
  hasImageValidationErrors,
} from './markdown/imageUnits.js'

export default function MarkdownView({
  doc,
  draft,
  isEditing,
  dirty,
  mode = 'node',
  positions,
  busy,
  busyMessage,
  refs,
  rawById,
  prevNodeId,
  nextNodeId,
  onStartEdit,
  onCancelEdit,
  onConfirm,
  onDelete,
  onAddToGraph,
  onOpenNode,
  onChangeTitle,
  onChangeBody,
}) {
  const t = useT(STR)

  if (!doc) return null

  const readOnly = mode === 'fulldoc'
  const isAnswer = mode === 'answer'
  const isDraft = mode === 'draft' || isAnswer
  const canEdit = !readOnly
  const imageErrors = isEditing && hasImageValidationErrors(draft.markdown || '')

  const badgeText = String(doc.badge || '').toLowerCase()
  const isAgent =
    badgeText === 'agent' ||
    badgeText === 'agent note' ||
    badgeText === 'エージェントノート'

  const badgeClass = isAgent
    ? 'bg-orange/15 text-[#925d00] border-orange/25'
    : 'bg-green/10 text-[#08785a] border-green/20'

  const statusText = busy && busyMessage
    ? busyMessage
    : imageErrors
      ? t.invalidImages
      : readOnly
        ? doc.meta
        : isEditing
          ? t.editingStatus
          : isAnswer
            ? t.unsavedAnswer
            : isDraft
              ? t.draftStatus
              : dirty
                ? t.dirtyStatus
                : t.editButtonOnly

  const saveDisabled = !dirty || busy || imageErrors
  const addDisabled = busy || imageErrors

  const handleCancelEdit = () => {
    onCancelEdit?.()
  }

  // Export exactly what is on screen: the markdown, no added metadata.
  const handleExport = () => {
    downloadMarkdown(
      draft.title || doc.title,
      stripCitedNodeIdsBlocks(draft.markdown, rawById),
      t.untitled,
    )
  }

  const titlePreviewInteractionProps = !isEditing
    ? getPreviewInteractionProps()
    : {}

  // console.log('MarkdownView state', {
  //   isEditing,
  //   mode,
  //   docTitle: doc?.title,
  //   draftTitle: draft?.title,
  //   docMarkdownLength: doc?.markdown?.length,
  //   draftMarkdownLength: draft?.markdown?.length,
  //   docMarkdownPreview: doc?.markdown?.slice(0, 120),
  //   draftMarkdownPreview: draft?.markdown?.slice(0, 120),
  // })

  return (
    <div className="grid h-full w-full grid-rows-[auto_minmax(0,1fr)] bg-white">
      <div className="border-b border-line bg-white px-[26px] pb-[14px] pt-[18px]">
        <div className="mb-[8px] flex flex-wrap items-center gap-2 text-[12px] text-muted">
          <span
            className={`inline-flex items-center gap-[6px] border px-[8px] py-[5px] font-bold ${
              isEditing ? 'border-blue/20 bg-blue/10 text-[#244a9d]' : badgeClass
            }`}
          >
            {isEditing
              ? t.editing
              : isAnswer
                ? t.answerDraft
                : isDraft
                  ? t.draft
                  : isAgent
                    ? t.agentNote
                    : t.sourceNote}
          </span>

          <span className="inline-flex items-center gap-2">
            {busy && <InlineSpinner />}
            {statusText}
          </span>
        </div>

        <div className="flex items-center gap-3">
          <input
            {...titlePreviewInteractionProps}
            className="w-full border-0 bg-transparent p-0 text-[27px] font-extrabold tracking-tight text-ink outline-none read-only:cursor-text"
            value={draft.title}
            readOnly={!isEditing}
            onChange={(e) => onChangeTitle?.(e.target.value)}
          />
        </div>

        {!readOnly && (
          <div className="mt-[12px] flex flex-wrap items-center gap-2">
            <SmallBtn onClick={onStartEdit} active={isEditing} disabled={!canEdit || busy}>
              {isEditing ? t.editing : t.edit}
            </SmallBtn>

            {isEditing && (
              <SmallBtn onClick={handleCancelEdit} disabled={busy}>
                {t.cancel}
              </SmallBtn>
            )}

            {mode === 'doc' && !isEditing && (
              <>
                <SmallBtn
                  onClick={() => onOpenNode?.(prevNodeId)}
                  disabled={!prevNodeId || busy}
                  title={t.prevChunk}
                >
                  <span className="inline-flex items-center gap-[6px]">
                    <ChevronLeft size={13} />
                    {t.prevChunk}
                  </span>
                </SmallBtn>

                <SmallBtn
                  onClick={() => onOpenNode?.(nextNodeId)}
                  disabled={!nextNodeId || busy}
                  title={t.nextChunk}
                >
                  <span className="inline-flex items-center gap-[6px]">
                    {t.nextChunk}
                    <ChevronRight size={13} />
                  </span>
                </SmallBtn>
              </>
            )}

            {isDraft ? (
              <SmallBtn onClick={onAddToGraph} disabled={addDisabled} confirm>
                {busy ? t.adding : isAnswer ? t.addToWiki : t.addToGraph}
              </SmallBtn>
            ) : (
              <>
                <SmallBtn onClick={onConfirm} disabled={saveDisabled} confirm>
                  {busy ? t.saving : t.save}
                </SmallBtn>

                <SmallBtn onClick={handleExport} title={t.exportTitle} disabled={busy}>
                  <span className="inline-flex items-center gap-[6px]">
                    <Download size={13} />
                    {t.export}
                  </span>
                </SmallBtn>

                <SmallBtn onClick={onDelete} danger disabled={busy}>
                  {t.del}
                </SmallBtn>
              </>
            )}
          </div>
        )}
      </div>

      <div className="min-h-0 overflow-auto bg-gradient-to-b from-white to-[#fbfdff]">
        {isEditing ? (
          <RichMarkdownEditor
            markdown={draft.markdown}
            onChange={onChangeBody}
          />
        ) : (
          <PreviewModeContent
            readOnly={readOnly}
            positions={positions}
            markdown={draft.markdown}
            refs={isAnswer ? [] : refs}
            onOpenNode={onOpenNode}
            rawById={rawById}
          />
        )}
      </div>
    </div>
  )
}

function PreviewModeContent({
  readOnly,
  positions,
  markdown,
  refs,
  onOpenNode,
  rawById,
}) {
  const t = useT(STR)
  const showReferences = Array.isArray(refs) && refs.length > 0

  return (
    <div className="h-full w-full" {...getPreviewInteractionProps()}>
      {readOnly && positions?.length > 0 && (
        <div className="w-full px-[36px] pt-[24px]">
          <div className="border border-line bg-soft px-[14px] py-[10px] text-[12px] text-muted">
            <span className="font-bold text-ink">{positions.length}</span>{' '}
            {t.pagesChain}
            <span className="ml-[6px]">
              {positions.map((p) => (
                <span key={p.id} className="mr-[6px] whitespace-nowrap">
                  [{p.index}] {p.title}
                </span>
              ))}
            </span>
          </div>
        </div>
      )}

      <article className="md w-full max-w-none px-[36px] pb-[90px] pt-[36px]">
        <MarkdownRenderer
          markdown={markdown}
          onOpenNode={onOpenNode}
          rawById={rawById}
          referenceLabel={t.referenceLabel}
        />
        {showReferences && <References refs={refs} onOpenNode={onOpenNode} />}
      </article>
    </div>
  )
}

function References({ refs, onOpenNode }) {
  const t = useT(STR)

  if (!refs?.length) return null

  return (
    <div className="mt-[28px] border-t border-line pt-[14px] text-[13px] text-muted">
      <h3 className="mb-[8px] text-[12px] font-bold uppercase tracking-wider text-muted">
        {t.references}
      </h3>

      <ol className="m-0 list-decimal pl-[20px]">
        {refs.map((r) => (
          <li key={r.id} className="my-[6px]">
            <button
              type="button"
              className="border-b border-dotted border-[#244a9d]/40 text-left text-[#244a9d] hover:text-blue"
              onClick={() => onOpenNode?.(r.id)}
            >
              {r.label}
            </button>{' '}
            - {r.note}
          </li>
        ))}
      </ol>
    </div>
  )
}
