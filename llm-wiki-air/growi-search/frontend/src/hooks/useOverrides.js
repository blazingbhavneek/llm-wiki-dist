import { useState } from 'react'

// Per-request agent overrides, persisted in sessionStorage ONLY: a reload is
// intended to lose custom API endpoints/keys (read-only shared service).
const KEY = 'wikiOverrides'

function readStored() {
  try {
    return JSON.parse(window.sessionStorage.getItem(KEY) || 'null') || { values: {}, key: '' }
  } catch {
    return { values: {}, key: '' }
  }
}

export function useOverrides() {
  const [stored, setStored] = useState(readStored)

  const applyOverrides = ({ values, key }) => {
    const next = { values, key: key || '' }
    setStored(next)
    try {
      // The API key lives in sessionStorage too: same tab only, gone on reload.
      window.sessionStorage.setItem(KEY, JSON.stringify(next))
    } catch {
      /* private mode: overrides simply do not persist */
    }
  }

  const clearOverrides = () => {
    setStored({ values: {}, key: '' })
    try {
      window.sessionStorage.removeItem(KEY)
    } catch {
      /* ignore */
    }
  }

  // Send null-ish values as absent; chat_api_key only when the user typed one.
  const payload = Object.fromEntries(
    Object.entries(stored.values).filter(([, v]) => v !== '' && v !== null && v !== undefined),
  )
  if (stored.key) payload.chat_api_key = stored.key

  return {
    overrides: Object.keys(payload).length ? payload : undefined,
    persisted: stored.values,
    apiKey: stored.key,
    applyOverrides,
    clearOverrides,
  }
}
