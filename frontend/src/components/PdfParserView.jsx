import { useEffect, useRef, useState } from 'react'
import { useT } from '../i18n.jsx'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const QUEUE_STORAGE_KEY = 'pdf-parser-queue-v1'
const DONE_TTL_MS = 72 * 60 * 60 * 1000
const POLL_MS = 2500
const MAX_STATUS_CHECKS_PER_POLL = 10

const STR = {
  ja: {
    selectPdf: 'PDF ファイルを選択してください。',
    fillFields: 'base_url、api_key、model を入力してください。',
    uploading: 'アップロード中…',
    queued: 'キューに追加されました。',
    queuedWaiting: 'キューで待機中…',
    converting: 'PDF を Markdown に変換中…',
    statusOf: (s) => `状態: ${s}`,
    opened: 'Markdown を開きました。',
    desc: 'PDF をアップロードして Markdown に変換します。完了後、キュー一覧のボタンから Markdown を開けます。',
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
    footer:
      '画像対応モデルであれば画像説明付き Markdown が返ります。画像非対応の場合は、バックエンド側のフォールバック処理により画像を埋め込んだ Markdown が返ります。',
  },
  en: {
    selectPdf: 'Please select a PDF file.',
    fillFields: 'Please fill in base_url, api_key, and model.',
    uploading: 'Uploading…',
    queued: 'Added to the queue.',
    queuedWaiting: 'Waiting in the queue…',
    converting: 'Converting PDF to Markdown…',
    statusOf: (s) => `Status: ${s}`,
    opened: 'Opened the Markdown.',
    desc: 'Upload a PDF to convert it to Markdown. When it finishes, open it manually from the queue list.',
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

function loadStoredQueue() {
  if (typeof localStorage === 'undefined') return []

  try {
    const raw = localStorage.getItem(QUEUE_STORAGE_KEY)
    if (!raw) return []

    const items = JSON.parse(raw)
    return Array.isArray(items) ? pruneQueueItems(items) : []
  } catch {
    return []
  }
}

function saveStoredQueue(items) {
  if (typeof localStorage === 'undefined') return

  try {
    localStorage.setItem(QUEUE_STORAGE_KEY, JSON.stringify(pruneQueueItems(items)))
  } catch {
    // ignore localStorage errors
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

  // Future backend compatibility.
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

  return items
}

function mergeQueueItems(oldItems, newItems) {
  const map = new Map()

  for (const oldItem of pruneQueueItems(oldItems)) {
    map.set(oldItem.task_id, oldItem)
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
  onMarkdownReady,
}) {
  const t = useT(STR)
  const fileRef = useRef(null)
  const queueRef = useRef([])

  const [file, setFile] = useState(null)
  const [baseUrl, setBaseUrl] = useState(import.meta.env.VITE_OPENAI_BASE_URL || 'http://localhost:8080/v1')
  const [apiKey, setApiKey] = useState(import.meta.env.VITE_OPENAI_API_KEY || '')
  const [model, setModel] = useState(import.meta.env.VITE_MODEL || 'openai/gpt-oss-120b')

  const [busy, setBusy] = useState(false)
  const [queueBusy, setQueueBusy] = useState(false)
  const [openingTaskId, setOpeningTaskId] = useState(null)
  const [deletingTaskId, setDeletingTaskId] = useState(null)

  const [status, setStatus] = useState('')
  const [taskId, setTaskId] = useState(null)
  const [error, setError] = useState(null)
  const [queueItems, setQueueItems] = useState(() => loadStoredQueue())

  useEffect(() => {
    queueRef.current = queueItems
    saveStoredQueue(queueItems)
  }, [queueItems])

  const upsertQueueItems = (items) => {
    setQueueItems((prev) => mergeQueueItems(prev, items))
  }

  const refreshQueue = async ({ silent = true } = {}) => {
    if (!silent) setQueueBusy(true)

    try {
      const incoming = []

      const queueRes = await fetch(`${apiBase}/queue`)

      if (queueRes.ok) {
        const queueData = await queueRes.json()
        incoming.push(...normalizeQueueResponse(queueData))
      }

      const pending = queueRef.current
        .filter((item) => item.status === 'queued' || item.status === 'processing')
        .slice(0, MAX_STATUS_CHECKS_PER_POLL)

      const statusItems = await Promise.all(
        pending.map(async (item) => {
          try {
            const res = await fetch(`${apiBase}/status/${encodeURIComponent(item.task_id)}`)

            if (!res.ok) return null

            const data = await res.json()

            return normalizeQueueItem(data, {
              task_id: item.task_id,
              filename: item.filename,
              status: item.status,
            })
          } catch {
            return null
          }
        })
      )

      incoming.push(...statusItems.filter(Boolean))

      if (incoming.length > 0) {
        upsertQueueItems(incoming)
      } else {
        setQueueItems((prev) => pruneQueueItems(prev))
      }
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

    if (!baseUrl.trim() || !apiKey.trim() || !model.trim()) {
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
      fd.append('base_url', baseUrl.trim())
      fd.append('api_key', apiKey.trim())
      fd.append('model', model.trim())

      const uploadRes = await fetch(`${apiBase}/upload`, {
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
        upsertQueueItems([item])
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
      const resultRes = await fetch(`${apiBase}/result/${encodeURIComponent(item.task_id)}`)

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
      setQueueItems((prev) => prev.filter((x) => x.task_id !== item.task_id))

      // Future backend endpoint. Safe to ignore until implemented.
      try {
        await fetch(`${apiBase}/queue/${encodeURIComponent(item.task_id)}`, {
          method: 'DELETE',
        })
      } catch {
        // frontend delete is enough for now
      }
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
                PDF Parser
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

            <div className="grid grid-cols-1 gap-[12px] md:grid-cols-2">
              <Field
                label="Base URL"
                value={baseUrl}
                onChange={setBaseUrl}
                placeholder="http://localhost:8080/v1"
                disabled={busy}
              />

              <Field
                label="Model"
                value={model}
                onChange={setModel}
                placeholder="openai/gpt-oss-120b"
                disabled={busy}
              />
            </div>

            <Field
              label="API Key"
              value={apiKey}
              onChange={setApiKey}
              placeholder="nvapi-..."
              disabled={busy}
              password
            />

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

function Field({
  label,
  value,
  onChange,
  placeholder,
  disabled,
  password = false,
}) {
  return (
    <label className="block">
      <span className="mb-[6px] block text-[12px] font-extrabold uppercase tracking-wider text-muted">
        {label}
      </span>

      <input
        type={password ? 'password' : 'text'}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="h-[38px] w-full border border-line bg-white px-[11px] text-[13px] text-ink outline-none placeholder:text-muted/60 focus:border-blue/50 disabled:opacity-60"
      />
    </label>
  )
}
