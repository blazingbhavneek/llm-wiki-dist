import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import Lenis from 'lenis'
import { marked } from 'marked'
import { URL_PREFIX } from './url-prefix.js'

marked.setOptions({ gfm: true, breaks: true })

const IMAGE_UNIT_RE = /<image-unit\b[^>]*>([\s\S]*?)<\/image-unit>/gi
const IMAGE_SRC_RE = /<img\b[^>]*\bsrc=["']([^"']+)["']/i
const IMAGE_DESC_RE =
  /<image-description\b[^>]*>([\s\S]*?)<\/image-description>/i

/** Shorten base64 payloads so the raw view stays scrollable. */
function truncateBase64(markdown) {
  return markdown.replace(/base64,[A-Za-z0-9+/=]{80,}/g, (m) => {
    const body = m.slice('base64,'.length)
    return `base64,${body.slice(0, 60)}…[${body.length} chars]`
  })
}

const escapeHtml = (s) =>
  s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c])

/** Turn custom <image-unit> blocks into styled HTML the renderer can show. */
function preprocess(markdown) {
  return markdown.replace(IMAGE_UNIT_RE, (_m, body) => {
    const src = body.match(IMAGE_SRC_RE)?.[1] ?? ''
    const desc = (body.match(IMAGE_DESC_RE)?.[1] ?? '').trim()
    return [
      '<figure class="img-unit">',
      src ? `<img src="${src}" alt="" loading="lazy" />` : '',
      desc
        ? `<figcaption>${escapeHtml(desc).replace(/\n/g, '<br />')}</figcaption>`
        : '',
      '</figure>',
    ].join('\n')
  })
}

export default function App() {
  const { t, i18n } = useTranslation()
  const [file, setFile] = useState(null)
  const [includeImages, setIncludeImages] = useState(true)
  const [dragging, setDragging] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)
  const [view, setView] = useState('rendered') // 'rendered' | 'raw'

  const inputRef = useRef(null)
  const outerLenis = useRef(null)
  const innerLenis = useRef(null)
  const previewRef = useRef(null)

  // Smooth page scroll (outside).
  useEffect(() => {
    const lenis = new Lenis({ lerp: 0.12, smoothWheel: true })
    outerLenis.current = lenis
    let raf = requestAnimationFrame(function loop(t) {
      lenis.raf(t)
      raf = requestAnimationFrame(loop)
    })
    return () => {
      cancelAnimationFrame(raf)
      lenis.destroy()
      outerLenis.current = null
    }
  }, [])

  // Smooth scroll inside the markdown preview div.
  useEffect(() => {
    const el = previewRef.current
    if (!el) return
    const lenis = new Lenis({ wrapper: el, content: el, lerp: 0.12 })
    innerLenis.current = lenis
    let raf = requestAnimationFrame(function loop(t) {
      lenis.raf(t)
      raf = requestAnimationFrame(loop)
    })
    return () => {
      cancelAnimationFrame(raf)
      lenis.destroy()
      innerLenis.current = null
    }
  }, [result])

  const html = useMemo(
    () => (result ? marked.parse(preprocess(result.markdown)) : ''),
    [result],
  )

  async function parse(f) {
    if (!f || loading) return
    setLoading(true)
    setError(null)
    try {
      const form = new FormData()
      form.append('file', f)
      // Generic route: ordinary Markdown data-URL images, never an LLM call.
      const qs = new URLSearchParams({ images: String(includeImages) })
      const res = await fetch(`${URL_PREFIX}/parse?${qs.toString()}`, {
        method: 'POST',
        body: form,
      })
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`
        try {
          detail = (await res.json()).detail ?? detail
        } catch {
          /* non-JSON error body */
        }
        throw new Error(detail)
      }
      const data = await res.json()
      if (data.error) throw new Error(data.error)
      setResult(data)
      requestAnimationFrame(() =>
        innerLenis.current?.scrollTo(0, { immediate: true }),
      )
    } catch (err) {
      setError(err.message)
      setResult(null)
    } finally {
      setLoading(false)
    }
  }

  function download() {
    if (!result) return
    const blob = new Blob([result.markdown], {
      type: 'text/markdown;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = (file?.name ?? 'document').replace(/\.[^.]+$/, '') + '.md'
    a.click()
    URL.revokeObjectURL(url)
  }

  function pick(f) {
    if (!f) return
    setFile(f)
    setResult(null)
    setError(null)
  }

  return (
    <div className="mx-auto flex min-h-screen w-[90vw] flex-col py-10">
      <header className="mb-8 flex items-start justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">{t('appTitle')}</h1>
          <p className="mt-1 text-sm text-neutral-500">{t('tagline')}</p>
        </div>
        <div className="flex overflow-hidden rounded-sm border border-neutral-300 text-xs">
          {['ja', 'en'].map((lng) => (
            <button
              key={lng}
              type="button"
              onClick={() => i18n.changeLanguage(lng)}
              className={`px-3 py-1.5 transition-colors duration-150 active:scale-[0.95] ${
                i18n.language === lng
                  ? 'bg-neutral-900 text-white'
                  : 'bg-white text-neutral-500 hover:bg-neutral-100'
              }`}
            >
              {lng === 'ja' ? '日本語' : 'English'}
            </button>
          ))}
        </div>
      </header>

      <main className="flex flex-col gap-6">
        {/* ------------------------------------------------ upload zone */}
        <section
          onDragOver={(e) => {
            e.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDragging(false)
            pick(e.dataTransfer.files?.[0])
          }}
          onClick={() => inputRef.current?.click()}
          className={`cursor-pointer rounded-sm border border-dashed px-6 py-10 text-center transition-all duration-150 active:scale-[0.99] ${
            dragging
              ? 'border-neutral-900 bg-neutral-50'
              : 'border-neutral-300 hover:border-neutral-900'
          }`}
        >
          <input
            ref={inputRef}
            type="file"
            hidden
            accept=".pdf,.docx,.pptx,.xlsx,.csv"
            onChange={(e) => pick(e.target.files?.[0])}
          />
          <p className="text-sm">
            {file ? t('dropHintFile', { name: file.name }) : t('dropHint')}
          </p>
          <p className="mt-1 text-xs text-neutral-400">{t('formats')}</p>
        </section>

        {/* ------------------------------------------------ controls */}
        <section className="flex flex-wrap items-center gap-x-6 gap-y-3 text-sm">
          <label className="flex cursor-pointer select-none items-center gap-2">
            <input
              type="checkbox"
              checked={includeImages}
              onChange={(e) => setIncludeImages(e.target.checked)}
              className="h-4 w-4 accent-neutral-900"
            />
            {t('includeImages')}
          </label>

          <div className="ml-auto flex gap-2">
            <button
              type="button"
              disabled={!file || loading}
              onClick={() => parse(file)}
              className="rounded-sm border border-neutral-900 bg-white px-5 py-2 transition-all duration-150 hover:bg-neutral-900 hover:text-white active:scale-[0.96] disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-white disabled:hover:text-current"
            >
              {loading ? t('parsing') : result ? t('reparse') : t('parse')}
            </button>
            <button
              type="button"
              disabled={!result}
              onClick={download}
              className="rounded-sm border border-neutral-900 bg-neutral-900 px-5 py-2 text-white transition-all duration-150 hover:bg-white hover:text-neutral-900 active:scale-[0.96] disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-neutral-900 disabled:hover:text-white"
            >
              {t('download')}
            </button>
          </div>
        </section>

        {error && (
          <p className="rounded-sm border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {t('error', { message: error })}
          </p>
        )}

        {/* ------------------------------------------------ preview */}
        {result && !loading && (
          <div className="flex justify-end">
            <div className="flex overflow-hidden rounded-sm border border-neutral-300 text-xs">
              {[
                ['rendered', t('viewRendered')],
                ['raw', t('viewRaw')],
              ].map(([v, label]) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => setView(v)}
                  className={`px-3 py-1.5 transition-colors duration-150 active:scale-[0.95] ${
                    view === v
                      ? 'bg-neutral-900 text-white'
                      : 'bg-white text-neutral-500 hover:bg-neutral-100'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
        )}
        <section
          ref={previewRef}
          onMouseEnter={() => outerLenis.current?.stop()}
          onMouseLeave={() => outerLenis.current?.start()}
          className="preview-scroll h-[60vh] rounded-sm border border-neutral-200 bg-white"
        >
          {loading && (
            <div className="flex h-full items-center justify-center text-sm text-neutral-400">
              <span className="animate-pulse">{t('parsingFile')}</span>
            </div>
          )}
          {!loading && result && view === 'rendered' && (
            <article
              className="markdown-body px-6 py-5"
              dangerouslySetInnerHTML={{ __html: html }}
            />
          )}
          {!loading && result && view === 'raw' && (
            <pre className="whitespace-pre-wrap px-6 py-5 font-mono text-xs leading-relaxed text-neutral-800">
              {truncateBase64(result.markdown)}
            </pre>
          )}
          {!loading && !result && !error && (
            <div className="flex h-full items-center justify-center text-sm text-neutral-400">
              {t('previewEmpty')}
            </div>
          )}
          {!loading && !result && error && (
            <div className="flex h-full items-center justify-center text-sm text-neutral-400">
              {t('noResult')}
            </div>
          )}
        </section>

        {result && (
          <footer className="flex gap-4 text-xs text-neutral-400">
            <span>{t('parser', { name: result.parser })}</span>
            <span>{t('images', { count: result.image_count })}</span>
            <span>{t('duration', { seconds: result.duration_s?.toFixed(2) })}</span>
          </footer>
        )}
      </main>
    </div>
  )
}
