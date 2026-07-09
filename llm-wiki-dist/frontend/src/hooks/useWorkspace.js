import { useRef, useState } from 'react'

import { api } from '../api'
import { docFromNode } from '../data/layout'
import { reconstructDocument } from '../data/docs'

/**
 * The single markdown workspace shown in the center area, plus the
 * center-view routing state and back-history it participates in.
 */
export function useWorkspace({ t, fireToast, setFocusIds }) {
  const [workspace, setWorkspace] = useState(null)
  const [centerHistory, setCenterHistory] = useState([])
  // chat | graph | upload | glossary | settings | markdown | search
  const [centerView, setCenterView] = useState('chat')

  const draftSeq = useRef(0)

  const pushCenterHistory = () => {
    if (!centerView) return
    if (centerView === 'markdown' && !workspace) return

    const entry =
      centerView === 'markdown' ? { centerView, workspace } : { centerView }

    setCenterHistory((prev) => {
      const last = prev[prev.length - 1]

      if (
        last?.centerView === entry.centerView &&
        last?.workspace?.id === entry.workspace?.id
      ) {
        return prev
      }

      return [...prev, entry].slice(-20)
    })
  }

  const updateWorkspace = (id, patch) => {
    setWorkspace((prev) => {
      if (!prev || prev.id !== id) return prev

      return { ...prev, ...patch }
    })
  }

  const closeWorkspace = () => {
    setWorkspace(null)
    setCenterHistory([])
    setCenterView('chat')
  }

  const openWorkspace = (next, options = {}) => {
    const shouldPush =
      options.pushHistory !== false &&
      (centerView !== 'markdown' || workspace?.id !== next?.id)

    if (shouldPush) {
      pushCenterHistory()
    }

    setWorkspace(next)
    setCenterView('markdown')
  }

  const goBackFromWorkspace = () => {
    const target = centerHistory[centerHistory.length - 1]

    if (!target) return

    setCenterHistory((prev) => prev.slice(0, -1))

    if (target.centerView === 'markdown' && target.workspace) {
      setWorkspace(target.workspace)
      setCenterView('markdown')
      return
    }

    setWorkspace(null)
    setCenterView(target.centerView || 'chat')
  }

  const openNode = (n) => {
    const built = docFromNode(n)
    const id = `doc:${n.id}`

    openWorkspace({
      id,
      kind: 'doc',
      nodeId: n.id,
      title: built.title,
      doc: built,
      draft: {
        title: built.title,
        markdown: built.markdown,
      },
      editing: false,
      busy: false,
      busyMessage: '',
    })

    setFocusIds(new Set([n.id]))
  }

  const openNodeById = async (id) => {
    try {
      const node = await api.node(id)
      openNode(node)
    } catch (e) {
      fireToast(t.couldNotOpen(e.message))
    }
  }

  const openSearchResult = async (nodeOrId) => {
    if (!nodeOrId) return

    if (typeof nodeOrId === 'string') {
      await openNodeById(nodeOrId)
      return
    }

    if (nodeOrId.id) {
      await openNodeById(nodeOrId.id)
      return
    }

    openNode(nodeOrId)
  }

  const openFullDoc = (doc) => {
    const { markdown, positions } = reconstructDocument(doc)

    const id = `fulldoc:${doc.name}`

    const built = {
      title: doc.name,
      badge: doc.type === 'exogenous' ? t.agentNote : t.sourceNote,
      meta: t.fullDocMeta(doc.nodes.length),
      markdown,
    }

    openWorkspace({
      id,
      kind: 'fulldoc',
      title: doc.name,
      doc: built,
      draft: {
        title: built.title,
        markdown,
      },
      positions,
      editing: false,
      busy: false,
      busyMessage: '',
    })

    setFocusIds(new Set(doc.nodes.map((n) => n.id)))
  }

  const openDraft = ({
    title,
    filename,
    markdown,
    sourceType = 'endogenous',
    sourcePath,
    sourceRanges,
  }) => {
    draftSeq.current += 1

    const id = `draft:${draftSeq.current}`
    const draftTitle = title || filename || t.untitled
    const badge = sourceType === 'exogenous' ? t.agentNote : t.sourceNote

    const built = {
      title: draftTitle,
      badge,
      meta: t.unsavedDraft,
      markdown,
    }

    openWorkspace({
      id,
      kind: 'draft',
      title: draftTitle,
      sourceType,
      sourceName: filename || draftTitle,
      sourcePath,
      sourceRanges,
      doc: built,
      draft: {
        title: draftTitle,
        markdown,
      },
      editing: false,
      busy: false,
      busyMessage: '',
    })
  }

  const startEdit = (id) => updateWorkspace(id, { editing: true })

  const cancelEdit = (id) => {
    setWorkspace((prev) => {
      if (!prev || prev.id !== id) return prev

      return {
        ...prev,
        editing: false,
        draft: {
          title: prev.doc?.title || prev.draft?.title || '',
          markdown: prev.doc?.markdown ?? prev.draft?.markdown ?? '',
        },
      }
    })
  }

  const changeTitle = (id, title) => {
    setWorkspace((prev) => {
      if (!prev || prev.id !== id) return prev

      return {
        ...prev,
        draft: { ...prev.draft, title },
      }
    })
  }

  const changeBody = (id, markdown) => {
    setWorkspace((prev) => {
      if (!prev || prev.id !== id) return prev

      return {
        ...prev,
        draft: { ...prev.draft, markdown },
      }
    })
  }

  return {
    workspace,
    setWorkspace,
    centerView,
    setCenterView,
    centerHistory,
    updateWorkspace,
    closeWorkspace,
    openWorkspace,
    goBackFromWorkspace,
    openNode,
    openNodeById,
    openSearchResult,
    openFullDoc,
    openDraft,
    startEdit,
    cancelEdit,
    changeTitle,
    changeBody,
  }
}
