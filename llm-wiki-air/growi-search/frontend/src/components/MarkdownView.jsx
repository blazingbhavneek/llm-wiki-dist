import { ChevronLeft, ChevronRight, Download } from 'lucide-react'

import { useT } from '../i18n.jsx'
import { downloadMarkdown } from '../data/download.js'
import { STR } from './markdown/strings.js'
import { growiLinkFor } from '../data/growi.js'
import { MarkdownRenderer } from './markdown/MarkdownRenderer.jsx'
import { SmallBtn } from './markdown/ui.jsx'

export default function MarkdownView({ doc, mode = 'node', rawById, prevNodeId, nextNodeId, onOpenNode, growiConnection }) {
  const t = useT(STR)
  if (!doc) return null

  const badgeText = String(doc.badge || '').toLowerCase()
  const isAgent = badgeText === 'agent' || badgeText === 'agent note' || badgeText === 'エージェントノート'
  const handleExport = () => downloadMarkdown(doc.title, doc.markdown, t.untitled)

  return (
    <div className="grid h-full w-full grid-rows-[auto_minmax(0,1fr)] bg-white">
      <div className="border-b border-line bg-white px-[26px] pb-[14px] pt-[18px]">
        <div className="mb-[8px] flex flex-wrap items-center gap-2 text-[12px] text-muted">
          <span className={`inline-flex items-center gap-[6px] border px-[8px] py-[5px] font-bold ${isAgent ? 'border-neutral-300 bg-neutral-100 text-neutral-900' : 'border-neutral-300 bg-neutral-100 text-neutral-900'}`}>
            {mode === 'answer' ? t.answerDraft : isAgent ? t.agentNote : t.sourceNote}
          </span>
          <span>{doc.meta}</span>
        </div>

        <h1 className="w-full text-[27px] font-semibold tracking-tight text-ink">{doc.title}</h1>

        {(() => {
          const link = growiLinkFor(doc, growiConnection)
          return link ? <a href={link.view} target="_blank" rel="noreferrer" className="mt-2 inline-block text-[12px] font-semibold text-blue-700 hover:underline">{t.openInGrowi}</a> : null
        })()}

        <div className="mt-[12px] flex flex-wrap items-center gap-2">
          {mode === 'doc' && (
            <>
              <SmallBtn onClick={() => onOpenNode?.(prevNodeId)} disabled={!prevNodeId} title={t.prevChunk}>
                <span className="inline-flex items-center gap-[6px]"><ChevronLeft size={13} />{t.prevChunk}</span>
              </SmallBtn>
              <SmallBtn onClick={() => onOpenNode?.(nextNodeId)} disabled={!nextNodeId} title={t.nextChunk}>
                <span className="inline-flex items-center gap-[6px]">{t.nextChunk}<ChevronRight size={13} /></span>
              </SmallBtn>
            </>
          )}
          <SmallBtn onClick={handleExport} title={t.exportTitle}>
            <span className="inline-flex items-center gap-[6px]"><Download size={13} />{t.export}</span>
          </SmallBtn>
        </div>
      </div>

      <div className="min-h-0 overflow-auto bg-white">
        <PreviewModeContent markdown={doc.markdown} sourcePath={doc.source_path} links={doc.links} onOpenNode={onOpenNode} rawById={rawById} />
      </div>
    </div>
  )
}

function PreviewModeContent({ markdown, sourcePath, links, onOpenNode, rawById }) {
  return (
    <article className="md w-full max-w-none px-[36px] pb-[90px] pt-[36px]">
      <MarkdownRenderer markdown={markdown} sourcePath={sourcePath} links={links} onOpenNode={onOpenNode} rawById={rawById} />
    </article>
  )
}
