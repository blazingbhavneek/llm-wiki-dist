import { FileText, Loader2, Search } from 'lucide-react'

import { useT } from '../../i18n.jsx'
import { growiPageUrl } from '../../data/growi.js'
import { PageHeader, PlaceholderPage } from './Shell'
import { STR } from './strings.js'

export function SearchResultsCenter({
  query,
  results = [],
  loading = false,
  onOpenNode,
  connection,
}) {
  const t = useT(STR)

  if (loading) {
    return (
      <div className="grid h-full place-items-center bg-white px-6">
        <div className="flex items-center gap-2 rounded-2xl border border-neutral-200 bg-white px-5 py-4 text-[14px] font-bold text-neutral-600 shadow-sm">
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
    <div className="h-full overflow-y-auto bg-white">
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
              connection={connection}
              onOpenNode={onOpenNode}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

function SearchResultCard({ node, connection, onOpenNode }) {
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

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onOpenNode?.(node)}
      onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && onOpenNode?.(node)}
      className="w-full rounded-2xl border border-neutral-200 bg-white p-4 text-left shadow-sm transition hover:border-blue-200 hover:bg-blue-50/30"
    >
      <div className="flex items-start gap-3">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-50 text-blue-700">
          <FileText size={20} />
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h3 className="line-clamp-1 text-[15px] font-semibold text-neutral-950">
                {title}
              </h3>

              <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] font-bold text-neutral-400">
                <span className="line-clamp-1">{source}</span>
              </div>
            </div>

            <div className="flex shrink-0 items-center gap-2">
              <span className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-[12px] font-semibold text-blue-700">
                {t.searchResults.open}
              </span>
              {growiPageUrl(connection?.url, node.path) && (
                <a href={growiPageUrl(connection.url, node.path)} target="_blank" rel="noreferrer"
                   onClick={(e) => e.stopPropagation()}
                   className="rounded-lg border border-neutral-200 px-3 py-1.5 text-[12px] font-bold text-neutral-600 hover:bg-neutral-50">
                  {t.searchResults.openInGrowi}
                </a>
              )}
            </div>
          </div>

          <p className="mt-3 line-clamp-3 text-[13px] leading-6 text-neutral-600">
            {summary}
          </p>
        </div>
      </div>
    </div>
  )
}

export { SearchResultCard }
