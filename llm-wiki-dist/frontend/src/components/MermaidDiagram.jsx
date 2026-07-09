import { useEffect, useRef, useState } from 'react'
import mermaid from 'mermaid'
import { useT } from '../i18n.jsx'

mermaid.initialize({ startOnLoad: false, theme: 'default', securityLevel: 'strict' })

const STR = {
  ja: { building: '図を作成中…', rendering: '図をレンダリング中…' },
  en: { building: 'Building diagram…', rendering: 'Rendering diagram…' },
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))

/**
 * パンとズームに対応した Mermaid 図をレンダーします（グラフキャンバスのように操作できます）。
 * `state`: 'pending' の場合はローダーを表示し、'failed' の場合は元のコードを表示します。
 * それ以外の場合は図をレンダーします。
 */
export default function MermaidDiagram({ code, state }) {
  const t = useT(STR)
  const [svg, setSvg] = useState('')
  const [failed, setFailed] = useState(false)
  const [view, setView] = useState({ scale: 1, x: 0, y: 0 })
  const frameRef = useRef(null)
  const dragRef = useRef(null)

  useEffect(() => {
    if (state === 'pending') return
    let alive = true
    const id = 'mmd-' + Math.random().toString(36).slice(2)
    mermaid
      .render(id, code)
      .then((res) => alive && setSvg(res.svg))
      .catch(() => alive && setFailed(true))
    return () => {
      alive = false
    }
  }, [code, state])

  // スクロールではなくズームできるように preventDefault を呼ぶため、
  // passive ではない wheel リスナーを使用します。
  useEffect(() => {
    const el = frameRef.current
    if (!el) return
    const onWheel = (e) => {
      e.preventDefault()
      const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12
      setView((v) => ({ ...v, scale: clamp(v.scale * factor, 0.3, 6) }))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [svg])

  const onPointerDown = (e) => {
    dragRef.current = { sx: e.clientX, sy: e.clientY, ox: view.x, oy: view.y }
    e.currentTarget.setPointerCapture?.(e.pointerId)
  }
  const onPointerMove = (e) => {
    const d = dragRef.current
    if (!d) return
    setView((v) => ({ ...v, x: d.ox + (e.clientX - d.sx), y: d.oy + (e.clientY - d.sy) }))
  }
  const onPointerUp = () => {
    dragRef.current = null
  }
  const reset = () => setView({ scale: 1, x: 0, y: 0 })
  const zoom = (factor) => setView((v) => ({ ...v, scale: clamp(v.scale * factor, 0.3, 6) }))

  if (state === 'pending') {
    return (
      <div className="my-[12px] flex items-center gap-2 border border-line bg-soft px-[14px] py-[16px] text-[13px] text-muted">
        <Dots /> {t.building}
      </div>
    )
  }

  if (state === 'failed' || failed) {
    return (
      <pre className="my-[12px] overflow-x-auto rounded-lg border border-line bg-[#0f172a] p-4 text-sm text-white">
        <code>{code}</code>
      </pre>
    )
  }

  if (!svg) {
    return (
      <div className="my-[12px] flex items-center gap-2 border border-line bg-soft px-[14px] py-[16px] text-[13px] text-muted">
        <Dots /> {t.rendering}
      </div>
    )
  }

  return (
    <div className="relative my-[14px] overflow-hidden rounded-lg border border-line bg-white">
      <div className="absolute right-2 top-2 z-10 flex gap-1">
        <CtrlBtn onClick={() => zoom(1.2)}>+</CtrlBtn>
        <CtrlBtn onClick={() => zoom(1 / 1.2)}>−</CtrlBtn>
        <CtrlBtn onClick={reset}>⤢</CtrlBtn>
      </div>
      <div
        ref={frameRef}
        className="h-[420px] cursor-grab touch-none select-none active:cursor-grabbing"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onDoubleClick={reset}
      >
        <div
          className="grid h-full place-items-center [&_svg]:max-w-none"
          style={{
            transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
            transformOrigin: 'center center',
          }}
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      </div>
    </div>
  )
}

function CtrlBtn({ children, onClick }) {
  return (
    <button
      className="grid h-[26px] w-[26px] place-items-center border border-line bg-white/90 text-[14px] font-bold text-muted shadow-sm hover:text-ink"
      onClick={onClick}
      type="button"
    >
      {children}
    </button>
  )
}

function Dots() {
  const frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
  const [i, setI] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setI((v) => (v + 1) % frames.length), 80)
    return () => clearInterval(id)
  }, [])
  return <span className="font-mono text-blue">{frames[i]}</span>
}
