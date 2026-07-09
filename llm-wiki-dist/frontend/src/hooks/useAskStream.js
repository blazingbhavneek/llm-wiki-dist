import { useMemo, useState } from 'react'

import { api } from '../api'

function activityLine(ev, t) {
  const who = ev.agent ? t.explorer(ev.agent) : null
  const nm = (n) => n?.title || n?.id || '…'

  switch (ev.type) {
    case 'search':
      return t.searching(who, ev.query)
    case 'candidates':
      return t.pagesFound(ev.count)
    case 'route':
      return ev.mode === 'reuse'
        ? t.routeReuse
        : ev.mode === 'shallow'
          ? t.routeShallow
          : t.routeDeep
    case 'subagents_spawned':
      return t.spawned(ev.starts?.length || 0)
    case 'subagent_start':
      return t.exploring(who, nm(ev.node))
    case 'read':
      return t.reading(who, nm(ev.node))
    case 'follow_link':
      return t.following(who, nm(ev.node), ev.neighbors)
    case 'subagent_done':
      return t.subDone(who, ev.cited?.length || 0)
    case 'compiling':
      return t.compiling
    case 'diagram_pending':
      return t.diagramBuilding
    case 'diagram_ready':
      return t.diagramReady
    case 'diagram_failed':
      return t.diagramFailed
    default:
      return null
  }
}

/**
 * Chat message list + SSE agent-run lifecycle (/api/ask/stream).
 *
 * `onAskStart()` fires when a question is submitted (e.g. switch to chat
 * view); `onAnswer(ans, activity, question)` receives the final answer
 * event and is expected to patch the last assistant message via patchLast.
 */
export function useAskStream({ t, overrides, fireToast, onAskStart, onAnswer }) {
  const [messages, setMessages] = useState([])
  const [agentRunning, setAgentRunning] = useState(false)
  const [agentRunId, setAgentRunId] = useState(null)
  const [agentStopping, setAgentStopping] = useState(false)

  const patchLast = (fn) => {
    setMessages((prev) => {
      const copy = prev.slice()
      const i = copy.length - 1

      if (i >= 0 && copy[i].role === 'assistant') {
        copy[i] = fn(copy[i])
      }

      return copy
    })
  }

  const ask = async (q) => {
    const clean = q.trim()

    if (!clean || agentRunning) return

    onAskStart?.()
    setAgentRunning(true)
    setAgentRunId(null)
    setAgentStopping(false)

    setMessages((prev) => [
      ...prev,
      { role: 'user', text: clean },
      { role: 'assistant', streaming: true, title: t.working, activity: [] },
    ])

    const activity = []
    let sawCancelled = false

    try {
      await api.askStream(clean, overrides, (ev) => {
        if (ev.type === 'run') {
          setAgentRunId(ev.run_id || null)
          return
        }

        if (ev.type === 'cancelled') {
          sawCancelled = true

          return patchLast((m) => ({
            ...m,
            streaming: false,
            title: t.agentStopped,
            text: t.agentStoppedText,
            activity: activity.length ? [...activity] : m.activity || [],
          }))
        }

        if (ev.type === 'answer') {
          return onAnswer(ev, activity, clean)
        }

        if (ev.type === 'error') {
          return patchLast(() => ({
            role: 'assistant',
            streaming: false,
            error: true,
            title: t.requestFailed,
            text: ev.message,
          }))
        }

        if (ev.type === 'diagram_pending') {
          patchLast((m) => ({ ...m, _diagState: 'pending' }))
        } else if (ev.type === 'diagram_ready') {
          patchLast((m) => ({
            ...m,
            _diagState: 'ready',
            _diagMd: ev.answer ?? m._diagMd,
          }))
        } else if (ev.type === 'diagram_failed') {
          patchLast((m) => ({
            ...m,
            _diagState: 'failed',
            _diagMd: ev.answer ?? m._diagMd,
          }))
        }

        const line = activityLine(ev, t)

        if (!line) return

        activity.push(line)

        patchLast((m) => ({ ...m, activity: [...activity] }))
      })
    } catch (e) {
      if (sawCancelled) return

      patchLast(() => ({
        role: 'assistant',
        streaming: false,
        error: true,
        title: t.requestFailed,
        text: e.message,
      }))
    } finally {
      setAgentRunning(false)
      setAgentRunId(null)
      setAgentStopping(false)
    }
  }

  const stopAgent = async () => {
    if (!agentRunId || agentStopping) return

    setAgentStopping(true)

    try {
      await api.stopAgentRun(agentRunId)
    } catch (e) {
      setAgentStopping(false)
      fireToast(t.stopAgentFailed(e.message))
    }
  }

  /** Clear the conversation, stopping any in-flight run first. */
  const resetChat = () => {
    if (agentRunId) {
      api.stopAgentRun(agentRunId).catch(() => {})
    }

    setMessages([])
    setAgentRunning(false)
    setAgentRunId(null)
    setAgentStopping(false)
  }

  const recentQuestions = useMemo(() => {
    return messages
      .filter((m) => m.role === 'user')
      .slice(-5)
      .reverse()
  }, [messages])

  return {
    messages,
    patchLast,
    ask,
    stopAgent,
    resetChat,
    agentRunning,
    agentRunId,
    agentStopping,
    recentQuestions,
  }
}
