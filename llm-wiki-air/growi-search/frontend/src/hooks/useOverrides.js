import { useState } from 'react'

/** Per-request agent overrides, persisted in localStorage. */
export function useOverrides() {
  const [overrides, setOverrides] = useState(() => {
    try {
      return JSON.parse(window.localStorage.getItem('wikiOverrides') || 'null')
    } catch {
      return null
    }
  })

  const applyOverrides = (o) => {
    setOverrides(o)
    const { chat_api_key, ...persisted } = o || {}
    window.localStorage.setItem('wikiOverrides', JSON.stringify(persisted))
  }

  return { overrides, applyOverrides }
}
