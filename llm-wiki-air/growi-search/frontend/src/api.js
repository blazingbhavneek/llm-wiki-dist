// Read-only client over the growi-search FastAPI backend (app.py).
// Served under WIKI_PREFIX; derive the API base from the URL the SPA loaded from.
export const BASE = import.meta.env.VITE_API_URL ?? window.location.pathname.replace(/\/$/, '')

export class ApiError extends Error {
  constructor(status, payload, fallback) {
    const detail = payload?.detail || fallback || `Request failed: ${status}`
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.retryable = Boolean(payload?.retryable)
    this.code = payload?.code || 'request_failed'
    this.payload = payload
  }
}

async function parseError(res) {
  const text = await res.text().catch(() => res.statusText)
  try {
    return JSON.parse(text)
  } catch {
    return { detail: text || res.statusText, retryable: res.status >= 500 }
  }
}

async function req(path, opts) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!res.ok) {
    throw new ApiError(res.status, await parseError(res), res.statusText)
  }
  return res.status === 204 ? null : res.json()
}

export const api = {
  ready: () => req('/api/ready'),
  growi: () => req('/api/growi'),
  children: ({ pageId, path } = {}) => {
    const query = pageId ? `page_id=${encodeURIComponent(pageId)}` : `path=${encodeURIComponent(path ?? '/')}`
    return req(`/api/pages/children?${query}`)
  },
  node: (id) => req(`/api/node/${encodeURIComponent(id)}`),
  search: (q, limit) =>
    req(`/api/search?q=${encodeURIComponent(q)}${limit ? `&limit=${limit}` : ''}`),
  ask: (question, overrides) =>
    req('/api/ask', { method: 'POST', body: JSON.stringify({ question, overrides }) }),
  stopAgentRun: (runId) =>
    req(`/api/agent-runs/${encodeURIComponent(runId)}/stop`, { method: 'POST' }),

  // Stream step-level agent progress via SSE. Calls onEvent(ev) per event;
  // resolves when the stream ends. Falls back to throwing on a non-OK response.
  // `overrides` is an optional per-request tunable map (subagents/depth/etc).
  askStream: async (question, overrides, onEvent) => {
    const res = await fetch(`${BASE}/api/ask/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, overrides }),
    })
    if (!res.ok || !res.body) {
      throw new ApiError(res.status, await parseError(res), res.statusText)
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let sep
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)

        for (const line of frame.split('\n')) {
          if (!line.startsWith('data:')) continue // skip ": ping"/comments
          const payload = line.slice(5).trim()
          if (!payload) continue
          let ev
          try {
            ev = JSON.parse(payload)
          } catch {
            continue // ignore malformed frame
          }
          onEvent(ev) // throws here propagate to the caller
        }
      }
    }
  },
  settings: () => req('/api/settings'),
  document: (path) => req(`/api/document?path=${encodeURIComponent(path)}`),
  attachmentUrl: (src) => `${BASE}/api${src}`,
}
