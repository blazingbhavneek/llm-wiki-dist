import { useEffect, useState } from 'react'
import { ExternalLink, FolderOpen, Loader2 } from 'lucide-react'

import { api } from '../../api'
import { useT } from '../../i18n.jsx'
import { Centered, PageHeader } from './Shell'
import { SearchResultCard } from './SearchResults'
import { STR } from './strings.js'

/** Center view for one document folder: every page as a card. */
export function DocumentCenter({ path, connection, onOpenNode }) {
  const t = useT(STR)
  const [state, setState] = useState({ loading: true, doc: null, error: '' })

  useEffect(() => {
    let cancelled = false
    setState({ loading: true, doc: null, error: '' })
    api.document(path)
      .then((doc) => !cancelled && setState({ loading: false, doc, error: '' }))
      .catch((e) => !cancelled && setState({ loading: false, doc: null, error: e.message }))
    return () => {
      cancelled = true
    }
  }, [path])

  if (state.loading) {
    return <Centered><Loader2 size={18} className="animate-spin text-blue-600" /></Centered>
  }
  if (state.error) return <Centered>{t.couldNotOpen(state.error)}</Centered>

  const pages = state.doc?.pages || []
  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="mx-auto max-w-[960px] px-6 py-6">
        <PageHeader
          icon={FolderOpen}
          title={state.doc?.title || path}
          text={path}
          aside={
            state.doc?.source_url ? (
              <a href={state.doc.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[12px] font-bold text-blue-700 hover:underline">
                <ExternalLink size={12} /> {t.searchResults.openInGrowi}
              </a>
            ) : (
              t.app.documentPages(pages.length)
            )
          }
        />
        {pages.length === 0 && <p className="text-[13px] text-muted">{t.app.noPages}</p>}
        <div className="grid gap-3">
          {pages.map((node) => (
            <SearchResultCard key={node.id} node={node} connection={connection} onOpenNode={onOpenNode} />
          ))}
        </div>
      </div>
    </div>
  )
}
