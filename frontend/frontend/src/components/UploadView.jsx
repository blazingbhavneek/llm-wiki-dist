import { useRef, useState } from 'react'
import PdfParserView from './PdfParserView'

// "Upload" workspace tab. Two sources of source material:
//  1. Markdown file -> added straight to the graph (endogenous), no editor.
//  2. PDF -> parser -> markdown -> editable draft (via PdfParserView).
export default function UploadView({ pdfApiBase, onOpenDraft, onUploadMarkdown }) {
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
    setStatus('')
    setName(file.name)
    try {
      const text = await file.text()
      if (!text.trim()) throw new Error('File is empty.')
      await onUploadMarkdown({ filename: file.name, markdown: text })
      setStatus('Added to the graph.')
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
        <section className="mb-[20px] rounded-lg border border-line bg-white p-[18px] shadow-sm">
          <h2 className="m-0 text-[16px] font-extrabold text-ink">Upload markdown</h2>
          <p className="mb-[12px] mt-[4px] text-[12.5px] text-muted">
            Add a <code className="rounded bg-soft px-1">.md</code> file straight to the graph as source material.
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
              className="rounded-md border border-blue/30 bg-blue px-[15px] py-[9px] text-[13px] font-extrabold text-white shadow-sm transition hover:brightness-105 disabled:opacity-50"
            >
              {busy ? 'Adding…' : 'Choose markdown'}
            </button>
            <div className="min-w-0 flex-1 truncate rounded-md border border-line bg-soft px-[12px] py-[9px] text-[13px] text-muted">
              {name || 'No file selected'}
            </div>
          </div>

          {busy && (
            <div className="mt-[10px] rounded-md border border-line bg-soft px-[10px] py-[8px] text-[12.5px] text-muted">
              Embedding + linking into the graph… this can take a moment.
            </div>
          )}
          {status && !busy && (
            <div className="mt-[10px] rounded-md border border-green/25 bg-[#f0fdf8] px-[10px] py-[8px] text-[12.5px] text-[#065f46]">
              {status}
            </div>
          )}
          {error && (
            <div className="mt-[10px] rounded-md border border-red/25 bg-red/10 px-[10px] py-[8px] text-[12.5px] text-[#7c1230]">
              {error}
            </div>
          )}
        </section>
      </div>

      <PdfParserView apiBase={pdfApiBase} onMarkdownReady={onOpenDraft} />
    </div>
  )
}
