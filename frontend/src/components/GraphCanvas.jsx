import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useT } from '../i18n.jsx'

const clampZoom = (v) => Math.max(0.05, Math.min(4.5, v))

const STR = {
  ja: {
    canvasTitle: '無限グラフキャンバス',
    canvasDesc:
      'ドラッグで移動、スクロールでズームできます。ノートは点線のトピック領域にグループ化されています。領域を拡大すると内容を読めます。',
    hint: 'ドラッグで移動 · スクロールでズーム · ノートをクリックして開く',
    canvasMap: 'キャンバスマップ',
    notes: '件のノート',
    zoomOut: 'ズームアウト',
    zoomIn: 'ズームイン',
    fitAll: 'グラフ全体を表示',
    resetView: '表示をリセット',
    fitBtn: '全体表示',
    resetBtn: 'リセット',
    legOpen: '開いているノート',
    legSource: 'ソースノート',
    legAgent: 'エージェントノート',
    legStale: '古いノート',
    legConflict: '競合',
  },
  en: {
    canvasTitle: 'Infinite graph canvas',
    canvasDesc:
      'Drag to move, scroll to zoom. Notes are grouped into dotted topic regions. Zoom into a region to read its contents.',
    hint: 'Drag to move · scroll to zoom · click a note to open',
    canvasMap: 'Canvas map',
    notes: 'notes',
    zoomOut: 'Zoom out',
    zoomIn: 'Zoom in',
    fitAll: 'Fit whole graph',
    resetView: 'Reset view',
    fitBtn: 'Fit',
    resetBtn: 'Reset',
    legOpen: 'Open note',
    legSource: 'Source note',
    legAgent: 'Agent note',
    legStale: 'Stale note',
    legConflict: 'Conflict',
  },
}

function hashUnit(str) {
  let h = 2166136261
  for (let i = 0; i < str.length; i += 1) {
    h ^= str.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return (h >>> 0) / 4294967295
}

function curvedEdgePath(a, b, edge) {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const dist = Math.max(1, Math.hypot(dx, dy))
  const offset = Math.min(54, Math.max(10, dist * 0.055))
  const side = hashUnit(`${edge.a}:${edge.b}`) > 0.5 ? 1 : -1
  const cx = (a.x + b.x) / 2 - (dy / dist) * offset * side
  const cy = (a.y + b.y) / 2 + (dx / dist) * offset * side
  return `M ${a.x} ${a.y} Q ${cx} ${cy} ${b.x} ${b.y}`
}

export default function GraphCanvas({
  nodes,
  edges,
  clusters,
  worldW = 1200,
  worldH = 760,
  openIds,
  answerIds,
  showConflict,
  showStale,
  onOpenNode,
}) {
  const svgRef = useRef(null)
  const wrapRef = useRef(null)
  const drag = useRef({ active: false, lastX: 0, lastY: 0 })
  const t = useT(STR)
  const [view, setView] = useState({ scale: 1, panX: 0, panY: 0 })
  const [hover, setHover] = useState(null)

  const nodeById = useMemo(() => {
    const m = new Map()
    nodes.forEach((n) => m.set(n.id, n))
    return m
  }, [nodes])

  const clientToSvg = useCallback((clientX, clientY) => {
    const svg = svgRef.current
    if (!svg) return { x: clientX, y: clientY }
    const pt = svg.createSVGPoint()
    pt.x = clientX
    pt.y = clientY
    const m = svg.getScreenCTM()
    return m ? pt.matrixTransform(m.inverse()) : { x: clientX, y: clientY }
  }, [])

  const zoomAt = useCallback((svgX, svgY, factor) => {
    setView((v) => {
      const before = { x: (svgX - v.panX) / v.scale, y: (svgY - v.panY) / v.scale }
      const scale = clampZoom(v.scale * factor)
      return { scale, panX: svgX - before.x * scale, panY: svgY - before.y * scale }
    })
  }, [])

  // ワールド全体を表示中の viewBox に収める
  const fit = useCallback(() => {
    setView({ scale: 0.92, panX: worldW * 0.04, panY: worldH * 0.04 })
  }, [worldW, worldH])

  const reset = useCallback(() => setView({ scale: 1, panX: 0, panY: 0 }), [])

  // ワールドサイズが変更されたとき、新しいデータに合わせて再フィットする
  useEffect(() => {
    fit()
  }, [fit])

  useEffect(() => {
    const svg = svgRef.current
    if (!svg) return
    const onWheel = (e) => {
      e.preventDefault()
      const p = clientToSvg(e.clientX, e.clientY)
      zoomAt(p.x, p.y, e.deltaY < 0 ? 1.12 : 0.88)
    }
    svg.addEventListener('wheel', onWheel, { passive: false })
    return () => svg.removeEventListener('wheel', onWheel)
  }, [clientToSvg, zoomAt])

  const onPointerDown = (e) => {
    if (e.target.dataset && e.target.dataset.node) return
    drag.current = { active: true, lastX: e.clientX, lastY: e.clientY }
    svgRef.current.setPointerCapture(e.pointerId)
  }

  const onPointerMove = (e) => {
    if (!drag.current.active) return
    const p0 = clientToSvg(drag.current.lastX, drag.current.lastY)
    const p1 = clientToSvg(e.clientX, e.clientY)
    drag.current.lastX = e.clientX
    drag.current.lastY = e.clientY
    setView((v) => ({ ...v, panX: v.panX + (p1.x - p0.x), panY: v.panY + (p1.y - p0.y) }))
  }

  const endDrag = (e) => {
    drag.current.active = false
    try {
      svgRef.current.releasePointerCapture(e.pointerId)
    } catch {
      /* 何もしない */
    }
  }

  const moveHover = (e, n) => {
    const rect = wrapRef.current.getBoundingClientRect()
    setHover({ x: e.clientX - rect.left + 16, y: e.clientY - rect.top + 16, title: n.title, summary: n.summary })
  }

  const vp = {
    x: -view.panX / view.scale,
    y: -view.panY / view.scale,
    w: worldW / view.scale,
    h: worldH / view.scale,
  }

  const onMiniClick = (e) => {
    const mini = e.currentTarget
    const pt = mini.createSVGPoint()
    pt.x = e.clientX
    pt.y = e.clientY
    const m = mini.getScreenCTM()
    const p = m ? pt.matrixTransform(m.inverse()) : { x: worldW / 2, y: worldH / 2 }
    setView((v) => ({ ...v, panX: worldW / 2 - p.x * v.scale, panY: worldH / 2 - p.y * v.scale }))
  }

  const isOpen = (id) => openIds && openIds.has(id)
  const isCited = (id) => answerIds && answerIds.has(id)
  const transform = `translate(${view.panX} ${view.panY}) scale(${view.scale})`
  const gridSize = 42 * view.scale

  const nodeClass = (n) => {
    // Highlight ONLY nodes that are open in a tab (current) or cited by the
    // current answer (used). Never dim the rest.
    const cls = ['gnode', n.type]
    if (n.stale && showStale) cls.push('stale')
    if (n.conflict && showConflict) cls.push('conflict')
    if (isCited(n.id)) cls.push('used')
    if (isOpen(n.id)) cls.push('current')
    return cls.join(' ')
  }

  const nodeRadius = (n) =>
    isOpen(n.id) ? 15 : isCited(n.id) ? 11 : n.type === 'agent' ? 8.5 : 7.5

  return (
    <div
      ref={wrapRef}
      className="graph-grid relative h-full overflow-hidden"
      style={{
        backgroundSize: `${gridSize}px ${gridSize}px`,
        backgroundPosition: `${view.panX}px ${view.panY}px`,
      }}
    >
      <div className="absolute left-[18px] top-[18px] z-[3] w-[330px] border border-line bg-white/90 px-[14px] py-[13px] shadow-md backdrop-blur">
        <h2 className="m-0 text-[14px] font-bold tracking-tight">{t.canvasTitle}</h2>
        <p className="mt-[5px] text-[12px] leading-[1.4] text-muted">
          {t.canvasDesc}
        </p>
      </div>

      <svg
        ref={svgRef}
        className={`graph-svg block h-full w-full${drag.current.active ? ' panning' : ''}`}
        viewBox={`0 0 ${worldW} ${worldH}`}
        preserveAspectRatio="xMidYMid meet"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerLeave={endDrag}
      >
        <g transform={transform}>
          <g>
            {clusters.map((c) => (
              <g key={c.id}>
                <ellipse
                  className="cluster-ring"
                  cx={c.cx}
                  cy={c.cy}
                  rx={c.rx}
                  ry={c.ry}
                  stroke={c.color}
                  fill={c.color + '10'}
                />
                <text className="cluster-label" x={c.cx} y={c.cy - c.ry - 8} textAnchor="middle">
                  {c.label}
                </text>
              </g>
            ))}
          </g>

          <g>
            {edges.map((edge, i) => {
              const a = nodeById.get(edge.a)
              const b = nodeById.get(edge.b)
              if (!a || !b) return null
              const cls = ['edge']
              if (edge.conflict && showConflict) cls.push('conflict')
              if (edge.stale && showStale) cls.push('stale')
              if (edge.support) cls.push('support')
              return <path key={i} d={curvedEdgePath(a, b, edge)} className={cls.join(' ')} />
            })}
          </g>

          <g>
            {nodes.map((n) => (
              <circle
                key={n.id}
                data-node={n.id}
                cx={n.x}
                cy={n.y}
                r={nodeRadius(n)}
                className={nodeClass(n)}
                onPointerEnter={(e) => moveHover(e, n)}
                onPointerMove={(e) => moveHover(e, n)}
                onPointerLeave={() => setHover(null)}
                onClick={(e) => {
                  e.stopPropagation()
                  onOpenNode(n)
                }}
              />
            ))}
          </g>
        </g>
      </svg>

      {hover && (
        <div
          className="pointer-events-none absolute z-[5] w-[250px] border border-line bg-white/95 p-[12px] shadow-md backdrop-blur"
          style={{ left: hover.x, top: hover.y }}
        >
          <strong className="mb-[4px] block text-[13px]">{hover.title}</strong>
          <p className="m-0 text-[12px] leading-[1.4] text-muted">{hover.summary}</p>
        </div>
      )}

      <div className="absolute left-[18px] top-[116px] z-[4] border border-line bg-white/90 px-[10px] py-[8px] text-[12px] text-muted shadow-md backdrop-blur">
        {t.hint}
      </div>

      <div className="absolute right-[18px] top-[18px] z-[4] w-[190px] border border-line bg-white/90 p-[10px] shadow-md backdrop-blur">
        <div className="mb-[7px] flex justify-between gap-2 text-[11px] font-extrabold uppercase tracking-wider text-muted">
          <span>{t.canvasMap}</span>
          <span>{nodes.length} {t.notes}</span>
        </div>

        <svg
          className="block h-[120px] w-full border border-line bg-soft"
          viewBox={`0 0 ${worldW} ${worldH}`}
          preserveAspectRatio="xMidYMid meet"
          onClick={onMiniClick}
        >
          {clusters.map((c) => (
            <ellipse
              key={c.id}
              cx={c.cx}
              cy={c.cy}
              rx={c.rx}
              ry={c.ry}
              fill={c.color + '14'}
              stroke={c.color}
              strokeWidth={Math.max(6, worldW / 220)}
              opacity={0.6}
            />
          ))}

          {nodes.map((n) => (
            <circle
              key={n.id}
              cx={n.x}
              cy={n.y}
              r={isOpen(n.id) ? worldW / 110 : isCited(n.id) ? worldW / 170 : worldW / 280}
              className={`mini-node ${n.type === 'agent' ? 'agent' : ''} ${isOpen(n.id) ? 'current' : isCited(n.id) ? 'used' : ''}`}
            />
          ))}

          <rect className="mini-viewport" x={vp.x} y={vp.y} width={vp.w} height={vp.h} />
        </svg>
      </div>

      <div className="absolute bottom-[18px] right-[18px] z-[4] flex items-center gap-[7px] border border-line bg-white/90 p-[8px] shadow-md backdrop-blur">
        <button
          className="h-[32px] min-w-[34px] border border-line bg-white text-[12px] font-extrabold text-slate-700"
          onClick={() => zoomAt(worldW / 2, worldH / 2, 0.82)}
          title={t.zoomOut}
        >
          −
        </button>

        <span className="min-w-[54px] text-center text-[12px] font-extrabold text-muted">
          {Math.round(view.scale * 100)}%
        </span>

        <button
          className="h-[32px] min-w-[34px] border border-line bg-white text-[12px] font-extrabold text-slate-700"
          onClick={() => zoomAt(worldW / 2, worldH / 2, 1.18)}
          title={t.zoomIn}
        >
          +
        </button>

        <button
          className="h-[32px] min-w-[34px] border border-line bg-white text-[12px] font-extrabold text-slate-700"
          onClick={fit}
          title={t.fitAll}
        >
          {t.fitBtn}
        </button>

        <button
          className="h-[32px] min-w-[34px] border border-line bg-white text-[12px] font-extrabold text-slate-700"
          onClick={reset}
          title={t.resetView}
        >
          {t.resetBtn}
        </button>
      </div>

      <div className="absolute bottom-[18px] left-[18px] z-[3] flex max-w-[390px] flex-wrap gap-[8px] border border-line bg-white/90 p-[10px] shadow-md backdrop-blur">
        <LegendDot
          label={t.legOpen}
          style={{ width: 10, height: 10, background: '#111827', boxShadow: '0 0 0 6px rgba(57,119,246,.13)' }}
        />
        <LegendDot label={t.legSource} style={{ background: 'var(--color-blue)' }} />
        <LegendDot label={t.legAgent} style={{ background: 'var(--color-orange)' }} />
        <LegendDot label={t.legStale} style={{ background: '#b6c0ce' }} />
        <LegendDot label={t.legConflict} style={{ background: 'var(--color-red)' }} />
      </div>
    </div>
  )
}

function LegendDot({ label, style }) {
  return (
    <span className="inline-flex items-center gap-[7px] text-[12px] text-muted">
      <span className="inline-block h-[8px] w-[8px]" style={style} />
      {label}
    </span>
  )
}
