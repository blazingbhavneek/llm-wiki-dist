import { useState } from 'react'

import { api } from '../api'
import { docFromNode } from '../data/layout'

export function describeWriteJob(job, doneText, t) {
  if (!job?.status) return t.startingWrite
  if (job.status === 'queued') {
    return job.position ? t.queuedPos(job.position) : t.queued
  }
  if (job.status === 'running') {
    // Long jobs (chunk_and_ingest) report live stage progress.
    const p = job.progress
    if (p?.stage) {
      const counter =
        p.current != null && p.total != null ? ` ${p.current}/${p.total}` : ''
      return t.chunkProgress(`${p.stage}${counter}`)
    }
    return t.writing
  }
  if (job.status === 'done') return doneText ?? t.writeFinished
  if (job.status === 'failed') return job.error || t.writeFailed
  if (job.status === 'cancelled') return t.writeCancelled
  return t.statusOf(job.status)
}

// chunk_and_ingest returns {chunked: true, ingested: N} instead of a node.
export function isChunkedResult(result) {
  return !!(result && typeof result === 'object' && result.chunked)
}

// Big documents chunk + ingest + enrich in one job; the default 10-minute
// poll timeout is far too short for them.
const BIG_JOB_TIMEOUT_MS = 24 * 60 * 60 * 1000

/**
 * All write-queue operations (node update/delete, markdown upload, saving
 * drafts and agent answers to the graph) plus the per-answer save-status
 * bookkeeping the chat panel renders.
 */
export function useGraphWrites({
  t,
  fireToast,
  reload,
  startAssimilationPolling,
  workspace,
  setWorkspace,
  updateWorkspace,
  closeWorkspace,
  openNode,
  setFocusIds,
}) {
  const [savedIds, setSavedIds] = useState(() => new Set())
  const [addingIds, setAddingIds] = useState(() => new Set())
  const [deletingDocs, setDeletingDocs] = useState(() => new Set())
  const [answerWriteStatuses, setAnswerWriteStatuses] = useState(() => new Map())

  const setAnswerWriteStatus = (answerId, text) => {
    if (!answerId) return

    setAnswerWriteStatuses((prev) => {
      const next = new Map(prev)

      if (text) {
        next.set(answerId, text)
      } else {
        next.delete(answerId)
      }

      return next
    })
  }

  const saveNode = async (item) => {
    startAssimilationPolling()

    updateWorkspace(item.id, {
      busy: true,
      busyMessage: t.startingUpdate,
    })

    try {
      const node = await api.updateNode(item.nodeId, item.draft.markdown, {
        onProgress: (job) => {
          updateWorkspace(item.id, {
            busyMessage: describeWriteJob(job, t.nodeUpdated, t),
          })
        },
      })

      await reload()

      const built = docFromNode(node)
      const nextId = `doc:${node.id}`

      setWorkspace((prev) => {
        if (!prev || prev.id !== item.id) return prev

        return {
          ...prev,
          id: nextId,
          nodeId: node.id,
          title: built.title,
          doc: built,
          draft: {
            title: built.title,
            markdown: built.markdown,
          },
          editing: false,
          busy: false,
          busyMessage: '',
        }
      })

      setFocusIds(new Set([node.id]))
      fireToast(t.savedNodeUpdated)
    } catch (e) {
      updateWorkspace(item.id, { busy: false, busyMessage: '' })

      fireToast(t.updateFailed(e.message))
    }
  }

  const deleteNode = async (item) => {
    updateWorkspace(item.id, {
      busy: true,
      busyMessage: t.startingDelete,
    })

    try {
      await api.deleteNode(item.nodeId, {
        onProgress: (job) => {
          updateWorkspace(item.id, {
            busyMessage: describeWriteJob(job, t.nodeDeleted, t),
          })
        },
      })

      await reload()
      closeWorkspace()
      fireToast(t.noteDeleted)
    } catch (e) {
      updateWorkspace(item.id, { busy: false, busyMessage: '' })

      fireToast(t.deleteFailed(e.message))
    }
  }

  // Whole-document delete: one write job on the backend, whatever the chunk
  // count. Closes the open workspace if it was showing part of this document.
  const deleteDocument = async (doc) => {
    const name = doc?.name

    if (!name) return

    setDeletingDocs((prev) => new Set(prev).add(name))

    const nodeIds = (Array.isArray(doc?.nodes) ? doc.nodes : [])
      .map((n) => n?.id)
      .filter(Boolean)

    try {
      // Send both: the name resolves every chunk the DB still has under it,
      // the ids cover agent-note groups that exist only as a client grouping.
      await api.deleteDocument({ documentName: name, nodeIds })

      const deletedIds = new Set(nodeIds)

      const showingDeletedDoc =
        workspace &&
        (workspace.id === `fulldoc:${name}` || deletedIds.has(workspace.nodeId))

      await reload()

      if (showingDeletedDoc) closeWorkspace()

      fireToast(t.documentDeleted(name))
    } catch (e) {
      fireToast(t.deleteFailed(e.message))
    } finally {
      setDeletingDocs((prev) => {
        const next = new Set(prev)
        next.delete(name)
        return next
      })
    }
  }

  const uploadMarkdown = async ({ filename, markdown }, onStatus) => {
    try {
      startAssimilationPolling()

      onStatus?.(t.startingWriteGraph)

      const node = await api.createDocument(
        {
          body: markdown,
          title: (filename || t.untitled).replace(/\.(md|markdown)$/i, ''),
          documentName: filename,
        },
        {
          timeoutMs: BIG_JOB_TIMEOUT_MS,
          onProgress: (job) => onStatus?.(describeWriteJob(job, t.mdAdded, t)),
          onAssimilating: (msg) => {
            startAssimilationPolling()
            fireToast(msg)
          },
        },
      )

      await reload()

      if (isChunkedResult(node)) {
        // Big-document path: many nodes were created; nothing to open.
        onStatus?.(t.chunkedAdded(node.ingested))
        fireToast(t.chunkedAdded(node.ingested))
        return node
      }

      openNode(node)

      onStatus?.(t.mdAddedOpened)
      fireToast(t.mdAdded)

      return node
    } catch (e) {
      fireToast(t.addFailed(e.message))
      throw e
    }
  }

  const addDraftToGraph = async (item) => {
    startAssimilationPolling()
    updateWorkspace(item.id, {
      busy: true,
      busyMessage: t.startingWriteGraph,
    })

    if (item.answer?.id) {
      setAddingIds((prev) => new Set(prev).add(item.answer.id))
      setAnswerWriteStatus(item.answer.id, t.startingWriteGraph)
    }

    try {
      const exogenousQuestion =
        item.answer?.question ||
        item.question ||
        item.draft?.question ||
        item.draft?.title ||
        ''

      const exogenousOriginPrefix = item.kind === 'answer' ? 'agent' : 'human'

      const node =
        item.sourceType === 'exogenous'
          ? await api.createExogenous(
              item.draft.markdown,
              item.sourceIds || [],
              `${exogenousOriginPrefix}:${exogenousQuestion.slice(0, 60)}`,
              {
                onProgress: (job) => {
                  const text = describeWriteJob(job, t.addedToGraph, t)

                  updateWorkspace(item.id, { busyMessage: text })

                  if (item.answer?.id) {
                    setAnswerWriteStatus(item.answer.id, text)
                  }
                },
                onAssimilating: (msg) => {
                  startAssimilationPolling()
                  fireToast(msg)
                },
              },
              {
                question: exogenousQuestion || null,
              },
            )
          : await api.createDocument(
              {
                body: item.draft.markdown,
                title: item.draft.title,
                documentName: item.sourceName || item.draft.title,
                sourcePath: item.sourcePath,
                sourceRanges: item.sourceRanges,
              },
              {
                timeoutMs: BIG_JOB_TIMEOUT_MS,
                onProgress: (job) => {
                  updateWorkspace(item.id, {
                    busyMessage: describeWriteJob(job, t.addedToGraph, t),
                  })
                },
                onAssimilating: (msg) => {
                  startAssimilationPolling()
                  fireToast(msg)
                },
              },
            )

      await reload()

      if (item.answer?.id) {
        setSavedIds((prev) => new Set(prev).add(item.answer.id))
        setAnswerWriteStatus(item.answer.id, t.savedOpened)
      }

      if (isChunkedResult(node)) {
        // Big-document path: the draft became many nodes; close the draft
        // instead of trying to open a single node.
        closeWorkspace()
        fireToast(t.chunkedAdded(node.ingested))
        return
      }

      openNode(node)
      fireToast(t.addedToGraph)
    } catch (e) {
      updateWorkspace(item.id, { busy: false, busyMessage: '' })

      if (item.answer?.id) {
        setAnswerWriteStatus(item.answer.id, '')
      }

      fireToast(t.addFailed(e.message))
    } finally {
      if (item.answer?.id) {
        setAddingIds((prev) => {
          const next = new Set(prev)
          next.delete(item.answer.id)
          return next
        })
      }
    }
  }

  const addWiki = async (answer) => {
    if (!answer || savedIds.has(answer.id) || addingIds.has(answer.id)) return

    startAssimilationPolling()

    const answerWorkspace =
      workspace?.kind === 'answer' && workspace.answer?.id === answer.id
        ? workspace
        : null

    const markdown = answerWorkspace?.draft?.markdown ?? answer.markdown

    const title =
      answerWorkspace?.draft?.title ?? answer.title ?? answer.question ?? t.answer

    const answerQuestion = answer.question || title || ''

    const citedIds = answerWorkspace?.sourceIds || answer.citedIds || []

    setAddingIds((prev) => new Set(prev).add(answer.id))
    setAnswerWriteStatus(answer.id, t.startingWriteGraph)

    if (answerWorkspace) {
      updateWorkspace(answerWorkspace.id, {
        busy: true,
        busyMessage: t.startingWriteGraph,
      })
    }

    try {
      const node = await api.createExogenous(
        markdown,
        citedIds,
        `agent:${answerQuestion.slice(0, 60)}`,
        {
          onProgress: (job) => {
            const text = describeWriteJob(job, t.savedToGraph, t)

            setAnswerWriteStatus(answer.id, text)

            if (answerWorkspace) {
              updateWorkspace(answerWorkspace.id, { busyMessage: text })
            }
          },
          onAssimilating: (msg) => {
            startAssimilationPolling()
            fireToast(msg)
          },
        },
        {
          question: answerQuestion || null,
        },
      )

      await reload()

      setSavedIds((prev) => new Set(prev).add(answer.id))
      setAnswerWriteStatus(answer.id, t.savedOpened)

      openNode(node)
      setFocusIds(new Set([node.id, ...citedIds]))

      fireToast(t.savedWikiNote)
    } catch (e) {
      setAnswerWriteStatus(answer.id, '')

      if (answerWorkspace) {
        updateWorkspace(answerWorkspace.id, { busy: false, busyMessage: '' })
      }

      fireToast(t.saveFailed(e.message))
    } finally {
      setAddingIds((prev) => {
        const next = new Set(prev)
        next.delete(answer.id)
        return next
      })
    }
  }

  return {
    savedIds,
    addingIds,
    deletingDocs,
    answerWriteStatuses,
    setAnswerWriteStatus,
    saveNode,
    deleteNode,
    deleteDocument,
    uploadMarkdown,
    addDraftToGraph,
    addWiki,
  }
}
