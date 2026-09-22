import { useEffect, useState } from 'react'

import { api } from '../api'

const CHAT_HISTORY_KEY = 'llm-wiki-chat-history'

function activityLine(ev, t) {
  const who = ev.agent ? t.explorer(ev.agent) : null
  const nm = (n) => n?.title || n?.id || '…'

  switch (ev.type) {
    case 'search':
      return t.searching(who, ev.query)
    case 'candidates':
      return t.pagesFound(ev.count)
    case 'map':
      return t.mapScanned(ev.documents, ev.pages, ev.selected)
    case 'budget':
      return ev.message ? t.budgetNote(ev.pages_used) : null
    case 'budget_search_exhausted':
      return t.budgetSearch
    case 'queued_for_agent':
      return t.queuedForAgent
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
  const [chatId, setChatId] = useState(newChatId)
  const [savedChats, setSavedChats] = useState(loadSavedChats)
  const [agentRunning, setAgentRunning] = useState(false)
  const [agentRunId, setAgentRunId] = useState(null)
  const [agentStopping, setAgentStopping] = useState(false)

  useEffect(() => {
    const firstQuestion = messages.find((message) => message.role === 'user')?.text?.trim()
    if (!firstQuestion) return

    setSavedChats((prev) => {
      const next = [
        {
          id: chatId,
          title: firstQuestion.slice(0, 80),
          messages,
        },
        ...prev.filter((chat) => chat.id !== chatId),
      ]
      saveChats(next)
      return next
    })
  }, [chatId, messages])

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
      const { context, citedNodeIds } = buildConversationContext(messages)

      await api.askStream(clean, overrides, context, citedNodeIds, (ev) => {
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
            text: ev.detail || ev.message || t.requestFailed,
          }))
        }

        if (ev.type === 'map') {
          patchLast((m) => ({ ...m, map: { documents: ev.documents, pages: ev.pages, nodes: ev.nodes || [] } }))
        } else if (ev.type === 'subagent_start' || ev.type === 'read') {
          const id = ev.node?.id
          if (id) patchLast((m) => ({ ...m, visitedIds: [...new Set([...(m.visitedIds || []), id])] }))
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
    setChatId(newChatId())
    setAgentRunning(false)
    setAgentRunId(null)
    setAgentStopping(false)
  }

  const openChat = (id) => {
    const saved = savedChats.find((chat) => chat.id === id)
    if (!saved) return
    if (agentRunId) api.stopAgentRun(agentRunId).catch(() => {})

    setChatId(saved.id)
    setMessages(saved.messages.map((message) => ({ ...message, streaming: false })))
    setAgentRunning(false)
    setAgentRunId(null)
    setAgentStopping(false)
  }

  const deleteChat = (id) => {
    setSavedChats((prev) => {
      const next = prev.filter((chat) => chat.id !== id)
      saveChats(next)
      return next
    })
    if (id === chatId) resetChat()
  }

  const clearChats = () => {
    saveChats([])
    setSavedChats([])
    resetChat()
  }

  return {
    messages,
    patchLast,
    ask,
    stopAgent,
    resetChat,
    openChat,
    deleteChat,
    clearChats,
    savedChats,
    chatId,
    agentRunning,
    agentRunId,
    agentStopping,
  }
}

function newChatId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
}

function loadSavedChats() {
  try {
    const value = JSON.parse(window.localStorage.getItem(CHAT_HISTORY_KEY) || '[]')
    return Array.isArray(value)
      ? value.filter((chat) => chat?.id && Array.isArray(chat.messages))
      : []
  } catch {
    return []
  }
}

function saveChats(chats) {
  try {
    window.localStorage.setItem(CHAT_HISTORY_KEY, JSON.stringify(chats))
  } catch {
    // Keep the current chat usable if browser storage is unavailable or full.
  }
}

function buildConversationContext(messages) {
  const citedNodeIds = new Set()
  const turns = []

  for (const message of messages) {
    if (message.role === 'user' && message.text) {
      turns.push(`User: ${message.text}`)
      continue
    }
    if (message.role !== 'assistant' || message.streaming) continue

    const answer = message.answer?.markdown || message.text || ''
    if (answer) turns.push(`Assistant: ${answer}`)

    for (const id of message.answer?.citedIds || []) citedNodeIds.add(id)
    for (const ref of message.answer?.refs || message.refs || []) {
      if (ref?.id) citedNodeIds.add(ref.id)
    }
  }

  return {
    context: turns.join('\n\n').slice(-30000),
    citedNodeIds: [...citedNodeIds],
  }
}
