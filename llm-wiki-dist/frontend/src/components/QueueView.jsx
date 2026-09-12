import { useEffect, useMemo, useRef, useState } from 'react'
import {
  CheckCircle2,
  CircleDashed,
  Clock3,
  ListTodo,
  Loader2,
  RefreshCw,
  XCircle,
} from 'lucide-react'
import { useT } from '../i18n.jsx'

// Self-contained librarian write-queue monitor. Deliberately has no imports
// from api.js or hooks so it can be dropped into another checkout with only
// a sidebar entry + a route line (it talks to the backend directly).
const API_BASE = import.meta.env.VITE_API_URL ?? window.location.pathname.replace(/\/$/, '')
const POLL_MS = 2500

// Stage labels are keyed on what the pipelines actually report: the Japanese
// `stage` string, with the ASCII `step` appended after a colon when the run is
// granular enough to report one ("ページ分割:routing"). An unknown key falls
// back to the bare stage and then to the raw string, so a step added on the
// backend still shows up as something readable.
const STR = {
  ja: {
    title: 'ジョブキュー',
    hint: 'ライブラリアンの書き込みキュー。ジョブは1件ずつ順番に実行されます。',
    empty: 'ジョブはまだありません。',
    loadError: 'キューを取得できません：',
    refresh: '更新',
    cancel: 'キャンセル',
    assimilating: (n) => `バックグラウンド整理中: ${n}`,
    queuedPos: (p) => `待機中（${p} 番目）`,
    statuses: {
      queued: '待機中',
      running: '実行中',
      done: '完了',
      failed: '失敗',
      cancelled: 'キャンセル済み',
    },
    stages: {
      'チャンク分割': 'チャンク分割',
      'チャンク分割:planning': 'チャンク分割: 分割計画を作成中',
      'チャンク分割:metadata': 'チャンク分割: 見出し・メタデータを整理中',
      'チャンク分割:done': 'チャンク分割: 完了、取り込みへ',
      'ページ分割': 'ページ組み立て',
      'ページ分割:chunking': 'ページ組み立て: チャンクを切り出し中',
      'ページ分割:summarizing': 'ページ組み立て: チャンクを要約中',
      'ページ分割:shelf': 'ページ組み立て: 章立てを設計中',
      'ページ分割:routing': 'ページ組み立て: チャンクをページに割り当て中',
      'ページ分割:stitching': 'ページ組み立て: 章のつながりを編集中',
      'ページ分割:writing': 'ページ組み立て: ページを書き出し中',
      convert: 'raw を変換中',
      sync: '変更を同期中',
      write: 'Wiki を書き出し中',
      ingesting: 'グラフへ取り込み中',
      enriching: '要約・重複チェック中',
      done: '完了処理中',
    },
    types: {
      chunk_and_ingest: '大型ドキュメント（チャンク＋取り込み）',
      sync_project: 'raw/ から Wiki を同期',
      create_document: 'ドキュメント追加',
      create_exogenous: 'ノート追加',
      update_node: 'ノード更新',
      delete_node: 'ノード削除',
      delete_document: 'ドキュメント削除',
      ingest_md_output: 'フォルダ取り込み',
      cascading_update: 'カスケード更新',
      recluster: '再クラスタリング',
      ensure_japanese_clusters: 'クラスタ名の整理',
    },
  },
  en: {
    title: 'Job queue',
    hint: "The librarian's write queue. Jobs run strictly one at a time.",
    empty: 'No jobs yet.',
    loadError: 'Cannot load the queue: ',
    refresh: 'Refresh',
    cancel: 'Cancel',
    assimilating: (n) => `Background assimilation: ${n}`,
    queuedPos: (p) => `Queued (position ${p})`,
    statuses: {
      queued: 'Queued',
      running: 'Running',
      done: 'Done',
      failed: 'Failed',
      cancelled: 'Cancelled',
    },
    stages: {
      'チャンク分割': 'Chunking',
      'チャンク分割:planning': 'Chunking: planning the split',
      'チャンク分割:metadata': 'Chunking: tidying headings and metadata',
      'チャンク分割:done': 'Chunking: done, ingesting',
      'ページ分割': 'Page assembly',
      'ページ分割:chunking': 'Page assembly: cutting chunks',
      'ページ分割:summarizing': 'Page assembly: summarizing chunks',
      'ページ分割:shelf': 'Page assembly: designing the shelf',
      'ページ分割:routing': 'Page assembly: routing chunks to pages',
      'ページ分割:stitching': 'Page assembly: stitching the page together',
      'ページ分割:writing': 'Page assembly: writing pages',
      convert: 'Converting raw files',
      sync: 'Syncing changes',
      write: 'Writing wiki pages',
      ingesting: 'Ingesting into the graph',
      enriching: 'Summaries + dedup',
      done: 'Finishing',
    },
    types: {
      chunk_and_ingest: 'Large document (chunk + ingest)',
      sync_project: 'Sync Wiki from raw/',
      create_document: 'Add document',
      create_exogenous: 'Add note',
      update_node: 'Update node',
      delete_node: 'Delete node',
      delete_document: 'Delete document',
      ingest_md_output: 'Ingest folder',
      cascading_update: 'Cascading update',
      recluster: 'Recluster',
      ensure_japanese_clusters: 'Tidy cluster names',
    },
  },
}

const STATUS_STYLE = {
  queued: 'bg-amber-50 text-amber-700 border-amber-200',
  running: 'bg-blue-50 text-blue-700 border-blue-200',
  done: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  failed: 'bg-red-50 text-red-700 border-red-200',
  cancelled: 'bg-slate-100 text-slate-500 border-slate-200',
}

function StatusIcon({ status }) {
  if (status === 'running') return <Loader2 size={14} className="animate-spin" />
  if (status === 'queued') return <Clock3 size={14} />
  if (status === 'done') return <CheckCircle2 size={14} />
  if (status === 'failed') return <XCircle size={14} />
  return <CircleDashed size={14} />
}

function jobLabel(job, t) {
  const payload = job.payload || {}
  return (
    payload.document_name ||
    payload.title ||
    payload.node_id ||
    payload.path ||
    payload.source_file ||
    (typeof payload.body === 'string' ? payload.body.slice(0, 60) : '') ||
    job.id.slice(0, 8)
  )
}

function progressText(job, t) {
  const p = job.progress
  if (!p?.stage) return null
  const key = p.step ? `${p.stage}:${p.step}` : p.stage
  const stage = t.stages[key] || t.stages[p.stage] || p.stage
  const counter =
    p.current != null && p.total != null ? ` ${p.current}/${p.total}` : ''
  const tallies = [
    p.files != null ? p.files : p.file_count,
    p.lines != null ? p.lines : p.source_line_count,
    p.chunk_count,
  ].filter((value) => value != null)
  const extra = tallies.length ? ` (${tallies.join(' / ')})` : ''
  return `${stage}${counter}${extra}`
}

function fmtTime(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleTimeString()
  } catch {
    return iso
  }
}

function duration(job) {
  if (!job.started_at) return null
  const end = job.finished_at ? new Date(job.finished_at) : new Date()
  const seconds = Math.max(0, Math.round((end - new Date(job.started_at)) / 1000))
  if (seconds < 90) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m${seconds % 60}s`
}

export default function QueueView() {
  const t = useT(STR)
  const [jobs, setJobs] = useState([])
  const [pending, setPending] = useState(0)
  const [error, setError] = useState(null)
  const [tick, setTick] = useState(0)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true

    const load = async () => {
      try {
        const [jobsRes, assimRes] = await Promise.all([
          fetch(`${API_BASE}/api/write-jobs?limit=100`),
          fetch(`${API_BASE}/api/assimilation`),
        ])
        if (!jobsRes.ok) throw new Error(`${jobsRes.status}`)
        const jobsData = await jobsRes.json()
        const assimData = assimRes.ok ? await assimRes.json() : { pending: 0 }
        if (!alive.current) return
        setJobs(Array.isArray(jobsData) ? jobsData : [])
        setPending(assimData.pending || 0)
        setError(null)
      } catch (e) {
        if (alive.current) setError(e.message || String(e))
      }
    }

    load()
    const timer = setInterval(load, POLL_MS)
    return () => {
      alive.current = false
      clearInterval(timer)
    }
  }, [tick])

  const counts = useMemo(() => {
    const byStatus = { queued: 0, running: 0, done: 0, failed: 0, cancelled: 0 }
    for (const job of jobs) {
      if (byStatus[job.status] != null) byStatus[job.status] += 1
    }
    return byStatus
  }, [jobs])

  const cancelJob = async (id) => {
    try {
      await fetch(`${API_BASE}/api/write-jobs/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      })
      setTick((v) => v + 1)
    } catch {
      /* next poll will show reality */
    }
  }

  return (
    <div className="h-full overflow-auto bg-white">
      <div className="mx-auto max-w-[860px] px-[28px] py-[26px]">
        <div className="mb-[18px] flex items-start justify-between gap-4">
          <div>
            <h1 className="m-0 flex items-center gap-2 text-[20px] font-extrabold tracking-tight text-slate-950">
              <ListTodo size={20} className="text-blue-600" />
              {t.title}
            </h1>
            <p className="mb-0 mt-[4px] text-[12.5px] text-slate-500">{t.hint}</p>
          </div>
          <button
            type="button"
            onClick={() => setTick((v) => v + 1)}
            className="flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-1.5 text-[12px] font-semibold text-slate-600 hover:bg-slate-50"
          >
            <RefreshCw size={13} />
            {t.refresh}
          </button>
        </div>

        <div className="mb-[16px] flex flex-wrap items-center gap-2">
          {Object.entries(counts).map(([status, count]) => (
            <span
              key={status}
              className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[12px] font-bold ${STATUS_STYLE[status]}`}
            >
              <StatusIcon status={status} />
              {t.statuses[status]} {count}
            </span>
          ))}
          {pending > 0 && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-blue-200 bg-blue-50 px-3 py-1 text-[12px] font-medium text-blue-800">
              <span className="h-[6px] w-[6px] animate-pulse rounded-full bg-blue-600" />
              {t.assimilating(pending)}
            </span>
          )}
        </div>

        {error && (
          <div className="mb-[12px] rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-[12.5px] text-red-700">
            {t.loadError}
            {error}
          </div>
        )}

        {jobs.length === 0 && !error && (
          <div className="rounded-xl border border-dashed border-slate-200 p-6 text-center text-[13px] text-slate-400">
            {t.empty}
          </div>
        )}

        <div className="space-y-2">
          {jobs.map((job) => {
            const progress = progressText(job, t)
            return (
              <div
                key={job.id}
                className="rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm"
              >
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-[13.5px] font-bold text-slate-900">
                      {jobLabel(job, t)}
                    </div>
                    <div className="truncate text-[12px] text-slate-500">
                      {t.types[job.type] || job.type}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {job.status === 'queued' && (
                      <button
                        type="button"
                        onClick={() => cancelJob(job.id)}
                        className="rounded-lg border border-slate-200 px-2.5 py-1 text-[11.5px] font-semibold text-slate-500 hover:bg-slate-50 hover:text-red-600"
                      >
                        {t.cancel}
                      </button>
                    )}
                    <span
                      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11.5px] font-bold ${STATUS_STYLE[job.status] || STATUS_STYLE.cancelled}`}
                    >
                      <StatusIcon status={job.status} />
                      {job.status === 'queued' && job.position
                        ? t.queuedPos(job.position)
                        : t.statuses[job.status] || job.status}
                    </span>
                  </div>
                </div>

                {(progress || job.error) && (
                  <div className="mt-2">
                    {progress && job.status === 'running' && (
                      <div className="flex items-center gap-2 rounded-lg bg-blue-50 px-2.5 py-1.5 text-[12px] font-medium text-blue-800">
                        <Loader2 size={12} className="shrink-0 animate-spin" />
                        <span className="truncate">{progress}</span>
                      </div>
                    )}
                    {job.error && (
                      <div className="rounded-lg bg-red-50 px-2.5 py-1.5 text-[12px] text-red-700">
                        {job.error}
                      </div>
                    )}
                  </div>
                )}

                <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-400">
                  <span>{fmtTime(job.created_at)}</span>
                  {duration(job) && <span>⏱ {duration(job)}</span>}
                  <span className="font-mono">{job.id.slice(0, 8)}</span>
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
