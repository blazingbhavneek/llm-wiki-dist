import { useCallback, useState } from 'react'

import { api } from '../api'

/**
 * Lazy GROWI folder browser for the right document rail.
 * Replaces useGraphData: there is no /api/graph and no full-library scan.
 * Each expand fetches exactly one /api/pages/children call for one path.
 */
export function useWikiData({ rootPath, fireToast, t }) {
  const [childrenByPath, setChildrenByPath] = useState({}) // path -> {loading, pages, error}
  const [expanded, setExpanded] = useState({}) // path -> true
  const [rootStatus, setRootStatus] = useState(null)

  const load = useCallback(
    async (path) => {
      setChildrenByPath((prev) =>
        prev[path]?.pages || prev[path]?.loading ? prev : { ...prev, [path]: { loading: true } },
      )
      try {
        const data = await api.children({ path })
        const pages = Array.isArray(data?.children) ? data.children : []
        // Path depth 1 sorts "001-" style numeric prefixes naturally via localeCompare.
        pages.sort((a, b) => String(a.path).localeCompare(String(b.path), 'ja'))
        setChildrenByPath((prev) => ({ ...prev, [path]: { loading: false, pages } }))
        return pages
      } catch (e) {
        setChildrenByPath((prev) => ({ ...prev, [path]: { loading: false, pages: [], error: e.message } }))
        fireToast?.(t?.couldNotOpen ? t.couldNotOpen(e.message) : e.message)
        return []
      }
    },
    [fireToast, t],
  )

  const loadRoot = useCallback(() => load(rootPath || '/'), [load, rootPath])

  const toggle = useCallback(
    (path) => {
      const open = !expanded[path]
      setExpanded((prev) => ({ ...prev, [path]: open }))
      if (open && !childrenByPath[path]?.pages) load(path)
    },
    [expanded, childrenByPath, load],
  )

  const refresh = useCallback(() => {
    setExpanded({})
    setChildrenByPath({})
    setRootStatus(load(rootPath || '/'))
  }, [load, rootPath])

  return { childrenByPath, expanded, toggle, loadRoot, load, refresh, rootStatus }
}
