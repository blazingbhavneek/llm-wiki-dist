import { FileText, Loader2, Search } from 'lucide-react'

import { useT } from '../../i18n.jsx'
import { PageHeader, PlaceholderPage } from './Shell'
import { STR } from './strings.js'

export function SearchResultsCenter({
  query,
  results = [],
  loading = false,
  onOpenNode,
}) {
  const t = useT(STR)

  if (loading) {
    return (
      <div className="grid h-full place-items-center bg-gradient-to-b from-white to-[#f8fbff] px-6">
        <div className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-5 py-4 text-[14px] font-bold text-slate-600 shadow-sm">
          <Loader2 size={18} className="animate-spin text-blue-600" />
          <span>{t.topbar.searching}</span>
        </div>
      </div>
    )
  }

  if (!query) {
    return (
      <PlaceholderPage
        icon={Search}
        title={t.pages.searchIdleTitle}
        text={t.pages.searchIdleText}
      />
    )
  }

  if (!results.length) {
    return (
      <PlaceholderPage
        icon={Search}
        title={t.pages.searchEmptyTitle}
        text={t.pages.searchEmptyText(query)}
      />
    )
  }

  return (
    <div className="h-full overflow-y-auto bg-gradient-to-b from-white to-[#f8fbff]">
      <div className="mx-auto max-w-[960px] px-6 py-6">
        <PageHeader
          icon={Search}
          title={t.pages.searchTitle}
          text={t.pages.searchText(query, results.length)}
          aside={t.searchResults.resultCount(results.length)}
        />

        <div className="grid gap-3">
          {results.map((node) => (
            <SearchResultCard
              key={node.id}
              node={node}
              onOpenNode={onOpenNode}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

function SearchResultCard({ node, onOpenNode }) {
  const t = useT(STR)

  const title =
    node?.title ||
    node?.entity ||
    node?.name ||
    node?.id ||
    t.untitled

  const summary =
    node?.summary ||
    node?.abstract ||
    node?.text ||
    node?.markdown ||
    t.searchResults.noSummary

  const source =
    node?.document ||
    node?.source ||
    node?.source_name ||
    node?.sourceName ||
    t.searchResults.unknownSource

  const typeLabel =
    node?.type === 'exogenous'
      ? t.searchResults.agentNote
      : t.searchResults.sourceNote

  return (
    <button
      onClick={() => onOpenNode?.(node)}
      className="w-full rounded-2xl border border-slate-200 bg-white p-4 text-left shadow-sm transition hover:border-blue-200 hover:bg-blue-50/30 hover:shadow-md"
    >
      <div className="flex items-start gap-3">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-50 text-blue-700">
          <FileText size={20} />
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h3 className="line-clamp-1 text-[15px] font-extrabold text-slate-950">
                {title}
              </h3>

              <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] font-bold text-slate-400">
                <span>{typeLabel}</span>
                <span>·</span>
                <span className="line-clamp-1">{source}</span>
              </div>
            </div>

            <span className="shrink-0 rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-[12px] font-extrabold text-blue-700">
              {t.searchResults.open}
            </span>
          </div>

          <p className="mt-3 line-clamp-3 text-[13px] leading-6 text-slate-600">
            {summary}
          </p>
        </div>
      </div>
    </button>
  )
}

