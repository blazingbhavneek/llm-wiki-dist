import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ChatPanel from './components/ChatPanel'
import GraphCanvas from './components/GraphCanvas'
import MarkdownView from './components/MarkdownView'
import ErrorBoundary from './components/ErrorBoundary'
import DocSidebar from './components/DocSidebar'
import SettingsView from './components/SettingsView'
import UploadView from './components/UploadView'
import { api } from './api'
import { useT, LangToggle } from './i18n.jsx'
import { layoutGraph, docFromNode } from './data/layout'
import { reconstructDocument } from './data/docs'

const STR = {
  ja: {
    pinned: { home: 'スタート', graph: 'グラフ', upload: 'アップロード', settings: '設定' },
    startingWrite: '書き込みジョブを開始中...',
    queued: '書き込みジョブをキューに入れました。',
    queuedPos: (p) => `書き込みジョブをキューに入れました。順位 ${p}。`,
    writing: 'グラフに書き込み中：埋め込み、リンク、クラスタリング...',
    writeFinished: '書き込みが完了しました。',
    writeFailed: '書き込みジョブが失敗しました。',
    writeCancelled: '書き込みジョブがキャンセルされました。',
    statusOf: (s) => `書き込みジョブの状態: ${s}`,
    couldNotOpen: (m) => `ノートを開けませんでした: ${m}`,
    startingUpdate: 'グラフの更新を開始中...',
    nodeUpdated: 'ノードをグラフで更新しました。',
    savedNodeUpdated: '保存しました。ノードをグラフで更新しました。',
    updateFailed: (m) => `更新に失敗しました: ${m}`,
    startingDelete: '削除ジョブを開始中...',
    nodeDeleted: 'ノードをグラフから削除しました。',
    noteDeleted: 'ノートを削除しました。',
    deleteFailed: (m) => `削除に失敗しました: ${m}`,
    startingWriteGraph: 'グラフへの書き込みを開始中...',
    mdAdded: 'Markdown をグラフに追加しました。',
    mdAddedOpened: 'Markdown をグラフに追加し、ワークスペースで開きました。',
    addFailed: (m) => `追加に失敗しました: ${m}`,
    addedToGraph: 'グラフに追加しました。',
    savedOpened: 'グラフに保存し、ワークスペースで開きました。',
    savedToGraph: 'グラフに保存しました。',
    savedWikiNote: 'wiki ノートとして保存し、ソースにリンクしました。',
    saveFailed: (m) => `保存に失敗しました: ${m}`,
    noMatch: '一致するノートはありません。',
    searchFailed: (m) => `検索に失敗しました: ${m}`,
    agentNote: 'エージェントノート',
    sourceNote: 'ソースノート',
    untitled: '無題',
    answer: '回答',
    fullDocMeta: (n) => `${n} ページから再構成した完全なドキュメント。`,
    unsavedDraft: '未保存の下書き。',
    answerMetaSteps: (n) => `${n} ステップで生成。確認・編集してから wiki に追加してください。`,
    answerMeta: '確認・編集してから wiki に追加してください。',
    explorer: (n) => `エクスプローラー ${n}`,
    searching: (who, q) => (who ? `${who} · “${q}” を検索中` : `“${q}” を検索中`),
    pagesFound: (c) => `${c} 件のページが見つかりました`,
    spawned: (n) => `${n} 人のエクスプローラーを起動`,
    exploring: (who, n) => `${who} · ${n} を探索中`,
    reading: (who, n) => `${who} · ${n} を読み中`,
    following: (who, n, ne) => `${who} · ${n} からリンクをたどり中 (${ne})`,
    subDone: (who, c) => `${who} · 完了 (${c} ソース)`,
    compiling: '回答をコンパイル中…',
    diagramBuilding: '図を作成中…',
    diagramReady: '図の準備完了',
    diagramFailed: '図を描画できませんでした',
    working: '作業中…',
    answerReady: (steps) => `${steps} ステップで回答ができました。`,
    openedReview: 'ワークスペースで開きました →。確認してから wiki に追加してください。',
    foundNoBody: 'ソースは見つかりましたが、回答本文がありません。',
    foundNoBodyText: (c, steps) => `エージェントは ${steps} ステップで ${c} 件のソースを集めましたが、最終的な回答を生成しませんでした。`,
    requestFailed: 'リクエストが失敗しました',
    collapseDocs: 'ドキュメントを隠す',
    showDocs: 'ドキュメントを表示',
    loadingGraph: 'グラフを読み込み中…',
    cannotReach: 'バックエンドに接続できません',
    closeTab: 'タブを閉じる',
    nothingOpen: '何も開いていません',
    homeHint: (hasDocs) =>
      `左のパネルからドキュメントを開くか${hasDocs ? '' : '（取り込み後）'}、チャットでエージェントに作成を依頼してください。`,
    showDocuments: 'ドキュメントを表示',
    assimTitle: '新しいノードは今すぐ検索できます。ランキングとクラスタリングはバックグラウンドで進行中です。',
    assimBadge: 'グラフを整理中…',
  },
  en: {
    pinned: { home: 'Start', graph: 'Graph', upload: 'Upload', settings: 'Settings' },
    startingWrite: 'Starting write job...',
    queued: 'Queued write job.',
    queuedPos: (p) => `Queued write job. Position ${p}.`,
    writing: 'Writing to the graph: embedding, linking, and clustering...',
    writeFinished: 'Write finished.',
    writeFailed: 'Write job failed.',
    writeCancelled: 'Write job was cancelled.',
    statusOf: (s) => `Write job status: ${s}`,
    couldNotOpen: (m) => `Could not open note: ${m}`,
    startingUpdate: 'Starting graph update...',
    nodeUpdated: 'Node updated in the graph.',
    savedNodeUpdated: 'Saved. Node updated in the graph.',
    updateFailed: (m) => `Update failed: ${m}`,
    startingDelete: 'Starting delete job...',
    nodeDeleted: 'Node deleted from the graph.',
    noteDeleted: 'Note deleted.',
    deleteFailed: (m) => `Delete failed: ${m}`,
    startingWriteGraph: 'Starting graph write...',
    mdAdded: 'Markdown added to the graph.',
    mdAddedOpened: 'Markdown added to the graph and opened in the workspace.',
    addFailed: (m) => `Add failed: ${m}`,
    addedToGraph: 'Added to the graph.',
    savedOpened: 'Saved to the graph and opened in the workspace.',
    savedToGraph: 'Saved to the graph.',
    savedWikiNote: 'Saved as a wiki note, linked to its sources.',
    saveFailed: (m) => `Save failed: ${m}`,
    noMatch: 'No matching notes.',
    searchFailed: (m) => `Search failed: ${m}`,
    agentNote: 'Agent note',
    sourceNote: 'Source note',
    untitled: 'Untitled',
    answer: 'Answer',
    fullDocMeta: (n) => `Full document reconstructed from ${n} pages.`,
    unsavedDraft: 'Unsaved draft.',
    answerMetaSteps: (n) => `Generated in ${n} steps. Review, edit, then add it to the wiki.`,
    answerMeta: 'Review, edit, then add it to the wiki.',
    explorer: (n) => `Explorer ${n}`,
    searching: (who, q) => (who ? `${who} · searching “${q}”` : `searching “${q}”`),
    pagesFound: (c) => `${c} pages found`,
    spawned: (n) => `spawned ${n} explorers`,
    exploring: (who, n) => `${who} · exploring ${n}`,
    reading: (who, n) => `${who} · reading ${n}`,
    following: (who, n, ne) => `${who} · following links from ${n} (${ne})`,
    subDone: (who, c) => `${who} · done (${c} sources)`,
    compiling: 'compiling answer…',
    diagramBuilding: 'building diagram…',
    diagramReady: 'diagram ready',
    diagramFailed: 'could not render diagram',
    working: 'Working…',
    answerReady: (steps) => `Answer ready in ${steps} steps.`,
    openedReview: 'Opened in the workspace →. Review it, then add it to the wiki.',
    foundNoBody: 'Found sources, but no answer body.',
    foundNoBodyText: (c, steps) => `The agent gathered ${c} sources in ${steps} steps but produced no final answer.`,
    requestFailed: 'Request failed',
    collapseDocs: 'Collapse documents',
    showDocs: 'Show documents',
    loadingGraph: 'Loading graph…',
    cannotReach: 'Cannot reach the backend',
    closeTab: 'Close tab',
    nothingOpen: 'Nothing open',
    homeHint: (hasDocs) =>
      `Open a document from the left panel${hasDocs ? '' : ' (once you ingest some)'}, or ask the agent in the chat to write one.`,
    showDocuments: 'Show documents',
    assimTitle: 'New nodes are searchable now; ranking and clustering are catching up in the background.',
    assimBadge: 'Assimilating graph…',
  },
}

const pdfApiBase = 'http://localhost:51025'

const PINNED = [
  { id: 'home', label: 'Start', dot: 'bg-slate-400' },
  { id: 'graph', label: 'Graph', dot: 'bg-blue' },
  { id: 'upload', label: 'Upload', dot: 'bg-green' },
  { id: 'settings', label: 'Settings', dot: 'bg-slate-500' },
]
const PINNED_IDS = new Set(PINNED.map((p) => p.id))

function describeWriteJob(job, doneText, t) {
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

export default function App() {
  const [raw, setRaw] = useState({ nodes: [], edges: [] })
  const [health, setHealth] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [sidebarOpen, setSidebarOpen] = useState(true)

  const [tabs, setTabs] = useState([]) // dynamic tabs (doc/draft/fulldoc/answer)
  const [activeId, setActiveId] = useState('home')
  const draftSeq = useRef(0)

  // chat
  const [messages, setMessages] = useState([])
  const [activeAnswerId, setActiveAnswerId] = useState(null)
  const [savedIds, setSavedIds] = useState(() => new Set())
  const [addingIds, setAddingIds] = useState(() => new Set()) // answers being written to graph
  const [answerWriteStatuses, setAnswerWriteStatuses] = useState(() => new Map())
  const answerSeq = useRef(0)

  const [focusIds, setFocusIds] = useState(null)
  const [toast, setToast] = useState(null)
  const [assimPending, setAssimPending] = useState(0)

  // Per-request agent overrides, held per browser (not global server state).
  // Sent with every ask; unset keys fall back to backend defaults.
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

  const rawById = useMemo(() => new Map(raw.nodes.map((n) => [n.id, n])), [raw])
  // Graph highlights are driven only by what's open + the current answer.
  const openIds = useMemo(
    () => new Set(tabs.filter((t) => t.kind === 'doc').map((t) => t.nodeId)),
    [tabs],
  )
  const answerIds = useMemo(() => {
    const ans = [...tabs].reverse().find((t) => t.kind === 'answer')
    return new Set(ans?.answer?.citedIds || [])
  }, [tabs])
  const graph = useMemo(() => layoutGraph(raw.nodes, raw.edges), [raw])

  const activeTab = tabs.find((t) => t.id === activeId) || null
  const activeNodeId = activeTab?.kind === 'doc' ? activeTab.nodeId : null

  const t = useT(STR)

  const fireToast = (text) => {
    setToast(text)
    setTimeout(() => setToast(null), 3600)
  }

  // Poll background-assimilation backlog for the 'still catching up' badge.
  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const res = await api.assimilation()
        if (alive) setAssimPending(res?.pending ?? 0)
      } catch {
        /* endpoint optional; ignore */
      }
    }
    poll()
    const timer = setInterval(poll, 4000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [])

  const setAnswerWriteStatus = (answerId, text) => {
    if (!answerId) return
    setAnswerWriteStatuses((prev) => {
      const next = new Map(prev)
      if (text) next.set(answerId, text)
      else next.delete(answerId)
      return next
    })
  }

  const reload = useCallback(async () => {
    const [g, h] = await Promise.all([api.graph(), api.health()])
    setRaw({ nodes: g.nodes, edges: g.edges })
    setHealth(h)
    return g
  }, [])

  useEffect(() => {
    reload()
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false))
  }, [reload])

  // --- tab helpers ---------------------------------------------------------
  const updateTab = (id, patch) =>
    setTabs((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)))

  const upsertTab = (tab) => {
    setTabs((prev) => (prev.some((t) => t.id === tab.id) ? prev : [...prev, tab]))
    setActiveId(tab.id)
  }

  const closeTab = (id) => {
    setTabs((prev) => {
      const idx = prev.findIndex((t) => t.id === id)
      const next = prev.filter((t) => t.id !== id)
      if (activeId === id) {
        const fallback = next[idx] || next[idx - 1]
        setActiveId(fallback ? fallback.id : 'home')
      }
      return next
    })
  }

  const openNode = (n) => {
    const built = docFromNode(n)
    const id = `doc:${n.id}`
    setTabs((prev) =>
      prev.some((t) => t.id === id)
        ? prev.map((t) => (t.id === id ? { ...t, doc: built, draft: { title: built.title, markdown: built.markdown }, editing: false } : t))
        : [...prev, {
            id,
            kind: 'doc',
            nodeId: n.id,
            title: built.title,
            doc: built,
            draft: { title: built.title, markdown: built.markdown },
            editing: false,
            busy: false,
          }],
    )
    setActiveId(id)
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

  const openFullDoc = (doc) => {
    const { markdown, positions } = reconstructDocument(doc)
    const id = `fulldoc:${doc.name}`
    const built = {
      title: doc.name,
      badge: doc.type === 'exogenous' ? 'agent' : 'source',
      meta: t.fullDocMeta(doc.nodes.length),
      markdown,
    }
    upsertTab({ id, kind: 'fulldoc', title: doc.name, doc: built, draft: { title: built.title, markdown }, positions })
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
    const badge = sourceType === 'exogenous' ? 'agent' : 'source'
    const built = { title: draftTitle, badge, meta: t.unsavedDraft, markdown }
    upsertTab({
      id,
      kind: 'draft',
      title: draftTitle,
      sourceType,
      sourceName: filename || draftTitle,
      sourcePath,
      sourceRanges,
      doc: built,
      draft: { title: draftTitle, markdown },
      editing: false,
      busy: false,
    })
  }

  const openAnswerTab = (answer) => {
    if (!answer) return
    const id = `answer:${answer.id}`
    const title = answer.title || answer.question || t.answer
    const markdown = answer.markdown || ''
    upsertTab({
      id,
      kind: 'answer',
      title: title.slice(0, 28),
      sourceType: 'exogenous',
      sourceIds: answer.citedIds || [],
      sourceName: title,
      answer,
      doc: {
        title,
        badge: 'agent',
        meta: answer.steps ? t.answerMetaSteps(answer.steps) : t.answerMeta,
        markdown,
      },
      draft: { title, markdown },
      editing: false,
      busy: false,
      busyMessage: '',
      refs: answer.refs || [],
    })
    setActiveAnswerId(answer.id)
    setFocusIds(new Set(answer.citedIds || []))
  }

  // --- doc editing ---------------------------------------------------------
  const startEdit = (id) => updateTab(id, { editing: true })
  const changeTitle = (id, title) =>
    setTabs((prev) => prev.map((t) => (t.id === id ? { ...t, draft: { ...t.draft, title } } : t)))
  const changeBody = (id, markdown) =>
    setTabs((prev) => prev.map((t) => (t.id === id ? { ...t, draft: { ...t.draft, markdown } } : t)))

  const saveNode = async (tab) => {
    updateTab(tab.id, { busy: true, busyMessage: t.startingUpdate })
    try {
      const node = await api.updateNode(tab.nodeId, tab.draft.markdown, {
        onProgress: (job) => updateTab(tab.id, { busyMessage: describeWriteJob(job, t.nodeUpdated, t) }),
      })
      await reload()
      const built = docFromNode(node)
      const nextId = `doc:${node.id}`
      setTabs((prev) =>
        prev.map((t) =>
          t.id === tab.id
            ? {
                ...t,
                id: nextId,
                nodeId: node.id,
                title: built.title,
                doc: built,
                draft: { title: built.title, markdown: built.markdown },
                editing: false,
                busy: false,
                busyMessage: '',
              }
            : t,
        ),
      )
      setActiveId(nextId)
      setFocusIds(new Set([node.id]))
      fireToast(t.savedNodeUpdated)
    } catch (e) {
      updateTab(tab.id, { busy: false, busyMessage: '' })
      fireToast(t.updateFailed(e.message))
    }
  }

  const deleteNode = async (tab) => {
    updateTab(tab.id, { busy: true, busyMessage: t.startingDelete })
    try {
      await api.deleteNode(tab.nodeId, {
        onProgress: (job) => updateTab(tab.id, { busyMessage: describeWriteJob(job, t.nodeDeleted, t) }),
      })
      await reload()
      closeTab(tab.id)
      fireToast(t.noteDeleted)
    } catch (e) {
      updateTab(tab.id, { busy: false, busyMessage: '' })
      fireToast(t.deleteFailed(e.message))
    }
  }

  // Markdown file upload: straight into the graph as endogenous source, then
  // open the stored note.
  const uploadMarkdown = async ({ filename, markdown }, onStatus) => {
    try {
      onStatus?.(t.startingWriteGraph)
      const node = await api.createDocument({
        body: markdown,
        title: (filename || 'untitled').replace(/\.(md|markdown)$/i, ''),
        documentName: filename,
      }, {
        onProgress: (job) => onStatus?.(describeWriteJob(job, t.mdAdded, t)),
        onAssimilating: (msg) => fireToast(msg),
      })
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

  const addDraftToGraph = async (tab) => {
    updateTab(tab.id, { busy: true, busyMessage: t.startingWriteGraph })
    if (tab.answer?.id) {
      setAddingIds((prev) => new Set(prev).add(tab.answer.id))
      setAnswerWriteStatus(tab.answer.id, t.startingWriteGraph)
    }
    try {
      const node =
        tab.sourceType === 'exogenous'
          ? await api.createExogenous(
              tab.draft.markdown,
              tab.sourceIds || [],
              `${tab.kind === 'answer' ? 'agent' : 'human'}:${tab.draft.title.slice(0, 60)}`,
              {
                onProgress: (job) => {
                  const text = describeWriteJob(job, t.addedToGraph, t)
                  updateTab(tab.id, { busyMessage: text })
                  if (tab.answer?.id) setAnswerWriteStatus(tab.answer.id, text)
                },
                onAssimilating: (msg) => fireToast(msg),
              },
            )
          : await api.createDocument({
              body: tab.draft.markdown,
              title: tab.draft.title,
              documentName: tab.sourceName || tab.draft.title,
              sourcePath: tab.sourcePath,
              sourceRanges: tab.sourceRanges,
            }, {
              onProgress: (job) => updateTab(tab.id, { busyMessage: describeWriteJob(job, t.addedToGraph, t) }),
              onAssimilating: (msg) => fireToast(msg),
            })
      await reload()
      if (tab.answer?.id) {
        setSavedIds((prev) => new Set(prev).add(tab.answer.id))
        setAnswerWriteStatus(tab.answer.id, t.savedOpened)
      }
      closeTab(tab.id)
      openNode(node)
      fireToast(t.addedToGraph)
    } catch (e) {
      updateTab(tab.id, { busy: false, busyMessage: '' })
      if (tab.answer?.id) setAnswerWriteStatus(tab.answer.id, '')
      fireToast(t.addFailed(e.message))
    } finally {
      if (tab.answer?.id) {
        setAddingIds((prev) => {
          const next = new Set(prev)
          next.delete(tab.answer.id)
          return next
        })
      }
    }
  }

  // --- chat ----------------------------------------------------------------
  const refLabel = (id) => {
    const n = rawById.get(id)
    return { id, label: n?.title || n?.entity || id, note: n?.type === 'exogenous' ? t.agentNote : t.sourceNote }
  }

  const patchLast = (fn) =>
    setMessages((prev) => {
      const copy = prev.slice()
      const i = copy.length - 1
      if (i >= 0 && copy[i].role === 'assistant') copy[i] = fn(copy[i])
      return copy
    })

  const activityLine = (ev) => {
    const who = ev.agent ? t.explorer(ev.agent) : null
    const nm = (n) => n?.title || n?.id || '…'
    switch (ev.type) {
      case 'search': return t.searching(who, ev.query)
      case 'candidates': return t.pagesFound(ev.count)
      case 'subagents_spawned': return t.spawned(ev.starts?.length || 0)
      case 'subagent_start': return t.exploring(who, nm(ev.node))
      case 'read': return t.reading(who, nm(ev.node))
      case 'follow_link': return t.following(who, nm(ev.node), ev.neighbors)
      case 'subagent_done': return t.subDone(who, ev.cited?.length || 0)
      case 'compiling': return t.compiling
      case 'diagram_pending': return t.diagramBuilding
      case 'diagram_ready': return t.diagramReady
      case 'diagram_failed': return t.diagramFailed
      default: return null
    }
  }

  const finalizeAnswer = (ans, activity, q) => {
    const cited = ans.cited_node_ids || []
    const refs = cited.map(refLabel)
    const hasAnswer = !!(ans.answer && ans.answer.trim())
    setFocusIds(new Set(cited))

    if (hasAnswer) {
      answerSeq.current += 1
      const id = answerSeq.current
      const answer = {
        id,
        question: q,
        title: q,
        markdown: ans.answer,
        refs,
        steps: ans.steps,
        citedIds: cited,
      }
      patchLast(() => ({
        role: 'assistant',
        title: t.answerReady(ans.steps),
        text: t.openedReview,
        activity,
        answer,
      }))
      openAnswerTab(answer)
    } else {
      patchLast(() => ({
        role: 'assistant',
        title: t.foundNoBody,
        text: t.foundNoBodyText(cited.length, ans.steps),
        refs,
        activity,
      }))
      if (cited[0]) openNodeById(cited[0])
    }
  }

  const ask = async (q) => {
    setMessages((prev) => [
      ...prev,
      { role: 'user', text: q },
      { role: 'assistant', streaming: true, title: t.working, activity: [] },
    ])
    const activity = []
    try {
      await api.askStream(q, overrides, (ev) => {
        if (ev.type === 'answer') return finalizeAnswer(ev, activity, q)
        if (ev.type === 'error') return patchLast(() => ({ role: 'assistant', title: t.requestFailed, text: ev.message }))
        if (ev.type === 'diagram_pending') patchLast((m) => ({ ...m, _diagState: 'pending' }))
        else if (ev.type === 'diagram_ready') patchLast((m) => ({ ...m, _diagState: 'ready', _diagMd: ev.answer ?? m._diagMd }))
        else if (ev.type === 'diagram_failed') patchLast((m) => ({ ...m, _diagState: 'failed', _diagMd: ev.answer ?? m._diagMd }))
        const line = activityLine(ev)
        if (!line) return
        activity.push(line)
        patchLast((m) => ({ ...m, activity: [...activity] }))
      })
    } catch (e) {
      patchLast(() => ({ role: 'assistant', title: t.requestFailed, text: e.message }))
    }
  }

  const addWiki = async (answer) => {
    if (!answer || savedIds.has(answer.id) || addingIds.has(answer.id)) return
    const answerTab = tabs.find((t) => t.kind === 'answer' && t.answer?.id === answer.id)
    const markdown = answerTab?.draft?.markdown ?? answer.markdown
    const title = answerTab?.draft?.title ?? answer.title ?? answer.question ?? t.answer
    const citedIds = answerTab?.sourceIds || answer.citedIds || []

    setAddingIds((prev) => new Set(prev).add(answer.id))
    setAnswerWriteStatus(answer.id, t.startingWriteGraph)
    if (answerTab) updateTab(answerTab.id, { busy: true, busyMessage: t.startingWriteGraph })
    try {
      const node = await api.createExogenous(markdown, citedIds, `agent:${title.slice(0, 60)}`, {
        onProgress: (job) => {
          const text = describeWriteJob(job, t.savedToGraph, t)
          setAnswerWriteStatus(answer.id, text)
          if (answerTab) updateTab(answerTab.id, { busyMessage: text })
        },
        onAssimilating: (msg) => fireToast(msg),
      })
      await reload()
      setSavedIds((prev) => new Set(prev).add(answer.id))
      setAnswerWriteStatus(answer.id, t.savedOpened)
      if (answerTab) updateTab(answerTab.id, { busy: false, busyMessage: t.savedToGraph })
      if (answerTab) closeTab(answerTab.id)
      openNode(node)
      setFocusIds(new Set([node.id, ...citedIds]))
      fireToast(t.savedWikiNote)
    } catch (e) {
      setAnswerWriteStatus(answer.id, '')
      if (answerTab) updateTab(answerTab.id, { busy: false, busyMessage: '' })
      fireToast(t.saveFailed(e.message))
    } finally {
      setAddingIds((prev) => {
        const next = new Set(prev)
        next.delete(answer.id)
        return next
      })
    }
  }

  const onSearch = async (text) => {
    if (!text.trim()) return
    try {
      const results = await api.search(text, 8)
      if (results.length) openNode(results[0])
      else fireToast(t.noMatch)
    } catch (e) {
      fireToast(t.searchFailed(e.message))
    }
  }

  return (
    <div className="app-shell flex h-screen w-screen overflow-hidden bg-[#eef2f7]">
      {/* activity rail */}
      <div className="flex w-[42px] shrink-0 flex-col items-center gap-2 border-r border-line bg-white py-[10px]">
        <button
          title={sidebarOpen ? t.collapseDocs : t.showDocs}
          onClick={() => setSidebarOpen((v) => !v)}
          className={`grid h-[30px] w-[30px] place-items-center rounded text-[16px] ${sidebarOpen ? 'bg-blue/10 text-[#244a9d]' : 'text-muted hover:bg-soft'}`}
        >
          ☷
        </button>
      </div>

      {/* document sidebar (animated collapse) */}
      <div
        className="h-full shrink-0 overflow-hidden transition-[width,opacity] duration-300 ease-out"
        style={{ width: sidebarOpen ? 280 : 0 }}
      >
        <div
          className={`h-full w-[280px] transition-[opacity,transform] duration-300 ease-out ${
            sidebarOpen ? 'translate-x-0 opacity-100' : '-translate-x-3 opacity-0'
          }`}
        >
          <DocSidebar
            nodes={raw.nodes}
            edges={raw.edges}
            onOpenNode={openNode}
            onOpenFullDoc={openFullDoc}
            activeTabId={activeId}
          />
        </div>
      </div>

      {/* chat */}
      <div className="w-[380px] shrink-0 p-[10px]">
        <ChatPanel
          messages={messages}
          health={health}
          onAsk={ask}
          onSearch={onSearch}
          onOpenNode={openNodeById}
          onAddWiki={addWiki}
          onViewAnswer={openAnswerTab}
          activeAnswerId={activeAnswerId}
          savedIds={savedIds}
          addingIds={addingIds}
          writeStatuses={answerWriteStatuses}
        />
      </div>

      {/* workspace */}
      <main className="relative m-[10px] ml-0 flex min-w-0 flex-1 flex-col overflow-hidden rounded-lg border border-line bg-white shadow-lg">
        <div className="flex h-[46px] items-stretch gap-[2px] overflow-x-auto border-b border-line bg-gradient-to-b from-white to-[#fbfdff] px-[8px]">
          {PINNED.map((p) => (
            <TabBtn key={p.id} active={activeId === p.id} dot={p.dot} onClick={() => setActiveId(p.id)}>
              {t.pinned[p.id]}
            </TabBtn>
          ))}
          <div className="my-[8px] w-px bg-line" />
          {tabs.map((t) => (
            <TabBtn
              key={t.id}
              active={activeId === t.id}
              dot={t.kind === 'answer' ? 'bg-green' : t.kind === 'fulldoc' ? 'bg-blue' : 'bg-orange'}
              onClick={() => setActiveId(t.id)}
              onClose={() => closeTab(t.id)}
            >
              {t.title.slice(0, 24)}
            </TabBtn>
          ))}
          <div className="ml-auto flex items-center pr-[6px]">
            <LangToggle />
          </div>
        </div>

        <div className="relative min-h-0 flex-1 overflow-hidden bg-white">
          {loading && <Centered>{t.loadingGraph}</Centered>}
          {error && !loading && (
            <Centered>
              <div className="max-w-[420px] text-center">
                <p className="font-bold text-red">{t.cannotReach}</p>
                <p className="mt-2 text-[13px] text-muted">{error}</p>
              </div>
            </Centered>
          )}

          {!loading && !error && (
            <ErrorBoundary resetKey={activeId}>
              {activeId === 'home' && <Home hasDocs={raw.nodes.length > 0} onShowDocs={() => setSidebarOpen(true)} />}

              <div className={`h-full ${activeId === 'graph' ? 'block' : 'hidden'}`}>
                <GraphCanvas
                  nodes={graph.nodes}
                  edges={graph.edges}
                  clusters={graph.clusters}
                  worldW={graph.worldW}
                  worldH={graph.worldH}
                  openIds={openIds}
                  answerIds={answerIds}
                  showConflict={false}
                  showStale={false}
                  onOpenNode={openNode}
                />
              </div>

              {activeId === 'upload' && (
                <UploadView pdfApiBase={pdfApiBase} onOpenDraft={openDraft} onUploadMarkdown={uploadMarkdown} />
              )}
              {activeId === 'settings' && <SettingsView overrides={overrides} onApply={applyOverrides} />}

              {activeTab && (activeTab.kind === 'doc' || activeTab.kind === 'draft' || activeTab.kind === 'fulldoc' || activeTab.kind === 'answer') && (
                <MarkdownView
                  doc={activeTab.doc}
                  draft={activeTab.draft}
                  isEditing={!!activeTab.editing}
                  dirty={activeTab.kind !== 'fulldoc' && (activeTab.draft.markdown !== activeTab.doc.markdown || activeTab.draft.title !== activeTab.doc.title)}
                  mode={activeTab.kind}
                  positions={activeTab.positions}
                  busy={!!activeTab.busy}
                  busyMessage={activeTab.busyMessage}
                  refs={activeTab.refs}
                  onStartEdit={() => startEdit(activeTab.id)}
                  onConfirm={() => saveNode(activeTab)}
                  onDelete={() => deleteNode(activeTab)}
                  onAddToGraph={() => addDraftToGraph(activeTab)}
                  onOpenNode={openNodeById}
                  onChangeTitle={(v) => changeTitle(activeTab.id, v)}
                  onChangeBody={(v) => changeBody(activeTab.id, v)}
                />
              )}
            </ErrorBoundary>
          )}

          {toast && (
            <div className="absolute bottom-[22px] right-[22px] z-20 max-w-[390px] rounded-lg border border-green/25 bg-[#f0fdf8] px-[14px] py-[13px] text-[13px] leading-[1.45] text-[#065f46] shadow-xl motion-safe:animate-[toastIn_.25s_ease]">
              {toast}
            </div>
          )}

          {assimPending > 0 && (
            <div
              title={t.assimTitle}
              className="absolute bottom-[22px] left-[22px] z-20 flex items-center gap-2 rounded-full border border-blue/25 bg-[#eff6ff] px-[12px] py-[7px] text-[12px] font-medium text-[#1e40af] shadow-lg"
            >
              <span className="h-[7px] w-[7px] animate-pulse rounded-full bg-blue" />
              {t.assimBadge} {assimPending}
            </div>
          )}
        </div>
      </main>
    </div>
  )
}

function Home({ hasDocs, onShowDocs }) {
  const t = useT(STR)
  return (
    <div className="grid h-full place-items-center bg-gradient-to-b from-white to-[#fbfdff]">
      <div className="max-w-[440px] text-center">
        <h1 className="m-0 text-[22px] font-extrabold tracking-tight text-ink">{t.nothingOpen}</h1>
        <p className="mt-[10px] text-[14px] leading-[1.6] text-muted">
          {t.homeHint(hasDocs)}
        </p>
        <div className="mt-[16px] flex justify-center gap-[10px]">
          <button onClick={onShowDocs} className="border border-blue/25 bg-blue/10 px-[14px] py-[9px] text-[13px] font-bold text-[#244a9d]">
            {t.showDocuments}
          </button>
        </div>
      </div>
    </div>
  )
}

function Centered({ children }) {
  return <div className="grid h-full place-items-center text-[14px] text-muted">{children}</div>
}

function TabBtn({ children, active, onClick, dot, onClose }) {
  const t = useT(STR)
  return (
    <div
      className={`group my-[7px] flex items-center gap-[7px] whitespace-nowrap rounded-md border px-[12px] text-[13px] transition-all duration-150 ${
        active ? 'border-line bg-white text-ink shadow-sm' : 'border-transparent bg-transparent text-muted hover:bg-soft hover:text-ink'
      }`}
    >
      <button className="flex items-center gap-[7px]" onClick={onClick}>
        <span className={`h-[8px] w-[8px] rounded-full ${dot}`} />
        {children}
      </button>
      {onClose && (
        <button
          onClick={onClose}
          title={t.closeTab}
          className="grid h-[16px] w-[16px] place-items-center rounded text-[13px] text-muted2 opacity-60 hover:bg-soft hover:text-ink group-hover:opacity-100"
        >
          ×
        </button>
      )}
    </div>
  )
}
