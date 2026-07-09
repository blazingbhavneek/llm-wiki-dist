import { useEffect, useRef, useState } from 'react'
import PdfParserView from './PdfParserView'
import { useT } from '../i18n.jsx'

const PDF_API_COOKIE = 'pdf_parser_api_base'

function getCookie(name) {
  const prefix = `${name}=`
  const row = document.cookie
    .split('; ')
    .find((item) => item.startsWith(prefix))

  if (!row) return null
  return decodeURIComponent(row.slice(prefix.length))
}

function setCookie(name, value, days = 365) {
  const maxAge = days * 24 * 60 * 60
  document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=${maxAge}; SameSite=Lax`
}

function joinUrl(base, path) {
  const cleanBase = String(base || '').trim().replace(/\/+$/, '')
  const cleanPath = path.startsWith('/') ? path : `/${path}`
  return cleanBase ? `${cleanBase}${cleanPath}` : cleanPath
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

    pdfParserTitle: 'PDF パーサー URL',
    pdfParserHint:
      '独自の PDF パーサーサーバーを使う場合は、ここで URL を設定できます。',
    pdfParserLabel: 'PDF パーサーサーバー',
    pdfParserPlaceholder: '例: http://localhost:51024',
    checkAndSaveParserUrl: '確認して保存',
    checkingParserUrl: '確認中...',
    parserUrlSaved: 'PDF パーサー URL を確認して保存しました。',
    parserUrlRequired: 'PDF パーサー URL を入力してください。',
    parserUrlCheckFailed:
      'PDF パーサーサーバーが起動していない、または /queue に接続できません。URL は保存されませんでした。',
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

    pdfParserTitle: 'PDF parser URL',
    pdfParserHint: 'Set this if you want to use your own PDF parser server.',
    pdfParserLabel: 'PDF parser server',
    pdfParserPlaceholder: 'e.g. http://localhost:51024',
    checkAndSaveParserUrl: 'Check and save',
    checkingParserUrl: 'Checking...',
    parserUrlSaved: 'PDF parser URL checked and saved.',
    parserUrlRequired: 'Please enter a PDF parser URL.',
    parserUrlCheckFailed:
      'PDF parser server is not running or /queue is unreachable. URL was not saved.',
  },
}

// "Upload" workspace tab. Two sources of source material:
//  1. Markdown file -> added straight to the graph as endogenous source.
//  2. PDF -> parser -> markdown -> editable draft.
export default function UploadView({ pdfApiBase, onOpenDraft, onUploadMarkdown }) {
  const t = useT(STR)
  const mdRef = useRef(null)

  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [status, setStatus] = useState('')
  const [error, setError] = useState(null)

  const [parserBase, setParserBase] = useState(() => {
    const saved = getCookie(PDF_API_COOKIE)
    return saved !== null ? saved : pdfApiBase || ''
  })

  const [parserInput, setParserInput] = useState(() => {
    const saved = getCookie(PDF_API_COOKIE)
    return saved !== null ? saved : pdfApiBase || ''
  })

  const [parserChecking, setParserChecking] = useState(false)
  const [parserStatus, setParserStatus] = useState('')
  const [parserError, setParserError] = useState('')

  useEffect(() => {
    const saved = getCookie(PDF_API_COOKIE)

    // Cookie has priority over parent prop.
    if (saved !== null) {
      setParserBase(saved)
      setParserInput(saved)
      return
    }

    setParserBase(pdfApiBase || '')
    setParserInput(pdfApiBase || '')
  }, [pdfApiBase])

  const checkAndSaveParserBase = async () => {
    const next = parserInput.trim()

    if (!next) {
      setParserError(t.parserUrlRequired)
      setParserStatus('')
      return
    }

    setParserChecking(true)
    setParserError('')
    setParserStatus('')

    try {
      const queueUrl = joinUrl(next, '/queue')

      const res = await fetch(queueUrl, {
        method: 'GET',
        headers: {
          Accept: 'application/json',
        },
      })

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`)
      }

      // Make sure it is actually returning JSON from the parser server.
      await res.json()

      setCookie(PDF_API_COOKIE, next)
      setParserBase(next)
      setParserStatus(t.parserUrlSaved)
    } catch {
      setParserError(t.parserUrlCheckFailed)
      setParserStatus('')
    } finally {
      setParserChecking(false)
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

    try {
      const text = await file.text()
      if (!text.trim()) throw new Error(t.fileEmpty)

      await onUploadMarkdown?.(
        { filename: file.name, markdown: text },
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

        <section className="mb-[20px] border border-line bg-white p-[18px] shadow-sm">
          <h2 className="m-0 text-[16px] font-extrabold text-ink">
            {t.pdfParserTitle}
          </h2>

          <p className="mb-[12px] mt-[4px] text-[12.5px] text-muted">
            {t.pdfParserHint}
          </p>

          <label className="mb-[6px] block text-[12.5px] font-bold text-ink">
            {t.pdfParserLabel}
          </label>

          <div className="flex items-center gap-[10px]">
            <input
              type="text"
              value={parserInput}
              disabled={parserChecking}
              onChange={(e) => {
                setParserInput(e.target.value)
                setParserStatus('')
                setParserError('')
              }}
              placeholder={t.pdfParserPlaceholder}
              className="min-w-0 flex-1 border border-line bg-soft px-[12px] py-[9px] text-[13px] text-ink outline-none focus:border-blue/50 disabled:cursor-not-allowed disabled:opacity-60"
            />

            <button
              type="button"
              disabled={parserChecking}
              onClick={checkAndSaveParserBase}
              className="border border-blue/30 bg-blue px-[15px] py-[9px] text-[13px] font-extrabold text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
            >
              {parserChecking ? t.checkingParserUrl : t.checkAndSaveParserUrl}
            </button>
          </div>

          {parserStatus && (
            <div className="mt-[10px] border border-line bg-soft px-[10px] py-[8px] text-[12.5px] text-muted">
              {parserStatus}
            </div>
          )}

          {parserError && (
            <div className="mt-[10px] border border-red/25 bg-red/10 px-[10px] py-[8px] text-[12.5px] text-[#7c1230]">
              {parserError}
            </div>
          )}
        </section>
      </div>

      <PdfParserView apiBase={parserBase} onMarkdownReady={onOpenDraft} />
    </div>
  )
}
