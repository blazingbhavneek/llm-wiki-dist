import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  Database,
  ExternalLink,
  FileText,
  Globe2,
  Home,
  KeyRound,
  Loader2,
  LogOut,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Shield,
  Trash2,
  Upload,
  X,
} from 'lucide-react'

const I18N = {
  ja: {
    appTitle: 'LLM-Wiki Admin',
    appSub: 'ワークスペース管理',
    loginTitle: '管理者ログイン',
    loginText: '管理者パスワードを入力してください。',
    password: '管理者パスワード',
    login: 'ログイン',
    logout: 'ログアウト',
    authFailed: '認証に失敗しました',
    home: 'ホーム',
    databases: 'データベース',
    refresh: '更新',
    open: '開く',
    inspect: '詳細',
    delete: '削除',
    copy: 'コピー',
    rename: '名前変更',
    requestFailed: 'リクエストに失敗しました',
    dashboard: '管理ダッシュボード',
    dashboardSub: 'SQLite Wiki ワークスペースを作成・アップロード・確認・削除できます。',
    totalDbs: 'DB 合計',
    validDbs: '有効 DB',
    invalidDbs: '無効 DB',
    defaultDb: 'デフォルト DB',
    readyForWiki: 'Wiki ルートで利用可能',
    needRepair: '修復または削除が必要',
    createUploadTitle: 'Wiki DB を作成 / アップロード',
    createUploadText:
      '名前を入力してください。SQLite ファイルを選択した場合は検証・マイグレーション・ブートストラップ後に登録します。',
    dbName: 'データベース名',
    dbNamePlaceholder: 'mywiki',
    dbNameHint: '使用可能: 英数字、アンダースコア、ハイフン',
    sqliteFileOptional: 'SQLite ファイル 任意',
    sqliteFileHint: '未選択の場合は空の Wiki DB を作成します。',
    selected: '選択中',
    createEmpty: '空 DB を作成',
    uploadDb: 'SQLite をアップロード',
    nameRequired: 'データベース名を入力してください。',
    nameInvalid: '名前は英数字、アンダースコア、ハイフンのみ使用できます。',
    nameReserved: 'この名前は予約されています。',
    nameExists: 'この名前は既に存在します。別の名前を入力してください。',
    created: 'DB を作成しました',
    uploaded: 'DB をアップロードしました',
    deleted: 'DB を削除しました',
    copied: 'DB をコピーしました',
    renamed: 'DB 名を変更しました',
    confirmDelete: '削除してもよろしいですか？',
    dbTableText: '統計は SQLite から読み取り専用で取得しています。',
    name: '名前',
    status: '状態',
    docs: '文書',
    nodes: 'ノード',
    links: 'リンク',
    size: 'サイズ',
    modified: '更新日時',
    actions: '操作',
    valid: '有効',
    invalid: '無効',
    noDbs: 'データベースがありません',
    noDbsText: '新しい DB を作成するか、既存の SQLite をアップロードしてください。',
    detailTitle: 'データベース詳細',
    noSelection: 'データベースを選択してください',
    noSelectionText: '左の一覧またはテーブルから DB を選択してください。',
    openWiki: 'Wiki を開く',
    invalidDb: '無効なデータベース',
    invalidDbText: 'DB のスキーマ検証に失敗しました。',
    documents: '文書一覧',
    originalName: '元の名前',
    inferredName: '推定名',
    noDocuments: '文書がありません。',
    nodeStatus: 'ノード状態',
    active: '有効',
    deletedNodes: '削除済み',
    endogenous: '内生',
    exogenous: '外生',
    searchItems: '検索',
    dangerZone: '危険操作',
    dangerText: 'このデータベースを完全に削除します。この操作は元に戻せません。',
    apiBase: 'API base',
    loading: '読み込み中...',
    language: 'English',
    show: '表示',
    copyTitle: 'データベースをコピー',
    copyText: 'コピー先の DB 名を入力してください。',
    renameTitle: 'データベース名を変更',
    renameText: '新しい DB 名を入力してください。',
    sourceDb: '元 DB',
    targetDb: '変更後 DB 名',
    cancel: 'キャンセル',
    executeCopy: 'コピー実行',
    executeRename: '名前変更実行',
  },
  en: {
    appTitle: 'LLM-Wiki Admin',
    appSub: 'Workspace management',
    loginTitle: 'Admin Login',
    loginText: 'Enter the administrator password.',
    password: 'Admin password',
    login: 'Login',
    logout: 'Logout',
    authFailed: 'Authentication failed',
    home: 'Home',
    databases: 'Databases',
    refresh: 'Refresh',
    open: 'Open',
    inspect: 'Inspect',
    delete: 'Delete',
    copy: 'Copy',
    rename: 'Rename',
    requestFailed: 'Request failed',
    dashboard: 'Admin Dashboard',
    dashboardSub: 'Create, upload, inspect, and delete SQLite Wiki workspaces.',
    totalDbs: 'Total DBs',
    validDbs: 'Valid DBs',
    invalidDbs: 'Invalid DBs',
    defaultDb: 'Default DB',
    readyForWiki: 'Available as Wiki routes',
    needRepair: 'Needs repair or deletion',
    createUploadTitle: 'Create / Upload Wiki DB',
    createUploadText:
      'Enter a name. If you select a SQLite file, it will be validated, migrated, bootstrapped, and registered.',
    dbName: 'Database name',
    dbNamePlaceholder: 'mywiki',
    dbNameHint: 'Allowed: letters, numbers, underscore, hyphen',
    sqliteFileOptional: 'SQLite file optional',
    sqliteFileHint: 'If empty, a blank Wiki DB will be created.',
    selected: 'Selected',
    createEmpty: 'Create empty DB',
    uploadDb: 'Upload SQLite',
    nameRequired: 'Enter a database name.',
    nameInvalid: 'Only letters, numbers, underscores, and hyphens are allowed.',
    nameReserved: 'This name is reserved.',
    nameExists: 'This name already exists. Choose another name.',
    created: 'DB created',
    uploaded: 'DB uploaded',
    deleted: 'DB deleted',
    copied: 'DB copied',
    renamed: 'DB renamed',
    confirmDelete: 'Are you sure you want to delete this DB?',
    dbTableText: 'Statistics are read-only values from SQLite.',
    name: 'Name',
    status: 'Status',
    docs: 'Docs',
    nodes: 'Nodes',
    links: 'Links',
    size: 'Size',
    modified: 'Modified',
    actions: 'Actions',
    valid: 'Valid',
    invalid: 'Invalid',
    noDbs: 'No databases',
    noDbsText: 'Create a new DB or upload an existing SQLite file.',
    detailTitle: 'Database detail',
    noSelection: 'Select a database',
    noSelectionText: 'Select a DB from the sidebar or table.',
    openWiki: 'Open Wiki',
    invalidDb: 'Invalid database',
    invalidDbText: 'DB schema validation failed.',
    documents: 'Documents',
    originalName: 'Original name',
    inferredName: 'Inferred name',
    noDocuments: 'No documents.',
    nodeStatus: 'Node status',
    active: 'Active',
    deletedNodes: 'Deleted',
    endogenous: 'Endogenous',
    exogenous: 'Exogenous',
    searchItems: 'Search items',
    dangerZone: 'Danger zone',
    dangerText: 'Permanently delete this database. This action cannot be undone.',
    apiBase: 'API base',
    loading: 'Loading...',
    language: '日本語',
    show: 'Show',
    copyTitle: 'Copy database',
    copyText: 'Enter the destination DB name.',
    renameTitle: 'Rename database',
    renameText: 'Enter the new DB name.',
    sourceDb: 'Source DB',
    targetDb: 'Target DB name',
    cancel: 'Cancel',
    executeCopy: 'Copy',
    executeRename: 'Rename',
  },
}

const RESERVED_NAMES = new Set(['admin', 'assets'])
const DB_NAME_RE = /^[A-Za-z0-9_-]+$/

function detectAdminApiBase() {
  if (import.meta.env.VITE_ADMIN_API_BASE) {
    return import.meta.env.VITE_ADMIN_API_BASE.replace(/\/$/, '')
  }

  const path = window.location.pathname
  const marker = '/admin'
  const idx = path.indexOf(marker)

  if (idx >= 0) return `${path.slice(0, idx)}${marker}/api`
  return '/admin/api'
}

function detectPrefix() {
  const path = window.location.pathname
  const marker = '/admin'
  const idx = path.indexOf(marker)

  if (idx >= 0) return path.slice(0, idx)
  return ''
}

const ADMIN_API_BASE = detectAdminApiBase()
const APP_PREFIX = detectPrefix()
const LOGO_SRC = `${APP_PREFIX}/favicon.svg`

function cx(...parts) {
  return parts.filter(Boolean).join(' ')
}

function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'
  return Number(value).toLocaleString()
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || Number.isNaN(Number(bytes))) return '—'

  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let n = Number(bytes)
  let unit = 0

  while (n >= 1024 && unit < units.length - 1) {
    n /= 1024
    unit += 1
  }

  return `${n.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

function formatDate(ts) {
  if (!ts) return '—'

  try {
    return new Date(Number(ts) * 1000).toLocaleString()
  } catch {
    return '—'
  }
}

function asErrorMessage(err) {
  if (!err) return 'Unknown error'
  if (typeof err === 'string') return err
  if (err.detail) return err.detail
  if (err.message) return err.message
  return String(err)
}

function normalizeDbStats(payload) {
  return payload?.stats || payload
}

function getDocInfo(doc) {
  if (typeof doc === 'string') {
    return {
      key: doc,
      originalName: doc,
      inferredName: doc,
    }
  }

  const originalName =
    doc?.original_name ||
    doc?.originalName ||
    doc?.document_name ||
    doc?.documentName ||
    doc?.source_path ||
    doc?.sourcePath ||
    doc?.name ||
    '—'

  const inferredName =
    doc?.inferred_name ||
    doc?.inferredName ||
    doc?.title ||
    doc?.document_name ||
    doc?.documentName ||
    doc?.name ||
    originalName

  return {
    key: `${originalName}:${inferredName}`,
    originalName,
    inferredName,
  }
}

function StatCard({ icon: Icon, label, value, sub }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</p>
          <p className="mt-2 text-2xl font-bold text-slate-900">{value}</p>
          {sub && <p className="mt-1 text-xs text-slate-500">{sub}</p>}
        </div>
        {Icon && (
          <div className="rounded-xl bg-blue-50 p-2 text-blue-700">
            <Icon size={18} />
          </div>
        )}
      </div>
    </div>
  )
}

function Pill({ children, tone = 'slate' }) {
  const tones = {
    slate: 'border-slate-200 bg-slate-50 text-slate-700',
    blue: 'border-blue-200 bg-blue-50 text-blue-700',
    green: 'border-emerald-200 bg-emerald-50 text-emerald-700',
    red: 'border-red-200 bg-red-50 text-red-700',
    amber: 'border-amber-200 bg-amber-50 text-amber-700',
  }

  return (
    <span
      className={cx(
        'inline-flex items-center rounded-full border px-2.5 py-1 text-xs font-semibold',
        tones[tone] || tones.slate,
      )}
    >
      {children}
    </span>
  )
}

function PanelTitle({ icon: Icon, title, text, action }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
      <div className="flex items-start gap-3">
        {Icon && (
          <div className="mt-0.5 rounded-xl bg-blue-50 p-2 text-blue-700">
            <Icon size={18} />
          </div>
        )}
        <div>
          <h2 className="text-base font-bold text-slate-900">{title}</h2>
          {text && <p className="mt-1 text-sm text-slate-500">{text}</p>}
        </div>
      </div>
      {action}
    </div>
  )
}

function Button({
  children,
  type = 'button',
  tone = 'slate',
  size = 'md',
  busy = false,
  disabled = false,
  className = '',
  ...props
}) {
  const tones = {
    slate: 'border-slate-300 bg-white text-slate-700 hover:border-slate-400 hover:bg-slate-50',
    blue: 'border-blue-700 bg-blue-700 text-white hover:border-blue-800 hover:bg-blue-800',
    red: 'border-red-600 bg-red-600 text-white hover:border-red-700 hover:bg-red-700',
    ghost: 'border-transparent bg-transparent text-slate-600 hover:bg-slate-100',
  }

  const sizes = {
    sm: 'px-3 py-1.5 text-xs',
    md: 'px-4 py-2 text-sm',
  }

  return (
    <button
      type={type}
      disabled={disabled || busy}
      className={cx(
        'inline-flex items-center justify-center gap-2 rounded-xl border font-bold transition disabled:cursor-not-allowed disabled:opacity-50',
        tones[tone],
        sizes[size],
        className,
      )}
      {...props}
    >
      {busy && <Loader2 size={15} className="animate-spin" />}
      {children}
    </button>
  )
}

function TextInput({ label, hint, error, className = '', ...props }) {
  return (
    <label className={cx('block', className)}>
      {label && <span className="mb-1.5 block text-xs font-bold text-slate-600">{label}</span>}
      <input
        className={cx(
          'w-full rounded-xl border bg-white px-3 py-2 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:ring-4',
          error
            ? 'border-red-300 focus:border-red-600 focus:ring-red-100'
            : 'border-slate-300 focus:border-blue-600 focus:ring-blue-100',
        )}
        {...props}
      />
      {error ? (
        <span className="mt-1.5 block text-xs font-semibold text-red-700">{error}</span>
      ) : hint ? (
        <span className="mt-1.5 block text-xs text-slate-500">{hint}</span>
      ) : null}
    </label>
  )
}

function EmptyState({ icon: Icon, title, text }) {
  return (
    <div className="flex min-h-[260px] items-center justify-center p-8">
      <div className="max-w-sm text-center">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-slate-100 text-slate-500">
          <Icon size={22} />
        </div>
        <h3 className="mt-4 text-base font-bold text-slate-900">{title}</h3>
        <p className="mt-2 text-sm leading-6 text-slate-500">{text}</p>
      </div>
    </div>
  )
}

function Modal({ title, text, children, onClose }) {
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-slate-950/40 p-4 backdrop-blur-sm">
      <div className="w-[460px] max-w-full rounded-3xl border border-slate-200 bg-white shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
          <div>
            <h3 className="text-lg font-bold text-slate-900">{title}</h3>
            {text && <p className="mt-1 text-sm text-slate-500">{text}</p>}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1 text-slate-500 hover:bg-slate-100"
          >
            <X size={18} />
          </button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  )
}

export default function AdminApp() {
  const [lang, setLang] = useState(() => localStorage.getItem('llm-wiki-admin-lang') || 'ja')
  const t = I18N[lang] || I18N.ja

  const [authed, setAuthed] = useState(false)
  const [password, setPassword] = useState('')
  const [loginPassword, setLoginPassword] = useState('')
  const [showLoginPassword, setShowLoginPassword] = useState(false)

  const [dbs, setDbs] = useState([])
  const [defaultDb, setDefaultDb] = useState(null)
  const [dbDir, setDbDir] = useState(null)

  const [view, setView] = useState('home')
  const [selectedName, setSelectedName] = useState(null)
  const [selected, setSelected] = useState(null)

  const [loading, setLoading] = useState(false)
  const [loginBusy, setLoginBusy] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [busyAction, setBusyAction] = useState(null)
  const [error, setError] = useState(null)
  const [toast, setToast] = useState(null)

  const [newName, setNewName] = useState('')
  const [sqliteFile, setSqliteFile] = useState(null)

  const [copySource, setCopySource] = useState(null)
  const [copyTarget, setCopyTarget] = useState('')
  const [renameSource, setRenameSource] = useState(null)
  const [renameTarget, setRenameTarget] = useState('')

  const fileRef = useRef(null)

  const existingNames = useMemo(() => new Set(dbs.map((db) => db.name)), [dbs])
  const validCount = useMemo(() => dbs.filter((db) => db.valid).length, [dbs])
  const invalidCount = useMemo(() => dbs.filter((db) => !db.valid).length, [dbs])

  const trimmedName = newName.trim()

  const validateName = (name, { allowCurrent = null } = {}) => {
    const v = String(name || '').trim()
    if (!v) return null
    if (!DB_NAME_RE.test(v)) return t.nameInvalid
    if (RESERVED_NAMES.has(v)) return t.nameReserved
    if (existingNames.has(v) && v !== allowCurrent) return t.nameExists
    return null
  }

  const nameError = useMemo(() => validateName(trimmedName), [trimmedName, existingNames, lang])
  const copyTargetError = useMemo(() => validateName(copyTarget), [copyTarget, existingNames, lang])
  const renameTargetError = useMemo(
    () => validateName(renameTarget, { allowCurrent: renameSource }),
    [renameTarget, renameSource, existingNames, lang],
  )

  const canCreateOrUpload = !!trimmedName && !nameError && !busyAction
  const canCopy = !!copySource && !!copyTarget.trim() && !copyTargetError && !busyAction
  const canRename =
    !!renameSource &&
    !!renameTarget.trim() &&
    renameTarget.trim() !== renameSource &&
    !renameTargetError &&
    !busyAction

  const selectedFromList = useMemo(() => {
    if (!selectedName) return null
    return dbs.find((db) => db.name === selectedName) || null
  }, [dbs, selectedName])

  const detail = selected || selectedFromList

  const fireToast = (text) => {
    setToast(text)
    window.clearTimeout(fireToast._timer)
    fireToast._timer = window.setTimeout(() => setToast(null), 3600)
  }

  const toggleLanguage = () => {
    const next = lang === 'ja' ? 'en' : 'ja'
    setLang(next)
    localStorage.setItem('llm-wiki-admin-lang', next)
  }

  const apiFetch = async (path, options = {}, passwordOverride = password) => {
    if (!passwordOverride) throw new Error(t.password)

    const isForm = options.body instanceof FormData
    const headers = {
      'X-Admin-Password': passwordOverride,
      ...(isForm ? {} : { 'Content-Type': 'application/json' }),
      ...(options.headers || {}),
    }

    const res = await fetch(`${ADMIN_API_BASE}${path}`, {
      ...options,
      headers,
    })

    let data = null
    const text = await res.text()

    if (text) {
      try {
        data = JSON.parse(text)
      } catch {
        data = { detail: text }
      }
    }

    if (!res.ok) {
      const detailText = data?.detail || data?.error || res.statusText
      const code = data?.code ? ` (${data.code})` : ''
      throw new Error(`${detailText}${code}`)
    }

    return data
  }

  const applyListPayload = (data) => {
    const nextDbs = Array.isArray(data?.dbs) ? data.dbs : []
    setDbs(nextDbs)
    setDefaultDb(data?.default || null)
    setDbDir(data?.db_dir || null)
  }

  const authenticate = async (e) => {
    e.preventDefault()
    if (!loginPassword) return

    setLoginBusy(true)
    setError(null)

    try {
      const data = await apiFetch('/dbs', {}, loginPassword)
      setPassword(loginPassword)
      setAuthed(true)
      applyListPayload(data)
      setView('home')
      setSelectedName(null)
      setSelected(null)
    } catch (err) {
      setError(`${t.authFailed}: ${asErrorMessage(err)}`)
    } finally {
      setLoginBusy(false)
    }
  }

  const logout = () => {
    setAuthed(false)
    setPassword('')
    setLoginPassword('')
    setSelectedName(null)
    setSelected(null)
    setView('home')
  }

  const refresh = async () => {
    setLoading(true)
    setError(null)

    try {
      const data = await apiFetch('/dbs')
      applyListPayload(data)

      if (selectedName && !data.dbs?.some((db) => db.name === selectedName)) {
        setSelectedName(null)
        setSelected(null)
        setView('home')
      }
    } catch (err) {
      setError(asErrorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  const loadDetail = async (name) => {
    if (!name) {
      setSelected(null)
      return
    }

    setDetailLoading(true)
    setError(null)

    try {
      const data = await apiFetch(`/dbs/${encodeURIComponent(name)}`)
      setSelected(normalizeDbStats(data))
    } catch (err) {
      setSelected({
        name,
        valid: false,
        error: asErrorMessage(err),
      })
    } finally {
      setDetailLoading(false)
    }
  }

  const selectDb = (name) => {
    setSelectedName(name)
    setView('detail')
  }

  const goHome = () => {
    setView('home')
    setSelectedName(null)
    setSelected(null)
  }

  const createOrUpload = async () => {
    if (!trimmedName) {
      setError(t.nameRequired)
      return
    }

    if (nameError) {
      setError(nameError)
      return
    }

    setBusyAction('createUpload')
    setError(null)

    try {
      let data

      if (sqliteFile) {
        const form = new FormData()
        form.append('file', sqliteFile)

        const params = new URLSearchParams({
          replace: 'false',
          bootstrap: 'true',
        })

        data = await apiFetch(`/dbs/${encodeURIComponent(trimmedName)}/upload?${params}`, {
          method: 'POST',
          body: form,
        })

        fireToast(`${t.uploaded}: ${trimmedName}`)
      } else {
        data = await apiFetch(`/dbs/${encodeURIComponent(trimmedName)}`, {
          method: 'POST',
          headers: {},
        })

        fireToast(`${t.created}: ${trimmedName}`)
      }

      const stats = normalizeDbStats(data)

      setNewName('')
      setSqliteFile(null)
      if (fileRef.current) fileRef.current.value = ''

      await refresh()
      setSelectedName(trimmedName)
      setSelected(stats)
      setView('detail')
    } catch (err) {
      setError(asErrorMessage(err))
    } finally {
      setBusyAction(null)
    }
  }

  const deleteDb = async (name) => {
    if (!name) return
    if (!window.confirm(`${t.confirmDelete}\n\n${name}`)) return

    setBusyAction(`delete:${name}`)
    setError(null)

    try {
      await apiFetch(`/dbs/${encodeURIComponent(name)}`, {
        method: 'DELETE',
      })

      fireToast(`${t.deleted}: ${name}`)

      if (selectedName === name) {
        setSelectedName(null)
        setSelected(null)
        setView('home')
      }

      await refresh()
    } catch (err) {
      setError(asErrorMessage(err))
    } finally {
      setBusyAction(null)
    }
  }

  const openCopyModal = (name) => {
    setCopySource(name)
    setCopyTarget(`${name}_copy`)
  }

  const openRenameModal = (name) => {
    setRenameSource(name)
    setRenameTarget(name)
  }

  const copyDb = async () => {
    if (!canCopy) return

    const src = copySource
    const target = copyTarget.trim()

    setBusyAction(`copy:${src}`)
    setError(null)

    try {
      const data = await apiFetch(`/dbs/${encodeURIComponent(src)}/copy`, {
        method: 'POST',
        body: JSON.stringify({
          target,
          bootstrap: true,
        }),
      })

      const stats = normalizeDbStats(data)

      fireToast(`${t.copied}: ${src} → ${target}`)
      setCopySource(null)
      setCopyTarget('')

      await refresh()
      setSelectedName(target)
      setSelected(stats)
      setView('detail')
    } catch (err) {
      setError(asErrorMessage(err))
    } finally {
      setBusyAction(null)
    }
  }

  const renameDb = async () => {
    if (!canRename) return

    const src = renameSource
    const target = renameTarget.trim()

    setBusyAction(`rename:${src}`)
    setError(null)

    try {
      const data = await apiFetch(`/dbs/${encodeURIComponent(src)}/rename`, {
        method: 'PATCH',
        body: JSON.stringify({
          target,
        }),
      })

      const stats = normalizeDbStats(data)

      fireToast(`${t.renamed}: ${src} → ${target}`)
      setRenameSource(null)
      setRenameTarget('')

      await refresh()
      setSelectedName(target)
      setSelected(stats)
      setView('detail')
    } catch (err) {
      setError(asErrorMessage(err))
    } finally {
      setBusyAction(null)
    }
  }

  useEffect(() => {
    if (authed && selectedName) loadDetail(selectedName)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedName, authed])

  const renderDbActions = (db) => (
    <div className="flex flex-wrap justify-end gap-2" onClick={(e) => e.stopPropagation()}>
      {db.url && (
        <a
          href={db.url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-50"
        >
          <ExternalLink size={13} />
          {t.open}
        </a>
      )}

      <button
        type="button"
        onClick={() => selectDb(db.name)}
        className="inline-flex items-center gap-1 rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-50"
      >
        <Search size={13} />
        {t.inspect}
      </button>

      <button
        type="button"
        onClick={() => openCopyModal(db.name)}
        disabled={busyAction === `copy:${db.name}`}
        className="inline-flex items-center gap-1 rounded-lg border border-blue-200 bg-blue-50 px-2.5 py-1.5 text-xs font-bold text-blue-700 hover:bg-blue-100 disabled:opacity-50"
      >
        {busyAction === `copy:${db.name}` ? <Loader2 size={13} className="animate-spin" /> : <Copy size={13} />}
        {t.copy}
      </button>

      <button
        type="button"
        onClick={() => openRenameModal(db.name)}
        disabled={busyAction === `rename:${db.name}`}
        className="inline-flex items-center gap-1 rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-50 disabled:opacity-50"
      >
        {busyAction === `rename:${db.name}` ? <Loader2 size={13} className="animate-spin" /> : <Pencil size={13} />}
        {t.rename}
      </button>

      <button
        type="button"
        onClick={() => deleteDb(db.name)}
        disabled={busyAction === `delete:${db.name}`}
        className="inline-flex items-center gap-1 rounded-lg border border-red-200 bg-red-50 px-2.5 py-1.5 text-xs font-bold text-red-700 hover:bg-red-100 disabled:opacity-50"
      >
        {busyAction === `delete:${db.name}` ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
        {t.delete}
      </button>
    </div>
  )

  const renderDbTable = () => (
    <section className="mt-5 overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
      <PanelTitle
        icon={Database}
        title={t.databases}
        text={t.dbTableText}
        action={
          <Button size="sm" onClick={refresh} busy={loading}>
            <RefreshCw size={14} />
            {t.refresh}
          </Button>
        }
      />

      {loading ? (
        <div className="flex h-[260px] items-center justify-center text-slate-500">
          <Loader2 size={22} className="mr-2 animate-spin" />
          {t.loading}
        </div>
      ) : dbs.length === 0 ? (
        <EmptyState icon={Database} title={t.noDbs} text={t.noDbsText} />
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200">
            <thead className="bg-slate-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-bold uppercase tracking-wide text-slate-500">{t.name}</th>
                <th className="px-4 py-3 text-left text-xs font-bold uppercase tracking-wide text-slate-500">{t.status}</th>
                <th className="px-4 py-3 text-right text-xs font-bold uppercase tracking-wide text-slate-500">{t.docs}</th>
                <th className="px-4 py-3 text-right text-xs font-bold uppercase tracking-wide text-slate-500">{t.nodes}</th>
                <th className="px-4 py-3 text-right text-xs font-bold uppercase tracking-wide text-slate-500">{t.links}</th>
                <th className="px-4 py-3 text-right text-xs font-bold uppercase tracking-wide text-slate-500">{t.size}</th>
                <th className="px-4 py-3 text-left text-xs font-bold uppercase tracking-wide text-slate-500">{t.modified}</th>
                <th className="px-4 py-3 text-right text-xs font-bold uppercase tracking-wide text-slate-500">{t.actions}</th>
              </tr>
            </thead>

            <tbody className="divide-y divide-slate-100 bg-white">
              {dbs.map((db) => (
                <tr
                  key={db.name}
                  className={cx(
                    'cursor-pointer transition hover:bg-blue-50/50',
                    selectedName === db.name && 'bg-blue-50',
                  )}
                  onClick={() => selectDb(db.name)}
                >
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <Database size={15} className="text-slate-400" />
                      <span className="font-bold text-slate-900">{db.name}</span>
                      {db.name === defaultDb && <Pill tone="blue">{t.defaultDb}</Pill>}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    {db.valid ? <Pill tone="green">{t.valid}</Pill> : <Pill tone="amber">{t.invalid}</Pill>}
                  </td>
                  <td className="px-4 py-3 text-right text-sm text-slate-700">{formatNumber(db.docs_count)}</td>
                  <td className="px-4 py-3 text-right text-sm text-slate-700">{formatNumber(db.nodes_total)}</td>
                  <td className="px-4 py-3 text-right text-sm text-slate-700">{formatNumber(db.links_total)}</td>
                  <td className="px-4 py-3 text-right text-sm text-slate-700">{formatBytes(db.size_bytes)}</td>
                  <td className="px-4 py-3 text-sm text-slate-500">{formatDate(db.modified_at)}</td>
                  <td className="px-4 py-3">{renderDbActions(db)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )

  const renderCreateUpload = () => (
    <section className="mt-5 overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
      <PanelTitle icon={Upload} title={t.createUploadTitle} text={t.createUploadText} />

      <div className="grid grid-cols-1 gap-5 p-5 xl:grid-cols-[1fr_1fr_auto] xl:items-end">
        <TextInput
          label={t.dbName}
          placeholder={t.dbNamePlaceholder}
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          hint={t.dbNameHint}
          error={nameError}
        />

        <label className="block">
          <span className="mb-1.5 block text-xs font-bold text-slate-600">{t.sqliteFileOptional}</span>
          <input
            ref={fileRef}
            type="file"
            accept=".sqlite,.db,application/vnd.sqlite3,application/octet-stream"
            onChange={(e) => setSqliteFile(e.target.files?.[0] || null)}
            className="block w-full cursor-pointer rounded-xl border border-slate-300 bg-white text-sm text-slate-700 file:mr-4 file:border-0 file:bg-slate-100 file:px-4 file:py-2.5 file:text-sm file:font-bold file:text-slate-700 hover:file:bg-slate-200"
          />
          <span className="mt-1.5 block text-xs text-slate-500">
            {sqliteFile ? `${t.selected}: ${sqliteFile.name} · ${formatBytes(sqliteFile.size)}` : t.sqliteFileHint}
          </span>
        </label>

        <Button
          tone="blue"
          onClick={createOrUpload}
          busy={busyAction === 'createUpload'}
          disabled={!canCreateOrUpload}
          className="xl:mb-[22px]"
        >
          {sqliteFile ? <Upload size={15} /> : <Plus size={15} />}
          {sqliteFile ? t.uploadDb : t.createEmpty}
        </Button>
      </div>
    </section>
  )

  const renderHome = () => (
    <>
      <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={Database} label={t.totalDbs} value={formatNumber(dbs.length)} sub={dbDir || '—'} />
        <StatCard icon={CheckCircle2} label={t.validDbs} value={formatNumber(validCount)} sub={t.readyForWiki} />
        <StatCard icon={AlertTriangle} label={t.invalidDbs} value={formatNumber(invalidCount)} sub={t.needRepair} />
        <StatCard icon={Shield} label={t.defaultDb} value={defaultDb || '—'} sub="WIKI_DEFAULT_DB" />
      </section>

      {renderCreateUpload()}
      {renderDbTable()}
    </>
  )

  const renderDetail = () => {
    if (detailLoading && !detail) {
      return (
        <div className="flex h-full items-center justify-center text-slate-500">
          <Loader2 size={22} className="mr-2 animate-spin" />
          {t.loading}
        </div>
      )
    }

    if (!detail) {
      return <EmptyState icon={Database} title={t.noSelection} text={t.noSelectionText} />
    }

    const docs = Array.isArray(detail.docs) ? detail.docs.map(getDocInfo) : []

    return (
      <div className="space-y-5">
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <div className="flex items-center gap-3">
                <h2 className="text-2xl font-bold text-slate-900">{detail.name}</h2>
                {detail.valid ? <Pill tone="green">{t.valid}</Pill> : <Pill tone="amber">{t.invalid}</Pill>}
              </div>
              <p className="mt-1 text-sm text-slate-500">
                {formatBytes(detail.size_bytes)} · {formatDate(detail.modified_at)}
              </p>
            </div>

            <div className="flex flex-wrap justify-end gap-2">
              {detail.url && (
                <a
                  href={detail.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-2 rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-bold text-slate-700 hover:bg-slate-50"
                >
                  <ExternalLink size={15} />
                  {t.openWiki}
                </a>
              )}

              <Button onClick={() => openCopyModal(detail.name)}>
                <Copy size={15} />
                {t.copy}
              </Button>

              <Button onClick={() => openRenameModal(detail.name)}>
                <Pencil size={15} />
                {t.rename}
              </Button>

              <Button onClick={() => loadDetail(detail.name)} busy={detailLoading}>
                <RefreshCw size={15} />
                {t.refresh}
              </Button>
            </div>
          </div>
        </div>

        {!detail.valid && (
          <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4 text-amber-900">
            <div className="flex items-start gap-3">
              <AlertTriangle size={18} className="mt-0.5 shrink-0" />
              <div>
                <p className="font-bold">{t.invalidDb}</p>
                <p className="mt-1 break-words text-sm leading-6">{detail.error || t.invalidDbText}</p>
              </div>
            </div>
          </div>
        )}

        <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
          <PanelTitle icon={FileText} title={t.documents} text={`${formatNumber(docs.length)} ${t.docs}`} />

          {docs.length === 0 ? (
            <p className="p-5 text-sm text-slate-500">{t.noDocuments}</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200">
                <thead className="bg-slate-50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-bold uppercase tracking-wide text-slate-500">
                      {t.originalName}
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-bold uppercase tracking-wide text-slate-500">
                      {t.inferredName}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {docs.map((doc) => (
                    <tr key={doc.key}>
                      <td className="max-w-[420px] truncate px-4 py-3 text-sm font-semibold text-slate-800" title={doc.originalName}>
                        {doc.originalName}
                      </td>
                      <td className="max-w-[420px] truncate px-4 py-3 text-sm text-slate-600" title={doc.inferredName}>
                        {doc.inferredName}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
          <StatCard icon={FileText} label={t.docs} value={formatNumber(detail.docs_count)} />
          <StatCard icon={Database} label={t.nodes} value={formatNumber(detail.nodes_total)} />
          <StatCard icon={Database} label={t.links} value={formatNumber(detail.links_total)} />
          <StatCard icon={Search} label={t.searchItems} value={formatNumber(detail.search_items_total)} />
        </section>

        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <h3 className="text-base font-bold text-slate-900">{t.nodeStatus}</h3>

          <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
            <div className="rounded-xl bg-slate-50 p-4">
              <p className="text-xs font-bold text-slate-500">{t.active}</p>
              <p className="mt-1 text-xl font-bold">{formatNumber(detail.nodes_active)}</p>
            </div>
            <div className="rounded-xl bg-slate-50 p-4">
              <p className="text-xs font-bold text-slate-500">{t.deletedNodes}</p>
              <p className="mt-1 text-xl font-bold">{formatNumber(detail.nodes_deleted)}</p>
            </div>
            <div className="rounded-xl bg-slate-50 p-4">
              <p className="text-xs font-bold text-slate-500">{t.endogenous}</p>
              <p className="mt-1 text-xl font-bold">{formatNumber(detail.nodes_endo)}</p>
            </div>
            <div className="rounded-xl bg-slate-50 p-4">
              <p className="text-xs font-bold text-slate-500">{t.exogenous}</p>
              <p className="mt-1 text-xl font-bold">{formatNumber(detail.nodes_exo)}</p>
            </div>
          </div>
        </section>

        <section className="rounded-2xl border border-red-200 bg-red-50 p-5">
          <div className="flex items-start gap-3">
            <Trash2 size={20} className="mt-0.5 shrink-0 text-red-700" />
            <div className="min-w-0 flex-1">
              <h3 className="text-base font-bold text-red-900">{t.dangerZone}</h3>
              <p className="mt-1 text-sm leading-6 text-red-800">{t.dangerText}</p>

              <Button
                className="mt-4"
                tone="red"
                onClick={() => deleteDb(detail.name)}
                busy={busyAction === `delete:${detail.name}`}
              >
                <Trash2 size={15} />
                {t.delete}
              </Button>
            </div>
          </div>
        </section>
      </div>
    )
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-[#f6f8fc] text-slate-900">
      {!authed && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-white/80 backdrop-blur-sm">
          <form
            onSubmit={authenticate}
            className="w-[420px] max-w-[calc(100vw-32px)] rounded-3xl border border-slate-200 bg-white p-8 shadow-2xl"
          >
            <div className="flex flex-col items-center text-center">
              <img src={LOGO_SRC} alt="LLM-Wiki" className="h-14 w-14 rounded-2xl" />
              <h1 className="mt-5 text-2xl font-bold text-slate-900">{t.loginTitle}</h1>
              <p className="mt-2 text-sm text-slate-500">{t.loginText}</p>
            </div>

            {error && (
              <div className="mt-5 rounded-2xl border border-red-200 bg-red-50 p-3 text-sm text-red-800">
                {error}
              </div>
            )}

            <label className="relative mt-6 block">
              <span className="mb-1.5 block text-xs font-bold text-slate-600">{t.password}</span>
              <span className="pointer-events-none absolute left-3 top-[38px] text-slate-400">
                <KeyRound size={16} />
              </span>
              <input
                type={showLoginPassword ? 'text' : 'password'}
                value={loginPassword}
                onChange={(e) => setLoginPassword(e.target.value)}
                className="w-full rounded-xl border border-slate-300 bg-white py-2 pl-10 pr-3 text-sm outline-none transition focus:border-blue-600 focus:ring-4 focus:ring-blue-100"
                autoFocus
              />
            </label>

            <div className="mt-3 flex items-center justify-between">
              <label className="inline-flex items-center gap-2 text-sm text-slate-600">
                <input
                  type="checkbox"
                  checked={showLoginPassword}
                  onChange={(e) => setShowLoginPassword(e.target.checked)}
                />
                {t.show}
              </label>

              <button
                type="button"
                onClick={toggleLanguage}
                className="inline-flex items-center gap-1 text-sm font-bold text-blue-700 hover:text-blue-800"
              >
                <Globe2 size={15} />
                {t.language}
              </button>
            </div>

            <Button type="submit" tone="blue" busy={loginBusy} disabled={!loginPassword} className="mt-6 w-full">
              {t.login}
            </Button>
          </form>
        </div>
      )}

      <aside className="flex w-[260px] shrink-0 flex-col border-r border-slate-200 bg-slate-950 text-white">
        <div className="border-b border-white/10 px-5 py-5">
          <div className="flex items-center gap-3">
            <img src={LOGO_SRC} alt="LLM-Wiki" className="h-10 w-10 rounded-2xl" />
            <div>
              <h1 className="text-base font-bold">{t.appTitle}</h1>
              <p className="mt-0.5 text-xs text-slate-400">{t.appSub}</p>
            </div>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-4">
          <button
            type="button"
            onClick={goHome}
            className={cx(
              'flex w-full items-center gap-2 rounded-xl px-3 py-2.5 text-left text-sm font-semibold transition',
              view === 'home' ? 'bg-blue-600 text-white' : 'text-slate-300 hover:bg-white/10 hover:text-white',
            )}
          >
            <Home size={15} />
            {t.home}
          </button>

          <button
            type="button"
            onClick={refresh}
            disabled={loading || !authed}
            className="mt-3 flex w-full items-center justify-between rounded-xl border border-white/10 bg-white/5 px-3 py-2.5 text-left text-sm font-semibold text-slate-100 transition hover:bg-white/10 disabled:opacity-50"
          >
            <span className="flex items-center gap-2">
              <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
              {t.refresh}
            </span>
            <span className="rounded-full bg-white/10 px-2 py-0.5 text-xs">{dbs.length}</span>
          </button>

          <p className="mt-5 px-2 text-xs font-bold uppercase tracking-wide text-slate-500">{t.databases}</p>

          <div className="mt-3 space-y-1">
            {dbs.map((db) => (
              <button
                key={db.name}
                type="button"
                onClick={() => selectDb(db.name)}
                className={cx(
                  'flex w-full items-center justify-between rounded-xl px-3 py-2.5 text-left text-sm transition',
                  view === 'detail' && selectedName === db.name
                    ? 'bg-blue-600 text-white'
                    : 'text-slate-300 hover:bg-white/10 hover:text-white',
                )}
              >
                <span className="flex min-w-0 items-center gap-2">
                  <Database size={15} className="shrink-0" />
                  <span className="truncate font-semibold">{db.name}</span>
                </span>

                {db.valid ? (
                  <CheckCircle2 size={15} className="shrink-0 text-emerald-300" />
                ) : (
                  <AlertTriangle size={15} className="shrink-0 text-amber-300" />
                )}
              </button>
            ))}
          </div>
        </div>

        <div className="border-t border-white/10 p-4">
          <div className="text-xs text-slate-500">
            {t.apiBase}
            <div className="mt-1 break-all rounded-lg bg-black/20 p-2 font-mono text-slate-400">
              {ADMIN_API_BASE}
            </div>
          </div>

          <button
            type="button"
            onClick={logout}
            className="mt-3 flex w-full items-center justify-center gap-2 rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-sm font-bold text-slate-200 hover:bg-white/10"
          >
            <LogOut size={15} />
            {t.logout}
          </button>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-[66px] shrink-0 items-center justify-between border-b border-slate-200 bg-white px-5">
          <div>
            <h2 className="text-lg font-bold text-slate-900">
              {view === 'detail' && detail?.name ? detail.name : t.dashboard}
            </h2>
            <p className="text-xs text-slate-500">{view === 'detail' ? t.detailTitle : t.dashboardSub}</p>
          </div>

          <Button onClick={toggleLanguage}>
            <Globe2 size={15} />
            {t.language}
          </Button>
        </header>

        <main className="relative min-h-0 flex-1 overflow-y-auto p-5">
          {error && authed && (
            <div className="mb-5 flex items-start gap-3 rounded-2xl border border-red-200 bg-red-50 p-4 text-red-800">
              <AlertTriangle size={18} className="mt-0.5 shrink-0" />
              <div className="min-w-0 flex-1">
                <p className="font-bold">{t.requestFailed}</p>
                <p className="mt-1 break-words text-sm">{error}</p>
              </div>
              <button type="button" onClick={() => setError(null)} className="rounded-lg p-1 text-red-700 hover:bg-red-100">
                <X size={16} />
              </button>
            </div>
          )}

          {view === 'detail' ? renderDetail() : renderHome()}

          {toast && (
            <div className="absolute bottom-[22px] right-[22px] z-30 max-w-[390px] rounded-xl border border-emerald-200 bg-emerald-50 px-[14px] py-[13px] text-[13px] leading-[1.45] text-emerald-800 shadow-xl">
              {toast}
            </div>
          )}
        </main>
      </div>

      {copySource && (
        <Modal title={t.copyTitle} text={t.copyText} onClose={() => setCopySource(null)}>
          <div className="space-y-4">
            <div>
              <p className="text-xs font-bold text-slate-500">{t.sourceDb}</p>
              <p className="mt-1 rounded-xl bg-slate-50 px-3 py-2 font-mono text-sm text-slate-800">{copySource}</p>
            </div>

            <TextInput
              label={t.targetDb}
              value={copyTarget}
              onChange={(e) => setCopyTarget(e.target.value)}
              hint={t.dbNameHint}
              error={copyTargetError}
              autoFocus
            />

            <div className="flex justify-end gap-2 pt-2">
              <Button onClick={() => setCopySource(null)}>{t.cancel}</Button>
              <Button tone="blue" onClick={copyDb} busy={busyAction === `copy:${copySource}`} disabled={!canCopy}>
                <Copy size={15} />
                {t.executeCopy}
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {renameSource && (
        <Modal title={t.renameTitle} text={t.renameText} onClose={() => setRenameSource(null)}>
          <div className="space-y-4">
            <div>
              <p className="text-xs font-bold text-slate-500">{t.sourceDb}</p>
              <p className="mt-1 rounded-xl bg-slate-50 px-3 py-2 font-mono text-sm text-slate-800">{renameSource}</p>
            </div>

            <TextInput
              label={t.targetDb}
              value={renameTarget}
              onChange={(e) => setRenameTarget(e.target.value)}
              hint={t.dbNameHint}
              error={renameTargetError}
              autoFocus
            />

            <div className="flex justify-end gap-2 pt-2">
              <Button onClick={() => setRenameSource(null)}>{t.cancel}</Button>
              <Button tone="blue" onClick={renameDb} busy={busyAction === `rename:${renameSource}`} disabled={!canRename}>
                <Pencil size={15} />
                {t.executeRename}
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}
