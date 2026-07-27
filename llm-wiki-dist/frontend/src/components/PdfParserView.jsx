import { useEffect, useRef, useState } from 'react'
import { useT } from '../i18n.jsx'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const DONE_TTL_MS = 72 * 60 * 60 * 1000
const POLL_MS = 5000

const PDF_SETTING_FIELDS = [
  'chat_base_url',
  'chat_api_key',
  'chat_model',
]

const PDF_LLM_FALLBACKS = {
  chat_base_url: 'http://10.160.144.101:51029/v1',
  chat_api_key: 'sk-dummy',
  chat_model: 'gemma-4-31B',
}

const STR = {
  ja: {
    selectPdf: 'PDF ファイルを選択してください。',
    fillFields:
      '設定ページで base_url、api_key、model を入力してください。',
    uploading: 'アップロード中…',
    queued: 'キューに追加されました。',
    queuedWaiting: 'キューで待機中…',
    converting: 'PDF を Markdown に変換中…',
    statusOf: (s) => `状態: ${s}`,
    opened: 'Markdown を開きました。',
    desc:
      'PDF をアップロードして Markdown に変換します。LLM 接続設定は設定ページのチャットモデル設定を使用します。完了後、キュー一覧のボタンから Markdown を開けます。',
    pdfFile: 'PDF ファイル',
    choosePdf: 'PDF を選択',
    noFile: 'ファイル未選択',
    processing: '処理中…',
    convertBtn: 'PDF を Markdown に変換',
    submittedCheckQueue: '送信しました。キュー一覧を確認してください。',
    queueTitle: 'PDF 変換キュー',
    refreshQueue: '更新',
    refreshing: '更新中…',
    queueEmpty: 'キュー項目はありません。',
    activeAndWaiting: '処理中・待機中',
    recentCompleted: '最近完了した項目',
    keptFor72h: '完了・失敗した項目は 72 時間だけ表示されます。',
    openMarkdown: 'Markdown を開く',
    opening: '取得中…',
    deleteItem: '削除',
    deleting: '削除中…',
    task: 'タスク',
    filename: 'ファイル名',
    status: '状態',
    position: '順番',
    result: '結果',
    completed: '完了',
    failed: '失敗',
    queuedStatus: '待機中',
    processingStatus: '処理中',
    unknownFile: 'ファイル名不明',
    resultNotReady: '結果はまだ準備できていません。',
    deleteConfirm: 'この項目を一覧から削除しますか？',
    imageDescriptions: '画像説明を生成',
    imageDescriptionsHelp:
      '画像説明の生成は時間がかかるため、必要な場合だけ有効にしてください。',
    mermaidDiagrams: 'Mermaid 図を生成',
    mermaidDiagramsHelp:
      'Mermaid 図はフロー図をテキストとして残せますが、検証と修復のため追加処理が必要です。',
    imageProcessingSoon:
      '画像処理が利用できない場合があります。解析結果に画像説明が含まれないことがあります。',
    usingSettings:
      'LLM 接続設定は設定ページのチャットモデル設定を使用しています。',
    currentModel: (model) => `現在のモデル: ${model || '-'}`,
    footer:
      '画像対応モデルであれば画像説明付き Markdown が返ります。画像非対応の場合は、バックエンド側のフォールバック処理により画像を埋め込んだ Markdown が返ります。',
  },
  en: {
    selectPdf: 'Please select a PDF file.',
    fillFields:
      'Please configure base_url, api_key, and model in the Settings page.',
    uploading: 'Uploading…',
    queued: 'Added to the queue.',
    queuedWaiting: 'Waiting in the queue…',
    converting: 'Converting PDF to Markdown…',
    statusOf: (s) => `Status: ${s}`,
    opened: 'Opened the Markdown.',
    desc:
      'Upload a PDF to convert it to Markdown. The LLM connection is taken from the chat model settings on the Settings page. When it finishes, open it manually from the queue list.',
    pdfFile: 'PDF File',
    choosePdf: 'Choose PDF',
    noFile: 'No file selected',
    processing: 'Processing…',
    convertBtn: 'Convert PDF to Markdown',
    submittedCheckQueue: 'Submitted. Check the queue list.',
    queueTitle: 'PDF Conversion Queue',
    refreshQueue: 'Refresh',
    refreshing: 'Refreshing…',
    queueEmpty: 'No queue items.',
    activeAndWaiting: 'Active / Waiting',
    recentCompleted: 'Recently Finished',
    keptFor72h: 'Completed and failed items are shown for 72 hours.',
    openMarkdown: 'Open Markdown',
    opening: 'Fetching…',
    deleteItem: 'Delete',
    deleting: 'Deleting…',
    task: 'Task',
    filename: 'Filename',
    status: 'Status',
    position: 'Position',
    result: 'Result',
    completed: 'Completed',
    failed: 'Failed',
    queuedStatus: 'Queued',
    processingStatus: 'Processing',
    unknownFile: 'Unknown filename',
    resultNotReady: 'The result is not ready yet.',
    deleteConfirm: 'Delete this item from the list?',
    imageDescriptions: 'Generate image descriptions',
    imageDescriptionsHelp:
      'Image description generation takes a long time, so enable it only when needed.',
    mermaidDiagrams: 'Generate Mermaid diagrams',
    mermaidDiagramsHelp:
      'Mermaid diagrams can capture flow diagrams in text, but validation and repair add extra processing.',
    imageProcessingSoon:
      'Image processing may be unavailable. Parsed results may not include image descriptions.',
    usingSettings:
      'LLM connection settings are read from the chat model settings on the Settings page.',
    currentModel: (model) => `Current model: ${model || '-'}`,
    footer:
      'An image-capable model returns Markdown with image descriptions. Without image support, the backend falls back to Markdown with embedded images.',
  },
}

function nowMs() {
  return Date.now()
}

function parseTimeMs(value) {
  if (!value) return null

  if (typeof value === 'number') {
    return value > 1000000000000 ? value : value * 1000
  }

  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}

function clean(value) {
  return String(value ?? '').trim()
}

function apiUrl(apiBase = '', path = '') {
  const base = String(apiBase || '').replace(/\/+$/, '')
  const nextPath = String(path || '').startsWith('/')
    ? String(path || '')
    : `/${path || ''}`

  return `${base}${nextPath}`
}

function getSettingValue(source, field, altField) {
  if (!source) return undefined

  if (source[field] !== undefined) return source[field]
  if (altField && source[altField] !== undefined) return source[altField]

  return undefined
}

function readPdfLlmSettings(source = {}) {
  const baseUrl =
    getSettingValue(source, 'chat_base_url', 'baseUrl') ??
    import.meta.env.VITE_OPENAI_BASE_URL ??
    ''

  const apiKey =
    getSettingValue(source, 'chat_api_key', 'apiKey') ??
    import.meta.env.VITE_OPENAI_API_KEY ??
    ''

  const model =
    getSettingValue(source, 'chat_model', 'model') ??
    import.meta.env.VITE_MODEL ??
    ''

  return {
    baseUrl: clean(baseUrl) || PDF_LLM_FALLBACKS.chat_base_url,
    apiKey: clean(apiKey) || PDF_LLM_FALLBACKS.chat_api_key,
    model: clean(model) || PDF_LLM_FALLBACKS.chat_model,
  }
}

function isDoneStatus(status) {
  return status === 'completed' || status === 'failed'
}

function pruneQueueItems(items) {
  const cutoff = nowMs() - DONE_TTL_MS

  return items
    .filter((item) => {
      if (!item?.task_id) return false

      if (!isDoneStatus(item.status)) return true

      const doneAt = item.doneAt || item.finishedAt || item.updatedAt || item.lastSeenAt
      return !doneAt || doneAt >= cutoff
    })
    .sort(sortQueueItems)
}

function sortQueueItems(a, b) {
  const rank = {
    processing: 0,
    queued: 1,
    completed: 2,
    failed: 3,
  }

  const ar = rank[a.status] ?? 9
  const br = rank[b.status] ?? 9

  if (ar !== br) return ar - br

  if (a.status === 'queued' && b.status === 'queued') {
    return (a.position || 999999) - (b.position || 999999)
  }

  return (b.updatedAt || 0) - (a.updatedAt || 0)
}

function normalizeQueueItem(raw, fallback = {}) {
  if (!raw) return null

  const taskId = raw.task_id || raw.taskId || fallback.task_id

  if (!taskId) return null

  const status = raw.status || fallback.status || 'queued'
  const finishedAt = parseTimeMs(raw.finished_at || raw.finishedAt)
  const startedAt = parseTimeMs(raw.started_at || raw.startedAt)
  const createdAt = parseTimeMs(raw.created_at || raw.createdAt)

  const ts = nowMs()

  return {
    task_id: taskId,
    namespace: raw.namespace ?? fallback.namespace ?? null,
    filename: raw.filename || fallback.filename || raw.name || fallback.name || '',
    status,
    position: raw.position ?? raw.queue_position ?? fallback.position ?? null,
    queuedAhead: raw.queued_ahead ?? fallback.queuedAhead ?? null,
    result_url: raw.result_url || raw.resultUrl || fallback.result_url || null,
    error: raw.error || fallback.error || null,
    createdAt: createdAt || fallback.createdAt || ts,
    startedAt: startedAt || fallback.startedAt || null,
    finishedAt: finishedAt || fallback.finishedAt || null,
    doneAt:
      isDoneStatus(status)
        ? finishedAt || fallback.doneAt || ts
        : fallback.doneAt || null,
    updatedAt: ts,
    lastSeenAt: ts,
  }
}

function normalizeQueueResponse(data) {
  const items = []

  if (data?.processing) {
    const item = normalizeQueueItem(data.processing, {
      status: 'processing',
    })

    if (item) items.push(item)
  }

  for (const raw of data?.queued || []) {
    const item = normalizeQueueItem(raw, {
      status: 'queued',
    })

    if (item) items.push(item)
  }

  for (const raw of data?.completed || data?.completed_items || []) {
    const item = normalizeQueueItem(raw, {
      status: 'completed',
    })

    if (item) items.push(item)
  }

  for (const raw of data?.failed || data?.failed_items || []) {
    const item = normalizeQueueItem(raw, {
      status: 'failed',
    })

    if (item) items.push(item)
  }

  for (const raw of data?.items || data?.tasks || []) {
    const item = normalizeQueueItem(raw)

    if (item) items.push(item)
  }

  const deduped = new Map()

  for (const item of items) {
    deduped.set(item.task_id, {
      ...(deduped.get(item.task_id) || {}),
      ...item,
    })
  }

  return pruneQueueItems([...deduped.values()])
}

function mergeInMemoryItems(oldItems, newItems) {
  const map = new Map()

  for (const oldItem of oldItems) {
    if (oldItem?.task_id) {
      map.set(oldItem.task_id, oldItem)
    }
  }

  for (const newItem of newItems) {
    if (!newItem?.task_id) continue

    const oldItem = map.get(newItem.task_id)

    map.set(newItem.task_id, {
      ...oldItem,
      ...newItem,
      filename: newItem.filename || oldItem?.filename || '',
      error: newItem.error || oldItem?.error || null,
      result_url: newItem.result_url || oldItem?.result_url || null,
      doneAt:
        isDoneStatus(newItem.status)
          ? newItem.doneAt || oldItem?.doneAt || nowMs()
          : oldItem?.doneAt || null,
    })
  }

  return pruneQueueItems([...map.values()])
}

function statusLabel(status, t) {
  if (status === 'queued') return t.queuedStatus
  if (status === 'processing') return t.processingStatus
  if (status === 'completed') return t.completed
  if (status === 'failed') return t.failed
  return status || '-'
}

export default function PdfParserView({
  apiBase = '',
  onMarkdownReady = () => {},
  settings,
  overrides,
}) {
  const t = useT(STR)
  const fileRef = useRef(null)
  const queueRef = useRef([])

  const settingsSource = {
    ...PDF_LLM_FALLBACKS,
    ...(settings || {}),
    ...(overrides || {}),
  }

  const [file, setFile] = useState(null)

  const [llmSettings, setLlmSettings] = useState(() =>
    readPdfLlmSettings(settingsSource)
  )

  const [generateImageDescriptions, setGenerateImageDescriptions] = useState(false)
  const [generateMermaidDiagrams, setGenerateMermaidDiagrams] = useState(false)

  const [busy, setBusy] = useState(false)
  const [queueBusy, setQueueBusy] = useState(false)
  const [openingTaskId, setOpeningTaskId] = useState(null)
  const [deletingTaskId, setDeletingTaskId] = useState(null)

  const [status, setStatus] = useState('')
  const [taskId, setTaskId] = useState(null)
  const [error, setError] = useState(null)

  // Live-only queue state.
  // No localStorage.
  // No sessionStorage.
  // No cookies.
  const [queueItems, setQueueItems] = useState([])

  useEffect(() => {
    queueRef.current = queueItems
  }, [queueItems])

  useEffect(() => {
    if (!generateImageDescriptions) {
      setGenerateMermaidDiagrams(false)
    }
  }, [generateImageDescriptions])

  useEffect(() => {
    setLlmSettings(readPdfLlmSettings(settingsSource))
  }, [settings, overrides])

  useEffect(() => {
    const syncSettings = () => {
      setLlmSettings(readPdfLlmSettings(settingsSource))
    }

    window.addEventListener('focus', syncSettings)
    document.addEventListener('visibilitychange', syncSettings)

    return () => {
      window.removeEventListener('focus', syncSettings)
      document.removeEventListener('visibilitychange', syncSettings)
    }
  }, [settings, overrides])

  const refreshQueue = async ({ silent = true } = {}) => {
    if (!silent) setQueueBusy(true)

    try {
      const queueRes = await fetch(apiUrl(apiBase, '/queue'), {
        method: 'GET',
        cache: 'no-store',
      })

      if (!queueRes.ok) {
        const text = await queueRes.text()
        throw new Error(text || `Queue fetch failed: ${queueRes.status}`)
      }

      const queueData = await queueRes.json()
      const liveItems = normalizeQueueResponse(queueData)

      // Important:
      // Replace with live server state.
      // Do not merge with old browser-stored data.
      setQueueItems(liveItems)
    } catch (e) {
      if (!silent) {
        setError(e.message || String(e))
      }
    } finally {
      if (!silent) setQueueBusy(false)
    }
  }

  useEffect(() => {
    let cancelled = false

    const tick = async () => {
      if (cancelled) return
      await refreshQueue({ silent: true })
    }

    // Clear previous apiBase view immediately, then fetch live server queue.
    setQueueItems([])
    queueRef.current = []

    tick()

    const timer = setInterval(tick, POLL_MS)

    return () => {
      cancelled = true
      clearInterval(timer)
    }
    // apiBase intentionally controls the polling target.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBase])

  const pickFile = (e) => {
    const next = e.target.files?.[0] || null
    setFile(next)
    setError(null)
    setStatus('')
    setTaskId(null)
  }

  const upload = async () => {
    if (!file) {
      setError(t.selectPdf)
      return
    }

    const activeSettings = readPdfLlmSettings(settingsSource)
    setLlmSettings(activeSettings)

    if (!activeSettings.baseUrl || !activeSettings.apiKey || !activeSettings.model) {
      setError(t.fillFields)
      return
    }

    setBusy(true)
    setError(null)
    setStatus(t.uploading)
    setTaskId(null)

    try {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('base_url', activeSettings.baseUrl)
      fd.append('api_key', activeSettings.apiKey)
      fd.append('model', activeSettings.model)
      fd.append('describe_images', generateImageDescriptions ? 'true' : 'false')
      fd.append(
        'generate_mermaid',
        generateImageDescriptions && generateMermaidDiagrams ? 'true' : 'false'
      )

      const uploadRes = await fetch(apiUrl(apiBase, '/upload'), {
        method: 'POST',
        body: fd,
      })

      if (!uploadRes.ok) {
        const text = await uploadRes.text()
        throw new Error(text || `Upload failed: ${uploadRes.status}`)
      }

      const uploaded = await uploadRes.json()
      const id = uploaded.task_id

      if (!id) {
        throw new Error('Backend did not return task_id.')
      }

      setTaskId(id)

      const item = normalizeQueueItem(uploaded, {
        task_id: id,
        filename: file.name,
        status: uploaded.status || 'queued',
      })

      if (item) {
        // In-memory only, so the user sees the submitted task immediately.
        // The next /queue poll replaces this with server truth.
        setQueueItems((prev) => mergeInMemoryItems(prev, [item]))
      }

      if (uploaded.status === 'queued') {
        setStatus(t.queuedWaiting)
      } else if (uploaded.status === 'processing') {
        setStatus(t.converting)
      } else if (uploaded.status === 'completed') {
        setStatus(t.submittedCheckQueue)
      } else {
        setStatus(t.submittedCheckQueue)
      }

      await sleep(300)
      await refreshQueue({ silent: true })
    } catch (e) {
      setError(e.message || String(e))
      setStatus('')
    } finally {
      setBusy(false)
    }
  }

  const openMarkdown = async (item) => {
    if (item.status !== 'completed') {
      setError(t.resultNotReady)
      return
    }

    setOpeningTaskId(item.task_id)
    setError(null)

    try {
      const resultRes = await fetch(
        apiUrl(apiBase, `/result/${encodeURIComponent(item.task_id)}`),
        {
          method: 'GET',
          cache: 'no-store',
        }
      )

      if (!resultRes.ok) {
        const text = await resultRes.text()
        throw new Error(text || `Result fetch failed: ${resultRes.status}`)
      }

      const markdown = await resultRes.text()
      const title = item.filename || item.task_id

      setStatus(t.opened)

      onMarkdownReady({
        taskId: item.task_id,
        title,
        filename: item.filename || `${item.task_id}.md`,
        markdown,
        sourceType: 'endogenous',
        sourcePath: `pdf-parser:${item.task_id}:${title}`,
      })
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setOpeningTaskId(null)
    }
  }

  const deleteQueueItem = async (item) => {
    const ok = window.confirm(t.deleteConfirm)

    if (!ok) return

    setDeletingTaskId(item.task_id)
    setError(null)

    try {
      const previousItems = queueRef.current

      // Optimistic in-memory removal only.
      // Not persisted anywhere.
      setQueueItems((prev) => prev.filter((x) => x.task_id !== item.task_id))

      const deleteRes = await fetch(
        apiUrl(apiBase, `/queue/${encodeURIComponent(item.task_id)}`),
        {
          method: 'DELETE',
        }
      )

      if (!deleteRes.ok && deleteRes.status !== 404) {
        const text = await deleteRes.text()
        throw new Error(text || `Delete failed: ${deleteRes.status}`)
      }

      await refreshQueue({ silent: true })
    } catch (e) {
      setError(e.message || String(e))

      // Restore from in-memory snapshot if delete failed.
      setQueueItems(queueRef.current.length ? queueRef.current : previousItems)
    } finally {
      setDeletingTaskId(null)
    }
  }

  const activeItems = queueItems.filter(
    (item) => item.status === 'processing' || item.status === 'queued'
  )

  const recentDoneItems = queueItems.filter(
    (item) => item.status === 'completed' || item.status === 'failed'
  )

  return (
    <div className="h-full overflow-auto bg-white">
      <div className="mx-auto max-w-[960px] px-[28px] py-[26px]">
        <div className="mb-[18px] border border-line bg-gradient-to-b from-white to-[#fbfdff] p-[18px] shadow-sm">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h1 className="m-0 text-[20px] font-extrabold tracking-tight text-ink">
                PDFをアップロード
              </h1>

              <p className="mt-[7px] max-w-[620px] text-[13px] leading-[1.55] text-muted">
                {t.desc}
              </p>
            </div>

            {taskId && (
              <div className="border border-line bg-soft px-[10px] py-[8px] text-right text-[11px] text-muted">
                <div className="font-bold uppercase tracking-wider">Task</div>
                <div className="mt-[3px] max-w-[210px] truncate font-mono">{taskId}</div>
              </div>
            )}
          </div>
        </div>

        <div className="border border-line bg-white p-[18px] shadow-sm">
          <div className="grid gap-[14px]">
            <div>
              <label className="mb-[6px] block text-[12px] font-extrabold uppercase tracking-wider text-muted">
                {t.pdfFile}
              </label>

              <div className="flex items-center gap-[10px]">
                <input
                  ref={fileRef}
                  type="file"
                  accept="application/pdf,.pdf"
                  onChange={pickFile}
                  className="hidden"
                />

                <button
                  type="button"
                  disabled={busy}
                  onClick={() => fileRef.current?.click()}
                  className="border border-line bg-white px-[13px] py-[9px] text-[13px] font-bold text-slate-700 hover:border-line2 disabled:opacity-50"
                >
                  {t.choosePdf}
                </button>

                <div className="min-w-0 flex-1 truncate border border-line bg-soft px-[12px] py-[9px] text-[13px] text-muted">
                  {file ? file.name : t.noFile}
                </div>
              </div>
            </div>

            <div className="border border-line bg-soft px-[12px] py-[10px] text-[12px] leading-[1.5] text-muted">
              <div>{t.usingSettings}</div>
              <div className="mt-[3px] font-mono">
                {t.currentModel(llmSettings.model)}
              </div>
            </div>

            <div className="grid gap-[8px]">
              <ToggleField
                label={t.imageDescriptions}
                checked={generateImageDescriptions}
                onChange={setGenerateImageDescriptions}
                disabled={busy}
              />

              <p className="m-0 text-[12px] leading-[1.45] text-muted">
                {t.imageDescriptionsHelp}
              </p>

              {generateImageDescriptions && (
                <div className="grid gap-[8px]">
                  <ToggleField
                    label={t.mermaidDiagrams}
                    checked={generateMermaidDiagrams}
                    onChange={setGenerateMermaidDiagrams}
                    disabled={busy}
                  />

                  <p className="m-0 text-[12px] leading-[1.45] text-muted">
                    {t.mermaidDiagramsHelp}
                  </p>

                  <p className="m-0 border border-[#facc15]/45 bg-[#fef9c3] px-[10px] py-[7px] text-[12px] leading-[1.45] text-[#854d0e]">
                    {t.imageProcessingSoon}
                  </p>
                </div>
              )}
            </div>

            <div className="mt-[4px] flex flex-wrap items-center gap-[10px]">
              <button
                type="button"
                disabled={busy || !file}
                onClick={upload}
                className="border border-blue/30 bg-blue px-[16px] py-[10px] text-[13px] font-extrabold text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy ? t.processing : t.convertBtn}
              </button>

              {status && (
                <span className="border border-line bg-soft px-[10px] py-[8px] text-[12px] text-muted">
                  {status}
                </span>
              )}
            </div>

            {error && (
              <div className="border border-red/25 bg-red/10 px-[12px] py-[10px] text-[13px] leading-[1.45] text-[#7c1230]">
                {error}
              </div>
            )}
          </div>
        </div>

        <div className="mt-[16px] border border-line bg-white p-[18px] shadow-sm">
          <div className="mb-[12px] flex flex-wrap items-start justify-between gap-[10px]">
            <div>
              <h2 className="m-0 text-[16px] font-extrabold text-ink">
                {t.queueTitle}
              </h2>

              <p className="mt-[4px] text-[12px] text-muted">
                {t.keptFor72h}
              </p>
            </div>

            <button
              type="button"
              disabled={queueBusy}
              onClick={() => refreshQueue({ silent: false })}
              className="border border-line bg-white px-[12px] py-[8px] text-[12px] font-bold text-slate-700 hover:border-line2 disabled:opacity-50"
            >
              {queueBusy ? t.refreshing : t.refreshQueue}
            </button>
          </div>

          {queueItems.length === 0 ? (
            <div className="border border-line bg-soft px-[12px] py-[10px] text-[13px] text-muted">
              {t.queueEmpty}
            </div>
          ) : (
            <div className="grid gap-[14px]">
              <QueueGroup
                title={t.activeAndWaiting}
                items={activeItems}
                t={t}
                openingTaskId={openingTaskId}
                deletingTaskId={deletingTaskId}
                onOpen={openMarkdown}
                onDelete={deleteQueueItem}
              />

              <QueueGroup
                title={t.recentCompleted}
                items={recentDoneItems}
                t={t}
                openingTaskId={openingTaskId}
                deletingTaskId={deletingTaskId}
                onOpen={openMarkdown}
                onDelete={deleteQueueItem}
              />
            </div>
          )}
        </div>

        <div className="mt-[14px] border border-line bg-soft px-[13px] py-[11px] text-[12px] leading-[1.5] text-muted">
          {t.footer}
        </div>
      </div>
    </div>
  )
}

function QueueGroup({
  title,
  items,
  t,
  openingTaskId,
  deletingTaskId,
  onOpen,
  onDelete,
}) {
  if (!items.length) return null

  return (
    <div>
      <div className="mb-[7px] text-[12px] font-extrabold uppercase tracking-wider text-muted">
        {title}
      </div>

      <div className="grid gap-[8px]">
        {items.map((item) => (
          <QueueItem
            key={item.task_id}
            item={item}
            t={t}
            opening={openingTaskId === item.task_id}
            deleting={deletingTaskId === item.task_id}
            onOpen={() => onOpen(item)}
            onDelete={() => onDelete(item)}
          />
        ))}
      </div>
    </div>
  )
}

function QueueItem({
  item,
  t,
  opening,
  deleting,
  onOpen,
  onDelete,
}) {
  const canOpen = item.status === 'completed'

  return (
    <div className="border border-line bg-soft p-[11px]">
      <div className="flex flex-wrap items-start justify-between gap-[10px]">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-[8px]">
            <span
              className={[
                'border px-[8px] py-[4px] text-[11px] font-extrabold uppercase tracking-wider',
                item.status === 'completed'
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                  : item.status === 'failed'
                    ? 'border-red/25 bg-red/10 text-[#7c1230]'
                    : item.status === 'processing'
                      ? 'border-blue/25 bg-blue/10 text-blue'
                      : 'border-line bg-white text-muted',
              ].join(' ')}
            >
              {statusLabel(item.status, t)}
            </span>

            {item.position && (
              <span className="text-[11px] text-muted">
                {t.position}: {item.position}
              </span>
            )}

            {item.namespace && (
              <span className="text-[11px] text-muted">
                DB: {item.namespace}
              </span>
            )}
          </div>

          <div className="mt-[7px] truncate text-[13px] font-bold text-ink">
            {item.filename || t.unknownFile}
          </div>

          <div className="mt-[4px] truncate font-mono text-[11px] text-muted">
            {t.task}: {item.task_id}
          </div>

          {item.error && (
            <div className="mt-[6px] text-[12px] leading-[1.45] text-[#7c1230]">
              {item.error}
            </div>
          )}
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-[8px]">
          <button
            type="button"
            disabled={!canOpen || opening || deleting}
            onClick={onOpen}
            className="border border-blue/30 bg-blue px-[11px] py-[8px] text-[12px] font-extrabold text-white disabled:cursor-not-allowed disabled:opacity-50"
          >
            {opening ? t.opening : t.openMarkdown}
          </button>

          <button
            type="button"
            disabled={opening || deleting}
            onClick={onDelete}
            className="border border-line bg-white px-[11px] py-[8px] text-[12px] font-bold text-slate-700 hover:border-line2 disabled:opacity-50"
          >
            {deleting ? t.deleting : t.deleteItem}
          </button>
        </div>
      </div>
    </div>
  )
}

function ToggleField({
  label,
  checked,
  onChange,
  disabled,
}) {
  return (
    <label className="flex items-center gap-[9px] text-[13px] font-bold text-slate-700">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="h-[15px] w-[15px] accent-blue disabled:opacity-60"
      />

      <span>{label}</span>
    </label>
  )
}
