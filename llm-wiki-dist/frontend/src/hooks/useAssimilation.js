import { useCallback, useEffect, useRef, useState } from 'react'

import { api } from '../api'

/**
 * Polls /api/assimilation while background enrichment is running so the UI
 * can show a "graph assimilating (N)" badge. Polling self-stops after the
 * backlog drains (with a couple of idle confirmations) or a hard deadline.
 */
export function useAssimilation() {
  const [assimPending, setAssimPending] = useState(0)

  const timerRef = useRef(null)
  const stopAtRef = useRef(0)
  const idleHitsRef = useRef(0)

  const stopAssimilationPolling = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }

    stopAtRef.current = 0
    idleHitsRef.current = 0
  }, [])

  const startAssimilationPolling = useCallback((options = {}) => {
    const {
      intervalMs = 4000,
      minDurationMs = 10000,
      maxDurationMs = 120000,
      idleHitsToStop = 2,
    } = options

    const startedAt = Date.now()
    const minStopAt = startedAt + minDurationMs

    stopAtRef.current = Math.max(stopAtRef.current, startedAt + maxDurationMs)

    if (timerRef.current) {
      return
    }

    const tick = async () => {
      try {
        const res = await api.assimilation()
        const pending = Number(res?.pending ?? 0)

        setAssimPending(Number.isFinite(pending) ? pending : 0)

        if (pending > 0) {
          idleHitsRef.current = 0
        } else {
          idleHitsRef.current += 1
        }

        const now = Date.now()

        const shouldContinue =
          now < stopAtRef.current &&
          (pending > 0 || now < minStopAt || idleHitsRef.current < idleHitsToStop)

        if (shouldContinue) {
          timerRef.current = setTimeout(tick, intervalMs)
        } else {
          timerRef.current = null
          stopAtRef.current = 0
          idleHitsRef.current = 0

          if (pending <= 0) {
            setAssimPending(0)
          }
        }
      } catch {
        const now = Date.now()

        if (now < stopAtRef.current) {
          timerRef.current = setTimeout(tick, intervalMs)
        } else {
          timerRef.current = null
          stopAtRef.current = 0
          idleHitsRef.current = 0
        }
      }
    }

    tick()
  }, [])

  useEffect(() => {
    return () => {
      stopAssimilationPolling()
    }
  }, [stopAssimilationPolling])

  return { assimPending, startAssimilationPolling }
}
