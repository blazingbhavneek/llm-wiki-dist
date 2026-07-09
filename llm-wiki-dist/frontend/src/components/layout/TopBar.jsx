import { useState } from 'react'
import {
  Bookmark,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Loader2,
  Search,
  UserCircle,
} from 'lucide-react'

import { useT, LangToggle } from '../../i18n.jsx'
import { STR } from './strings.js'

export function TopBar({
  onSearch,
  onSearchResults,
  rightOpen,
  onToggleRight,
}) {
  const t = useT(STR)
  const [q, setQ] = useState('')
  const [searching, setSearching] = useState(false)

  const submitKeywordSearch = async () => {
    const clean = q.trim()
    if (!clean) return

    setSearching(true)

    try {
      const results = await onSearch?.(clean)

      if (onSearchResults) {
        onSearchResults({
          query: clean,
          results: Array.isArray(results) ? results : [],
        })
      }
    } finally {
      setSearching(false)
    }
  }

  return (
    <header className="flex h-[70px] shrink-0 items-center gap-4 border-b border-slate-200 bg-white px-5">
      <div className="flex min-w-0 flex-1 items-center">
        <div className="flex h-11 w-full max-w-[760px] items-center rounded-xl border border-slate-300 bg-white shadow-sm transition focus-within:border-blue-500 focus-within:ring-4 focus-within:ring-blue-50">
          <Search size={18} className="ml-4 shrink-0 text-slate-400" />

          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submitKeywordSearch()
            }}
            placeholder={t.topbar.placeholder}
            className="h-full min-w-0 flex-1 bg-transparent px-3 text-[14px] font-medium text-slate-800 outline-none placeholder:text-slate-400"
            aria-label={t.topbar.keywordSearch}
          />

          <button
            onClick={submitKeywordSearch}
            disabled={searching || !q.trim()}
            className="mr-1.5 inline-flex items-center gap-2 rounded-lg bg-blue-600 px-5 py-2 text-[13px] font-extrabold text-white shadow-sm hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
            title={t.topbar.keywordSearch}
            aria-label={t.topbar.keywordSearch}
          >
            {searching && <Loader2 size={14} className="animate-spin" />}
            <span>{searching ? t.topbar.searching : t.topbar.searchDocs}</span>
          </button>
        </div>
      </div>

      <div className="ml-auto flex items-center gap-2">
        <ToolbarButton icon={Clock3} label={t.shell.history} />
        <ToolbarButton icon={Bookmark} label={t.shell.bookmark} />

        <div className="mx-1 h-7 w-px bg-slate-200" />

        <LangToggle />

        <button
          onClick={onToggleRight}
          className={`grid h-9 w-9 place-items-center rounded-xl border transition ${
            rightOpen
              ? 'border-blue-200 bg-blue-50 text-blue-700'
              : 'border-slate-200 bg-white text-slate-500 hover:bg-slate-50'
          }`}
          title={rightOpen ? t.shell.collapseDocuments : t.shell.showDocuments}
          aria-label={rightOpen ? t.shell.collapseDocuments : t.shell.showDocuments}
        >
          {rightOpen ? <ChevronRight size={18} /> : <ChevronLeft size={18} />}
        </button>

        <button
          className="grid h-9 w-9 place-items-center rounded-full border border-slate-200 bg-white text-blue-700 hover:bg-blue-50"
          title={t.shell.profile}
          aria-label={t.shell.profile}
        >
          <UserCircle size={22} />
        </button>
      </div>
    </header>
  )
}

function ToolbarButton({ icon: Icon, label }) {
  return (
    <button
      className="hidden items-center gap-2 rounded-xl px-3 py-2 text-[13px] font-semibold text-slate-600 hover:bg-slate-100 hover:text-slate-950 lg:flex"
      title={label}
      aria-label={label}
    >
      <Icon size={17} />
      <span>{label}</span>
    </button>
  )
}

