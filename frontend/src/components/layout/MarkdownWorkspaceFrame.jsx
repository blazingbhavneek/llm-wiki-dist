import { ArrowLeft, FileText, Loader2, Minimize2 } from 'lucide-react'

import { useT } from '../../i18n.jsx'
import { STR } from './strings.js'

export function MarkdownWorkspaceFrame({
  item,
  children,
  canGoBack,
  onBack,
  onClose,
  onDoubleClick,
}) {
  const t = useT(STR)

  return (
    <div className="flex h-full flex-col bg-white">
      <div className="flex h-[54px] shrink-0 items-center gap-3 border-b border-slate-200 bg-white px-4">
        {canGoBack && (
          <button
            onClick={onBack}
            className="flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-[12px] font-bold text-slate-600 hover:bg-slate-50 hover:text-slate-950"
            title={t.markdownFrame.backTitle}
            aria-label={t.markdownFrame.backTitle}
          >
            <ArrowLeft size={16} />
            <span>{t.markdownFrame.back}</span>
          </button>
        )}

        <div className="grid h-9 w-9 place-items-center rounded-xl bg-blue-50 text-blue-700">
          <FileText size={18} />
        </div>

        <div className="min-w-0 flex-1">
          <div className="truncate text-[14px] font-extrabold text-slate-950">
            {item?.doc?.title || item?.title || t.markdownFrame.fallbackTitle}
          </div>
          <div className="truncate text-[11px] font-medium text-slate-400">
            {t.markdownFrame.hint}
          </div>
        </div>

        {item?.busy && (
          <div className="mr-2 flex items-center gap-2 rounded-full bg-blue-50 px-3 py-1.5 text-[12px] font-bold text-blue-700">
            <Loader2 size={14} className="animate-spin" />
            <span>{item.busyMessage || t.markdownFrame.working}</span>
          </div>
        )}

        <button
          onClick={onClose}
          className="flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-[12px] font-bold text-slate-600 hover:bg-slate-50 hover:text-slate-950"
          title={t.markdownFrame.collapseTitle}
          aria-label={t.markdownFrame.collapseTitle}
        >
          <Minimize2 size={16} />
          <span>{t.markdownFrame.collapse}</span>
        </button>
      </div>

      <div
        onDoubleClick={onDoubleClick}
        className="min-h-0 flex-1 overflow-hidden bg-white"
      >
        {children}
      </div>
    </div>
  )
}

