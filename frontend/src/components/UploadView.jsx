import { useRef, useState } from 'react'
import PdfParserView from './PdfParserView'
import { useT } from '../i18n.jsx'

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
          <h2 className="m-0 text-[16px] font-extrabold text-ink">{t.title}</h2>
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
      </div>

      <PdfParserView apiBase={pdfApiBase} onMarkdownReady={onOpenDraft} />
    </div>
  )
}
