import { useCallback, useEffect, useState } from 'react'

import { api } from '../api'

export const parentOf = (p) => String(p || '').replace(/\/[^/]+$/, '') || '/'

/** Backend readiness + lazy GROWI folder browser + citation node cache. */
export function useWikiData({ rootPath, fireToast, t }) {
  const [ready, setReady] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [childrenByPath, setChildrenByPath] = useState({})
  const [expanded, setExpanded] = useState({})
  const [rawById, setRawById] = useState(() => new Map())

  const rememberNodes = useCallback((nodes) => {
    const list = (Array.isArray(nodes) ? nodes : [nodes]).filter((n) => n?.id)
    if (!list.length) return
    setRawById((prev) => {
      const next = new Map(prev)
      for (const n of list) next.set(n.id, { ...(prev.get(n.id) || {}), ...n })
      return next
    })
  }, [])

  const load = useCallback(
    async (path) => {
      setChildrenByPath((prev) => ({ ...prev, [path]: { ...(prev[path] || {}), loading: true } }))
      try {
        const data = await api.children({ path })
        const pages = (Array.isArray(data?.children) ? data.children : [])
          .sort((a, b) => String(a.path).localeCompare(String(b.path), 'ja', { numeric: true }))
        setChildrenByPath((prev) => ({ ...prev, [path]: { loading: false, pages } }))
        rememberNodes(pages)
        return pages
      } catch (e) {
        setChildrenByPath((prev) => ({ ...prev, [path]: { loading: false, pages: [], error: e.message } }))
        fireToast?.(t.couldNotOpen(e.message))
        return []
      }
    },
    [fireToast, t, rememberNodes],
  )

  const toggle = useCallback(
    (path) => {
      const open = !expanded[path]
      setExpanded((prev) => ({ ...prev, [path]: open }))
      if (open && !childrenByPath[path]?.pages) load(path)
    },
    [expanded, childrenByPath, load],
  )

  const reload = useCallback(async () => {
    if (!rootPath) return
    setError(null)
    const r = await api.ready()
    setReady(r)
    if (!r.ready) {
      const err = new Error(r.error || t.cannotReach)
      err.retryable = true
      throw err
    }
    setChildrenByPath({})
    setExpanded({})
    await load(rootPath)
  }, [load, rootPath, t])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        await reload()
      } catch (e) {
        if (!cancelled) setError(String(e.message || e))
      } finally {
        if (!cancelled && rootPath) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [reload, rootPath])

  const retry = useCallback(async () => {
    setLoading(true)
    try {
      await reload()
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setLoading(false)
    }
  }, [reload])

  const siblingsOf = useCallback(
    (node) => childrenByPath[parentOf(node?.path)]?.pages || [],
    [childrenByPath],
  )

  return { ready, loading, error, retry, reload, childrenByPath, expanded, toggle, load, rawById, rememberNodes, siblingsOf }
}
