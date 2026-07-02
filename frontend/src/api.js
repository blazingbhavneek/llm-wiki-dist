// Thin client over the FastAPI backend (app.py).
// Base URL configurable via VITE_API_URL; defaults to the dev server port.

const BASE = import.meta.env.VITE_API_URL || 'http://localhost:51026'
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function req(path, opts) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText)
    throw new Error(`${res.status}: ${detail}`)
  }
  return res.status === 204 ? null : res.json()
}

async function waitForJob(job, { timeoutMs = 10 * 60 * 1000, onProgress } = {}) {
  if (!job?.id || !job?.status || !job?.type) return job

  const started = Date.now()
  let current = job
  onProgress?.(current)

  while (current.status === 'queued' || current.status === 'running') {
    if (Date.now() - started > timeoutMs) {
      throw new Error(`Write job timed out: ${current.type}`)
    }

    await sleep(current.status === 'queued' ? 400 : 900)
    current = await req(`/api/write-jobs/${encodeURIComponent(current.id)}`)
    onProgress?.(current)
  }

  if (current.status === 'failed') {
    throw new Error(current.error || `Write job failed: ${current.type}`)
  }
  if (current.status === 'cancelled') {
    throw new Error(`Write job was cancelled: ${current.type}`)
  }

  return current.result
}

async function writeReq(path, opts, jobOpts) {
  return waitForJob(await req(path, opts), jobOpts)
}

// Create endpoints return { node, assimilating, message } on the fast-add path:
// the node is searchable immediately while heavy bookkeeping catches up in the
// background. Unwrap to the node for callers; surface the notice via a callback.
function unwrapAdd(result, jobOpts) {
  if (result && typeof result === 'object' && 'assimilating' in result && result.node) {
    if (result.assimilating) jobOpts?.onAssimilating?.(result.message)
    return result.node
  }
  return result
}

export const api = {
  graph: () => req('/api/graph'),
  health: (nodeId) => req(`/api/health${nodeId ? `?node_id=${encodeURIComponent(nodeId)}` : ''}`),
  recluster: (resolution = 1.0, jobOpts) =>
    writeReq(`/api/recluster?resolution=${resolution}`, { method: 'POST' }, jobOpts),

  node: (id) => req(`/api/node/${encodeURIComponent(id)}`),
  links: (id, direction = 'both', label) =>
    req(`/api/node/${encodeURIComponent(id)}/links?direction=${direction}${label ? `&label=${encodeURIComponent(label)}` : ''}`),
  updateNode: (id, body, jobOpts) =>
    writeReq(`/api/node/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify({ body }) }, jobOpts),
  deleteNode: (id, jobOpts) =>
    writeReq(`/api/node/${encodeURIComponent(id)}`, { method: 'DELETE' }, jobOpts),

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
      const detail = await res.text().catch(() => res.statusText)
      throw new Error(`${res.status}: ${detail}`)
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
  createExogenous: async (body, sourceNodeIds, origin, jobOpts) =>
    unwrapAdd(await writeReq('/api/exogenous', {
      method: 'POST',
      body: JSON.stringify({ body, source_node_ids: sourceNodeIds, origin }),
    }, jobOpts), jobOpts),
  createDocument: async ({ body, title, documentName, sourcePath, sourceRanges }, jobOpts) =>
    unwrapAdd(await writeReq('/api/document', {
      method: 'POST',
      body: JSON.stringify({
        body,
        title,
        document_name: documentName,
        source_path: sourcePath,
        source_ranges: sourceRanges,
      }),
    }, jobOpts), jobOpts),

  assimilation: () => req('/api/assimilation'),

  settings: () => req('/api/settings'),
  settingsSchema: () => req('/api/settings/schema'),
  patchSettings: (patch) =>
    req('/api/settings', { method: 'PATCH', body: JSON.stringify(patch) }),
}
