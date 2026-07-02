import { useRef, useState } from 'react'
import { useT } from '../i18n.jsx'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const STR = {
  ja: {
    selectPdf: 'PDF ファイルを選択してください。',
    fillFields: 'base_url、api_key、model を入力してください。',
    uploading: 'アップロード中…',
    convertedFetching: '変換済み。結果を取得中…',
    queued: 'キューに追加されました…',
    queuedWaiting: 'キューで待機中…',
    converting: 'PDF を Markdown に変換中…',
    convertedGetting: '変換完了。Markdown を取得中…',
    statusOf: (s) => `状態: ${s}`,
    opened: 'Markdown を開きました。',
    desc: 'PDF をアップロードして Markdown に変換します。変換が完了すると、自動的に Markdown ビューアで開きます。',
    pdfFile: 'PDF ファイル',
    choosePdf: 'PDF を選択',
    noFile: 'ファイル未選択',
    processing: '処理中…',
    convertBtn: 'PDF を Markdown に変換',
    footer:
      '画像対応モデルであれば画像説明付き Markdown が返ります。画像非対応の場合は、バックエンド側のフォールバック処理により画像を埋め込んだ Markdown が返ります。',
  },
  en: {
    selectPdf: 'Please select a PDF file.',
    fillFields: 'Please fill in base_url, api_key, and model.',
    uploading: 'Uploading…',
    convertedFetching: 'Converted. Fetching result…',
    queued: 'Added to the queue…',
    queuedWaiting: 'Waiting in the queue…',
    converting: 'Converting PDF to Markdown…',
    convertedGetting: 'Conversion complete. Fetching Markdown…',
    statusOf: (s) => `Status: ${s}`,
    opened: 'Opened the Markdown.',
    desc: 'Upload a PDF to convert it to Markdown. When conversion finishes, it opens automatically in the Markdown viewer.',
    pdfFile: 'PDF File',
    choosePdf: 'Choose PDF',
    noFile: 'No file selected',
    processing: 'Processing…',
    convertBtn: 'Convert PDF to Markdown',
    footer:
      'An image-capable model returns Markdown with image descriptions. Without image support, the backend falls back to Markdown with embedded images.',
  },
}

export default function PdfParserView({
  apiBase = '',
  onMarkdownReady,
}) {
  const t = useT(STR)
  const fileRef = useRef(null)

  const [file, setFile] = useState(null)
  const [baseUrl, setBaseUrl] = useState('http://10.160.144.101:51021/v1')
  const [apiKey, setApiKey] = useState('<API_KEY>')
  const [model, setModel] = useState('openai/gpt-oss-120b')

  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const [taskId, setTaskId] = useState(null)
  const [error, setError] = useState(null)

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
      setStatus(uploaded.status === 'completed' ? t.convertedFetching : t.queued)

      if (uploaded.status !== 'completed') {
        while (true) {
          await sleep(1500)

          const statusRes = await fetch(`${apiBase}/status/${encodeURIComponent(id)}`)

          if (!statusRes.ok) {
            const text = await statusRes.text()
            throw new Error(text || `Status check failed: ${statusRes.status}`)
          }

          const s = await statusRes.json()

          if (s.status === 'queued') {
            setStatus(t.queuedWaiting)
          } else if (s.status === 'processing') {
            setStatus(t.converting)
          } else if (s.status === 'completed') {
            setStatus(t.convertedGetting)
            break
          } else if (s.status === 'failed') {
            throw new Error(s.error || 'PDF conversion failed.')
          } else {
            setStatus(t.statusOf(s.status))
          }
        }
      }

      const resultRes = await fetch(`${apiBase}/result/${encodeURIComponent(id)}`)

      if (!resultRes.ok) {
        const text = await resultRes.text()
        throw new Error(text || `Result fetch failed: ${resultRes.status}`)
      }

      const markdown = await resultRes.text()

      setStatus(t.opened)

      onMarkdownReady({
        taskId: id,
        title: file.name,
        filename: file.name,
        markdown,
        sourceType: 'endogenous',
        sourcePath: `pdf-parser:${id}:${file.name}`,
      })
    } catch (e) {
      setError(e.message || String(e))
      setStatus('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="h-full overflow-auto bg-white">
      <div className="mx-auto max-w-[860px] px-[28px] py-[26px]">
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
                label="NVIDIA Invoke URL"
                value={baseUrl}
                onChange={setBaseUrl}
                placeholder="http://10.160.144.101:51021/v1"
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

        <div className="mt-[14px] border border-line bg-soft px-[13px] py-[11px] text-[12px] leading-[1.5] text-muted">
          {t.footer}
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
