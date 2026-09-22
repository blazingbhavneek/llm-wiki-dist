import { useState } from 'react'

import { api } from '../api'

function docFromNode(n) {
  return {
    title: n.title || n.entity || n.id,
    badge: 'source',
    meta: n.document || n.path || '',
    markdown: n.body || `# ${n.title || n.id}\n\n${n.summary || ''}`,
    source_path: n.source_path || n.path || '',
    links: Array.isArray(n.links) ? n.links : [],
  }
}

export function useWorkspace({ t, fireToast, setFocusIds, rememberNodes }) {
  const [workspace, setWorkspace] = useState(null)
  const [centerHistory, setCenterHistory] = useState([])
  const [centerView, setCenterView] = useState('chat')

  const pushCenterHistory = () => {
    if (!centerView) return
    if ((centerView === 'markdown' || centerView === 'document') && !workspace) return

    const entry = centerView === 'markdown' || centerView === 'document' ? { centerView, workspace } : { centerView }
    setCenterHistory((prev) => {
      const last = prev[prev.length - 1]
      if (last?.centerView === entry.centerView && last?.workspace?.id === entry.workspace?.id) return prev
      return [...prev, entry].slice(-20)
    })
  }

  const closeWorkspace = () => {
    setWorkspace(null)
    setCenterHistory([])
    setCenterView('chat')
  }

  const openWorkspace = (next, options = {}) => {
    const shouldPush = options.pushHistory !== false && (centerView !== 'markdown' || workspace?.id !== next?.id)
    if (shouldPush) pushCenterHistory()
    setWorkspace(next)
    setCenterView('markdown')
  }

  const goBackFromWorkspace = () => {
    const target = centerHistory[centerHistory.length - 1]
    if (!target) return
    setCenterHistory((prev) => prev.slice(0, -1))
    if ((target.centerView === 'markdown' || target.centerView === 'document') && target.workspace) {
      setWorkspace(target.workspace)
      setCenterView(target.centerView)
      return
    }
    setWorkspace(null)
    setCenterView(target.centerView || 'chat')
  }

  const openNode = (n) => {
    if (!n?.id) return
    rememberNodes?.(n)
    const built = docFromNode(n)
    openWorkspace({ id: `doc:${n.id}`, kind: 'doc', nodeId: n.id, node: n, title: built.title, doc: built })
    setFocusIds?.(new Set([n.id]))
  }

  const openNodeById = async (id) => {
    try {
      const node = await api.node(id)
      openNode(node)
    } catch (e) {
      fireToast?.(t.couldNotOpen(e.message))
    }
  }

  const openSearchResult = async (nodeOrId) => {
    if (!nodeOrId) return
    if (typeof nodeOrId === 'string') return openNodeById(nodeOrId)
    if (nodeOrId.id) return openNodeById(nodeOrId.id)
    openNode(nodeOrId)
  }

  const openDocument = (folder) => {
    const path = folder?.path || folder
    const shouldPush = centerView !== 'document' || workspace?.path !== path
    if (shouldPush) pushCenterHistory()
    setWorkspace({ id: `document:${path}`, kind: 'document', path, title: folder?.title || String(path).split('/').filter(Boolean).pop() })
    setCenterView('document')
  }

  return {
    workspace,
    setWorkspace,
    centerView,
    setCenterView,
    centerHistory,
    pushCenterHistory,
    closeWorkspace,
    openWorkspace,
    goBackFromWorkspace,
    openNode,
    openNodeById,
    openSearchResult,
    openDocument,
  }
}
