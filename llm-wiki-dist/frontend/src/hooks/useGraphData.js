import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { api } from '../api'
import { layoutGraph } from '../data/layout'
import { buildLibrary } from '../data/docs'

function docTime(doc) {
  const nodes = Array.isArray(doc?.nodes) ? doc.nodes : []

  let best = 0

  for (const n of nodes) {
    const candidates = [
      n.updated_at,
      n.updatedAt,
      n.modified_at,
      n.modifiedAt,
      n.created_at,
      n.createdAt,
      n.timestamp,
      n.time,
    ]

    for (const value of candidates) {
      const parsed =
        typeof value === 'number' ? value : value ? Date.parse(value) : 0

      if (Number.isFinite(parsed) && parsed > best) {
        best = parsed
      }
    }
  }

  return best
}

function sortDocumentsForSidebar(a, b) {
  const aTime = docTime(a)
  const bTime = docTime(b)

  if (aTime !== bTime) return bTime - aTime

  return String(a.name || '').localeCompare(String(b.name || ''))
}

function sortTopicsForSidebar(a, b) {
  const aCount = Array.isArray(a.docs) ? a.docs.length : 0
  const bCount = Array.isArray(b.docs) ? b.docs.length : 0

  if (aCount !== bCount) return bCount - aCount

  return String(a.cluster || '').localeCompare(String(b.cluster || ''))
}

/**
 * Backend graph data + derived views.
 *
 * IMPORTANT:
 * `graph` is layout data for the canvas; `raw` is the original backend
 * graph data needed by buildLibrary() for the document rail.
 */
export function useGraphData() {
  const [raw, setRaw] = useState({ nodes: [], edges: [] })
  const [health, setHealth] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [errorRetryable, setErrorRetryable] = useState(false)
  const [readyState, setReadyState] = useState(null)

  // Guards against setState after unmount (delayed fetch resolves post-unmount).
  const mountedRef = useRef(true)

  const reload = useCallback(async () => {
    setError(null)
    setErrorRetryable(false)

    const ready = await api.ready()
    if (mountedRef.current) setReadyState(ready)

    if (!ready.ready) {
      const detail = ready.error || `バックエンドの起動中です。しばらくお待ちください...`
      const err = new Error(detail)
      err.retryable = true
      err.code = 'not_ready'
      throw err
    }

    const [g, h] = await Promise.all([api.graph(), api.health()])

    if (mountedRef.current) {
      setRaw({ nodes: g.nodes, edges: g.edges })
      setHealth(h)
    }

    return g
  }, [])

  useEffect(() => {
    mountedRef.current = true
    let cancelled = false
    let retryTimer = null

    const tick = async () => {
      if (cancelled) return
      try {
        await reload()
        if (!cancelled) setLoading(false)
      } catch (e) {
        if (cancelled) return
        setError(String(e.message || e))
        setErrorRetryable(Boolean(e.retryable))
        setLoading(false)
        if (e.code === 'not_ready') {
          retryTimer = setTimeout(tick, 1500)
        }
      }
    }

    tick()

    return () => {
      cancelled = true
      mountedRef.current = false
      if (retryTimer) clearTimeout(retryTimer)
    }
  }, [reload])

  const rawById = useMemo(() => new Map(raw.nodes.map((n) => [n.id, n])), [raw])

  const graph = useMemo(() => layoutGraph(raw.nodes, raw.edges), [raw])

  const docLibrary = useMemo(() => {
    return buildLibrary(raw.nodes || [], raw.edges || [], {
      sortDocuments: sortDocumentsForSidebar,
      sortTopicDocuments: sortDocumentsForSidebar,
      sortTopics: sortTopicsForSidebar,
    })
  }, [raw.nodes, raw.edges])

  const retry = useCallback(async () => {
    setLoading(true)
    setError(null)
    setErrorRetryable(false)
    if (readyState && !readyState.ready && readyState.error) {
      await api.restartBootstrap()
    }
    try {
      await reload()
    } catch (e) {
      setError(String(e.message || e))
      setErrorRetryable(Boolean(e.retryable))
    } finally {
      setLoading(false)
    }
  }, [readyState, reload])

  return {
    raw,
    rawById,
    graph,
    docLibrary,
    health,
    loading,
    error,
    errorRetryable,
    readyState,
    reload,
    retry,
  }
}
