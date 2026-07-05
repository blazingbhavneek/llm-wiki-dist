import { useState } from 'react'

import { api } from '../api'
import { docFromNode } from '../data/layout'

export function describeWriteJob(job, doneText, t) {
  if (!job?.status) return t.startingWrite
  if (job.status === 'queued') {
    return job.position ? t.queuedPos(job.position) : t.queued
  }
  if (job.status === 'running') return t.writing
  if (job.status === 'done') return doneText ?? t.writeFinished
  if (job.status === 'failed') return job.error || t.writeFailed
  if (job.status === 'cancelled') return t.writeCancelled
  return t.statusOf(job.status)
}

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
          onProgress: (job) => onStatus?.(describeWriteJob(job, t.mdAdded, t)),
          onAssimilating: (msg) => {
            startAssimilationPolling()
            fireToast(msg)
          },
        },
      )

      await reload()
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
    answerWriteStatuses,
    setAnswerWriteStatus,
    saveNode,
    deleteNode,
    uploadMarkdown,
    addDraftToGraph,
    addWiki,
  }
}
