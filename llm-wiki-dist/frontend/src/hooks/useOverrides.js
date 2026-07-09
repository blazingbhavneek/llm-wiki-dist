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
    window.localStorage.setItem('wikiOverrides', JSON.stringify(o))
  }

  return { overrides, applyOverrides }
}
