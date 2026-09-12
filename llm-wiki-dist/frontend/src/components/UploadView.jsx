import { useEffect, useRef, useState } from 'react'
import PdfParserView from './PdfParserView'
import { api } from '../api'
import { useT } from '../i18n.jsx'

const COOKIE_PREFIX = 'llm_wiki_setting_'
const PDF_API_FIELD = 'pdf_parser_api_base'
const PDF_API_COOKIE = `${COOKIE_PREFIX}${PDF_API_FIELD}`

// Old cookie name used by the previous UploadView.
// Kept for backward compatibility.
const LEGACY_PDF_API_COOKIE = 'pdf_parser_api_base'

function getCookie(name) {
  if (typeof document === 'undefined') return null

  const prefix = `${name}=`
  const row = document.cookie
    .split('; ')
    .find((item) => item.startsWith(prefix))

  if (!row) return null

  return decodeURIComponent(row.slice(prefix.length))
}

function clean(value) {
  return String(value ?? '').trim()
}

function getSettingValue(source, field, altField) {
  if (!source) return undefined

  if (source[field] !== undefined) return source[field]
  if (altField && source[altField] !== undefined) return source[altField]

  return undefined
}

function readParserBase({ pdfApiBase, settingsSource }) {
  const saved =
    getCookie(PDF_API_COOKIE) ??
    getCookie(LEGACY_PDF_API_COOKIE)

  if (saved !== null) return clean(saved)

  return clean(
    getSettingValue(settingsSource, PDF_API_FIELD, 'pdfParserApiBase') ??
      pdfApiBase ??
      '',
  )
}

const STR = {
  ja: {
    readingFile: 'Markdown ファイルを読み込み中...',
    fileEmpty: 'ファイルが空です。',
    added: 'グラフに追加し、ワークスペースで開きました。',
    title: 'Markdown をアップロード',
    hint: '.md ファイルをソース資料としてグラフに直接追加します。',
    adding: '追加中...',
    choose: 'Markdown を選択',
    noFile: 'ファイルが選択されていません',
    readyToUpload: 'Markdown ファイルを読み込みました。名前を確認してアップロードしてください。',
    markdownName: 'Markdown 名',
    markdownNamePlaceholder: '例: notes.md',
    uploadMarkdown: 'アップロード',
    markdownNameRequired: 'Markdown 名を入力してください。',
    markdownRequired: 'Markdown ファイルを選択してください。',
    ingestMode: '取り込みモード',
    modeDefault: (mode) => `設定に従う（${mode}）`,
    modeChunks: 'チャンク',
    modePages: 'ページ',
    modeWiki: 'Wiki',
    modeHint:
      'このドキュメントの分割方法だけを変更します。設定の値より先に、ここで選んだ方が適用されます。',
    modeNote:
      '短い文書は分割して取り込まないため、この選択は影響しません。',
    syncRaw: 'raw/ から同期',
    downloadWiki: 'Wiki をダウンロード',
    syncStarted: '同期ジョブをキューに追加しました。',
    syncNeedsRoot: 'WIKI_DATA_ROOT が設定されていません。',
  },
  en: {
    readingFile: 'Reading markdown file...',
    fileEmpty: 'File is empty.',
    added: 'Added to the graph and opened in the workspace.',
    title: 'Upload markdown',
    hint: 'Add a .md file straight to the graph as source material.',
    adding: 'Adding...',
    choose: 'Choose markdown',
    noFile: 'No file selected',
    readyToUpload: 'Markdown file is ready. Check the name, then upload it.',
    markdownName: 'Markdown name',
    markdownNamePlaceholder: 'e.g. notes.md',
    uploadMarkdown: 'Upload markdown',
    markdownNameRequired: 'Please enter a markdown name.',
    markdownRequired: 'Please choose a markdown file first.',
    ingestMode: 'Ingest mode',
    modeDefault: (mode) => `Follow settings (${mode})`,
    modeChunks: 'Chunks',
    modePages: 'Pages',
    modeWiki: 'Wiki',
    modeHint:
      'Changes how this document alone is split. What you pick here wins over the setting.',
    modeNote:
      'Short documents are ingested without splitting, so this choice does not affect them.',
    syncRaw: 'Sync from raw/',
    downloadWiki: 'Download wiki (zip)',
    syncStarted: 'Sync job queued.',
    syncNeedsRoot: 'WIKI_DATA_ROOT is not configured.',
  },
}

// "Upload" workspace tab. Two sources of source material:
//  1. Markdown file -> selected -> named -> added to the graph as endogenous source.
//  2. PDF -> parser -> markdown -> editable draft.
//
// The PDF parser server URL is configured in SettingsView.
// This component only reads the saved value and passes it to PdfParserView.
export default function UploadView({
  pdfApiBase,
  settings,
  overrides,
  onOpenDraft,
  onUploadMarkdown,
}) {
  const t = useT(STR)
  const mdRef = useRef(null)

  const settingsSource = settings || overrides || {}

  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [markdownName, setMarkdownName] = useState('')
  const [markdownDraft, setMarkdownDraft] = useState(null)
  const [ingestMode, setIngestMode] = useState('')
  const [serverMode, setServerMode] = useState('chunks')
  const [status, setStatus] = useState('')
  const [error, setError] = useState(null)
  const [syncBusy, setSyncBusy] = useState(false)
  const [syncStatus, setSyncStatus] = useState('')
  const [hasDataRoot, setHasDataRoot] = useState(null)

  // What the server would do if this document were uploaded with no choice
  // made. Echoed inside the "follow settings" option so the picker never lies.
  const activeServerMode = settingsSource.ingest_mode || serverMode
  const settingsMode =
    activeServerMode === 'pages'
      ? t.modePages
      : activeServerMode === 'wiki'
        ? t.modeWiki
        : t.modeChunks

  const [parserBase, setParserBase] = useState(() =>
    readParserBase({
      pdfApiBase,
      settingsSource,
    }),
  )

  useEffect(() => {
    setParserBase(
      readParserBase({
        pdfApiBase,
        settingsSource,
      }),
    )
  }, [pdfApiBase, settings, overrides])

  // The ingest mode lives on the server, so ask for it: the parent app keeps no
  // copy of server settings of its own.
  useEffect(() => {
    let alive = true

    api
      .settings()
      .then((server) => {
        if (alive && server?.ingest_mode) {
          setServerMode(
            ['chunks', 'pages', 'wiki'].includes(server.ingest_mode)
              ? server.ingest_mode
              : 'chunks',
          )
        }
        if (alive) setHasDataRoot(Boolean(server?.data_root))
      })
      .catch(() => {
        // Leave the default label alone: the picker still works, it just shows
        // チャンク until the server answers.
      })

    return () => {
      alive = false
    }
  }, [])

  useEffect(() => {
    const syncParserBase = () => {
      setParserBase(
        readParserBase({
          pdfApiBase,
          settingsSource,
        }),
      )
    }

    const onSettingsChanged = (event) => {
      const detail = event?.detail

      if (detail && detail.ingest_mode !== undefined) {
        setServerMode(
          ['chunks', 'pages', 'wiki'].includes(detail.ingest_mode)
            ? detail.ingest_mode
            : 'chunks',
        )
      }

      if (detail && detail[PDF_API_FIELD] !== undefined) {
        setParserBase(clean(detail[PDF_API_FIELD]))
        return
      }

      syncParserBase()
    }

    window.addEventListener('focus', syncParserBase)
    window.addEventListener('llm-wiki-settings-changed', onSettingsChanged)
    document.addEventListener('visibilitychange', syncParserBase)

    return () => {
      window.removeEventListener('focus', syncParserBase)
      window.removeEventListener('llm-wiki-settings-changed', onSettingsChanged)
      document.removeEventListener('visibilitychange', syncParserBase)
    }
  }, [pdfApiBase, settings, overrides])

  const syncRaw = async () => {
    setSyncBusy(true)
    setSyncStatus('')
    setError(null)
    try {
      await api.syncProject()
      setSyncStatus(t.syncStarted)
    } catch (ex) {
      if (ex?.code === 'no_data_root' || ex?.payload?.detail?.code === 'no_data_root') {
        setHasDataRoot(false)
      }
      setError(ex.message || String(ex))
    } finally {
      setSyncBusy(false)
    }
  }

  const pickMd = async (e) => {
    const file = e.target.files?.[0]
    e.target.value = ''

    if (!file) return

    setBusy(true)
    setError(null)
    setStatus(t.readingFile)
    setName(file.name)
    setMarkdownName(file.name)
    setMarkdownDraft(null)

    try {
      const text = await file.text()

      if (!text.trim()) {
        throw new Error(t.fileEmpty)
      }

      setMarkdownDraft({
        originalFilename: file.name,
        markdown: text,
      })

      setStatus(t.readyToUpload)
    } catch (ex) {
      setError(ex.message || String(ex))
      setStatus('')
      setMarkdownDraft(null)
    } finally {
      setBusy(false)
    }
  }

  const uploadMd = async () => {
    if (!markdownDraft) {
      setError(t.markdownRequired)
      setStatus('')
      return
    }

    const nextName = markdownName.trim()

    if (!nextName) {
      setError(t.markdownNameRequired)
      setStatus('')
      return
    }

    setBusy(true)
    setError(null)
    setStatus(t.adding)

    try {
      await onUploadMarkdown?.(
        {
          filename: nextName,
          markdown: markdownDraft.markdown,
          ingestMode: ingestMode || undefined,
        },
        (nextStatus) => setStatus(nextStatus),
      )

      setStatus(t.added)
    } catch (ex) {
      setError(ex.message || String(ex))
      setStatus('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="h-full overflow-auto bg-white">
      <div className="mx-auto max-w-[860px] px-[28px] py-[26px]">
        <section className="mb-[20px] border border-line bg-white p-[18px] shadow-sm">
          <h2 className="m-0 text-[16px] font-extrabold text-ink">
            {t.title}
          </h2>

          <p className="mb-[12px] mt-[4px] text-[12.5px] text-muted">
            {t.hint}
          </p>

          <div className="mb-[14px] flex flex-wrap gap-[10px]">
            <button
              type="button"
              disabled={syncBusy || hasDataRoot === false}
              title={hasDataRoot === false ? t.syncNeedsRoot : undefined}
              onClick={syncRaw}
              className="border border-blue/30 bg-blue px-[15px] py-[9px] text-[13px] font-extrabold text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
            >
              {syncBusy ? t.adding : t.syncRaw}
            </button>

            <a
              href={api.wikiZipUrl()}
              className={`border border-line bg-soft px-[15px] py-[9px] text-[13px] font-extrabold text-ink ${hasDataRoot === false ? 'pointer-events-none opacity-50' : ''}`}
              title={hasDataRoot === false ? t.syncNeedsRoot : undefined}
            >
              {t.downloadWiki}
            </a>
          </div>

          {syncStatus && (
            <p className="mb-[12px] text-[12px] font-semibold text-muted">
              {syncStatus}
            </p>
          )}

          <div className="flex items-center gap-[10px]">
            <input
              ref={mdRef}
              type="file"
              accept=".md,.markdown,text/markdown,text/plain"
              onChange={pickMd}
              className="hidden"
            />

            <button
              type="button"
              disabled={busy}
              onClick={() => mdRef.current?.click()}
              className="border border-blue/30 bg-blue px-[15px] py-[9px] text-[13px] font-extrabold text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy ? t.adding : t.choose}
            </button>

            <div className="min-w-0 flex-1 truncate border border-line bg-soft px-[12px] py-[9px] text-[13px] text-muted">
              {name || t.noFile}
            </div>
          </div>

          {markdownDraft && (
            <div className="mt-[14px]">
              <div className="mb-[12px] flex flex-wrap items-center gap-[10px]">
                <span className="text-[12.5px] font-bold text-ink">
                  {t.ingestMode}
                </span>

                <select
                  value={ingestMode}
                  disabled={busy}
                  onChange={(e) => setIngestMode(e.target.value)}
                  className="border border-line bg-soft px-[10px] py-[7px] text-[13px] text-ink outline-none focus:border-blue/50 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  <option value="">{t.modeDefault(settingsMode)}</option>
                  <option value="chunks">{t.modeChunks}</option>
                  <option value="pages">{t.modePages}</option>
                  <option value="wiki">{t.modeWiki}</option>
                </select>
              </div>

              <p className="mb-[6px] text-[12px] leading-[1.5] text-muted">
                {t.modeHint}
              </p>

              <p className="mb-[14px] text-[12px] leading-[1.5] text-muted">
                {t.modeNote}
              </p>

              <label className="mb-[6px] block text-[12.5px] font-bold text-ink">
                {t.markdownName}
              </label>

              <div className="flex items-center gap-[10px]">
                <input
                  type="text"
                  value={markdownName}
                  disabled={busy}
                  onChange={(e) => {
                    setMarkdownName(e.target.value)
                    setError(null)
                  }}
                  placeholder={t.markdownNamePlaceholder}
                  className="min-w-0 flex-1 border border-line bg-soft px-[12px] py-[9px] text-[13px] text-ink outline-none focus:border-blue/50 disabled:cursor-not-allowed disabled:opacity-60"
                />

                <button
                  type="button"
                  disabled={busy || !markdownDraft}
                  onClick={uploadMd}
                  className="border border-blue/30 bg-blue px-[15px] py-[9px] text-[13px] font-extrabold text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {busy ? t.adding : t.uploadMarkdown}
                </button>
              </div>
            </div>
          )}

          {status && (
            <div className="mt-[10px] border border-line bg-soft px-[10px] py-[8px] text-[12.5px] text-muted">
              {status}
            </div>
          )}

          {error && (
            <div className="mt-[10px] border border-red/25 bg-red/10 px-[10px] py-[8px] text-[12.5px] text-[#7c1230]">
              {error}
            </div>
          )}
        </section>
      </div>

      <PdfParserView apiBase={parserBase} onMarkdownReady={onOpenDraft} />
    </div>
  )
}
