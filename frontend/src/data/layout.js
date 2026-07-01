// Backend nodes have no coordinates. Compute a deterministic layout grouped by
// COARSE topic (chapter), so 900+ fine-grained headings don't each become their
// own 1-node cell. World size grows with the number of groups so nodes spread out
// and stay readable. Maps backend fields -> render flags.

const PALETTE = ['#3977f6', '#f59e0b', '#8b5cf6', '#10b981', '#ef476f', '#0ea5e9', '#ec4899', '#14b8a6', '#a855f7', '#f97316']

function hash(str) {
  let h = 2166136261
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return (h >>> 0) / 4294967295
}
function gaussian(seed) {
  const u = Math.max(hash(seed + 'u'), 0.001)
  const v = Math.max(hash(seed + 'v'), 0.001)
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v)
}

const STALE_STATUS = new Set(['stale', 'superseded', 'deleted'])
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5))

function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v))
}

function lerp(a, b, t) {
  return a + (b - a) * t
}

// Collapse a fine cluster label ("24.3.2.4.7 Memcpy…", "Chapter 26. …") into a
// coarse chapter bucket. Falls back to the raw label / "Misc".
function coarseKey(label) {
  if (!label) return 'Misc'
  const m = label.match(/^(?:Chapter\s+)?(\d+)/i)
  if (m) return `Ch ${m[1]}`
  // short topical labels (e.g. "Managed", "Shared Memory") keep their own bucket
  if (label.length <= 22) return label
  return 'Misc'
}

function chapterOrder(key) {
  const m = key.match(/^Ch (\d+)$/)
  return m ? parseInt(m[1], 10) : 9999
}

export function layoutGraph(rawNodes, rawEdges) {
  const conflictNodes = new Set()
  const nodeStats = new Map(rawNodes.map((n) => [n.id, { degree: 0, in: 0, out: 0 }]))

  rawEdges.forEach((e) => {
    if (e.label === 'contradicts') {
      conflictNodes.add(e.source_node_id)
      conflictNodes.add(e.target_node_id)
    }

    const source = nodeStats.get(e.source_node_id)
    const target = nodeStats.get(e.target_node_id)
    if (source) {
      source.degree += 1
      source.out += 1
    }
    if (target) {
      target.degree += 1
      target.in += 1
    }
  })

  const maxDegree = Math.max(...[...nodeStats.values()].map((s) => s.degree), 1)

  // AUTO: if the backend gave meaningful clusters (e.g. after recluster()), group
  // by node.cluster directly. Only when clusters are mostly singletons (a weak
  // model left every heading its own cluster) do we collapse by chapter number.
  const distinct = new Set(rawNodes.map((n) => n.cluster || 'Misc')).size
  const mostlySingletons = rawNodes.length > 0 && distinct > rawNodes.length * 0.4
  const keyFn = mostlySingletons ? (n) => coarseKey(n.cluster) : (n) => n.cluster || 'Misc'

  const groups = new Map()
  rawNodes.forEach((n) => {
    const key = keyFn(n)
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(n)
  })

  // order: numbered chapters first, then alpha
  const labels = [...groups.keys()].sort((a, b) => {
    const oa = chapterOrder(a)
    const ob = chapterOrder(b)
    return oa !== ob ? oa - ob : a.localeCompare(b)
  })

  const groupStats = new Map()
  labels.forEach((label) => {
    const members = groups.get(label)
    let degreeSum = 0
    let maxGroupDegree = 0
    let leafCount = 0
    members.forEach((n) => {
      const degree = nodeStats.get(n.id)?.degree ?? 0
      degreeSum += degree
      maxGroupDegree = Math.max(maxGroupDegree, degree)
      if (degree <= 1) leafCount += 1
    })
    groupStats.set(label, {
      size: members.length,
      degreeSum,
      maxDegree: maxGroupDegree,
      leafCount,
      score: maxGroupDegree * 1.7 + degreeSum / Math.max(1, members.length),
    })
  })

  const placementLabels = [...labels].sort((a, b) => {
    const sa = groupStats.get(a)
    const sb = groupStats.get(b)
    if (sb.score !== sa.score) return sb.score - sa.score
    if (sb.size !== sa.size) return sb.size - sa.size
    const oa = chapterOrder(a)
    const ob = chapterOrder(b)
    return oa !== ob ? oa - ob : a.localeCompare(b)
  })

  const groupCount = Math.max(labels.length, 1)
  const nodeCount = Math.max(rawNodes.length, 1)
  const worldW = Math.round(Math.max(1800, 900 + Math.sqrt(nodeCount) * 78 + Math.sqrt(groupCount) * 180))
  const worldH = Math.round(Math.max(1200, worldW * 0.74))
  const worldCx = worldW / 2
  const worldCy = worldH / 2
  const outerRx = worldW * 0.44
  const outerRy = worldH * 0.41
  const maxGroupScore = Math.max(...[...groupStats.values()].map((s) => s.score), 1)
  const rawById = new Map(rawNodes.map((n) => [n.id, n]))

  const clusterLinks = new Map()
  rawEdges.forEach((e) => {
    const source = rawById.get(e.source_node_id)
    const target = rawById.get(e.target_node_id)
    if (!source || !target) return
    const a = keyFn(source)
    const b = keyFn(target)
    if (!a || !b || a === b) return
    const key = a < b ? `${a}\u0000${b}` : `${b}\u0000${a}`
    clusterLinks.set(key, (clusterLinks.get(key) || 0) + 1)
  })

  const positions = new Map()
  placementLabels.forEach((label, rank) => {
    const stats = groupStats.get(label)
    const rankT = placementLabels.length <= 1 ? 0 : rank / (placementLabels.length - 1)
    const scoreT = stats.score / maxGroupScore
    const angle = -Math.PI / 2 + rank * GOLDEN_ANGLE + (hash(label + 'angle') - 0.5) * 0.32
    const desiredT = placementLabels.length <= 1 ? 0 : clamp(0.26 + Math.pow(rankT, 0.66) * 0.74 - scoreT * 0.24, 0.05, 0.98)
    const radius = Math.min(320, Math.max(118, 76 + Math.sqrt(stats.size) * 32))
    const x = worldCx + Math.cos(angle) * outerRx * desiredT
    const y = worldCy + Math.sin(angle) * outerRy * desiredT
    positions.set(label, {
      x,
      y,
      anchorX: x,
      anchorY: y,
      rx: radius * (1.02 + hash(label + 'rx') * 0.12),
      ry: radius * (0.78 + hash(label + 'ry') * 0.12),
      desiredT,
      scoreT,
      angle,
    })
  })

  // Let connected chapter regions attract each other while central/high-degree
  // regions stay near the middle. This avoids the old rectangular grid.
  const links = [...clusterLinks.entries()].map(([key, weight]) => {
    const [a, b] = key.split('\u0000')
    return { a, b, weight }
  })
  for (let iter = 0; iter < 170; iter += 1) {
    for (let i = 0; i < placementLabels.length; i += 1) {
      for (let j = i + 1; j < placementLabels.length; j += 1) {
        const a = positions.get(placementLabels[i])
        const b = positions.get(placementLabels[j])
        const dx = b.x - a.x
        const dy = b.y - a.y
        const dist = Math.max(1, Math.hypot(dx, dy))
        const nx = dx / dist
        const ny = dy / dist
        const minDist = Math.max(300, (a.rx + b.rx) * 1.55)
        const repel = dist < minDist ? (minDist - dist) * 0.035 : (minDist * minDist * 0.0012) / dist
        a.x -= nx * repel
        a.y -= ny * repel
        b.x += nx * repel
        b.y += ny * repel
      }
    }

    links.forEach((link) => {
      const a = positions.get(link.a)
      const b = positions.get(link.b)
      if (!a || !b) return
      const dx = b.x - a.x
      const dy = b.y - a.y
      const dist = Math.max(1, Math.hypot(dx, dy))
      const target = Math.max(360, (a.rx + b.rx) * 1.25)
      const pull = (dist - target) * 0.0009 * Math.min(5, Math.log1p(link.weight))
      a.x += (dx / dist) * pull
      a.y += (dy / dist) * pull
      b.x -= (dx / dist) * pull
      b.y -= (dy / dist) * pull
    })

    positions.forEach((p) => {
      const dx = p.x - worldCx
      const dy = p.y - worldCy
      const dist = Math.max(1, Math.hypot(dx, dy))
      const radialTarget = p.desiredT * Math.hypot(outerRx, outerRy) * 0.62
      const radialShift = (radialTarget - dist) * 0.018
      p.x += (dx / dist) * radialShift
      p.y += (dy / dist) * radialShift
      p.x += (p.anchorX - p.x) * 0.024
      p.y += (p.anchorY - p.y) * 0.024
      p.x += (worldCx - p.x) * 0.006 * p.scoreT
      p.y += (worldCy - p.y) * 0.006 * p.scoreT
      p.x = clamp(p.x, p.rx + 58, worldW - p.rx - 58)
      p.y = clamp(p.y, p.ry + 58, worldH - p.ry - 58)
    })
  }

  const baseClusters = labels.map((label, i) => {
    const p = positions.get(label) || { x: worldCx, y: worldCy, rx: 110, ry: 90 }
    return { id: label, label, cx: p.x, cy: p.y, rx: p.rx, ry: p.ry, color: PALETTE[i % PALETTE.length] }
  })
  const clusterById = new Map(baseClusters.map((c) => [c.id, c]))

  const nodes = rawNodes.map((n) => {
    const c = clusterById.get(keyFn(n))
    const stats = nodeStats.get(n.id) || { degree: 0 }
    const rootT = Math.pow(stats.degree / maxDegree, 0.62)
    const leafT = 1 - rootT
    const dx = c.cx - worldCx
    const dy = c.cy - worldCy
    const clusterDist = Math.hypot(dx, dy)
    const baseAngle = clusterDist > 8 ? Math.atan2(dy, dx) : hash(n.id + 'center-angle') * Math.PI * 2
    const spread = Math.PI * (0.44 + hash(c.id + 'spread') * 0.42)
    const angle = baseAngle + (hash(n.id + 'angle') - 0.5) * spread + gaussian(n.id + 'turn') * 0.08
    const radiusT = clamp(0.2 + leafT * 0.82 + Math.abs(gaussian(n.id + 'radius')) * 0.08, 0.14, 1.08)
    const localX = c.cx + Math.cos(angle) * c.rx * radiusT
    const localY = c.cy + Math.sin(angle) * c.ry * radiusT
    const rootPull = rootT * 0.2

    return {
      id: n.id,
      x: clamp(lerp(localX, worldCx, rootPull), 24, worldW - 24),
      y: clamp(lerp(localY, worldCy, rootPull), 24, worldH - 24),
      cluster: c.id,
      type: n.type === 'exogenous' ? 'agent' : 'source',
      stale: STALE_STATUS.has(n.status),
      conflict: conflictNodes.has(n.id),
      title: n.title || n.entity || n.id,
      summary: n.summary || '',
    }
  })

  const membersByCluster = new Map()
  nodes.forEach((n) => {
    if (!membersByCluster.has(n.cluster)) membersByCluster.set(n.cluster, [])
    membersByCluster.get(n.cluster).push(n)
  })

  const clusters = baseClusters.map((c) => {
    const members = membersByCluster.get(c.id) || []
    if (!members.length) return c

    const cx = members.reduce((sum, n) => sum + n.x, 0) / members.length
    const cy = members.reduce((sum, n) => sum + n.y, 0) / members.length
    const maxDx = Math.max(...members.map((n) => Math.abs(n.x - cx)), 0)
    const maxDy = Math.max(...members.map((n) => Math.abs(n.y - cy)), 0)
    const rmsDx = Math.sqrt(members.reduce((sum, n) => sum + (n.x - cx) ** 2, 0) / members.length)
    const rmsDy = Math.sqrt(members.reduce((sum, n) => sum + (n.y - cy) ** 2, 0) / members.length)

    return {
      ...c,
      cx,
      cy,
      rx: clamp(Math.max(maxDx + 42, rmsDx * 2.25, 78), 78, worldW * 0.22),
      ry: clamp(Math.max(maxDy + 36, rmsDy * 2.25, 58), 58, worldH * 0.2),
    }
  })

  const edges = rawEdges.map((e) => ({
    a: e.source_node_id,
    b: e.target_node_id,
    conflict: e.label === 'contradicts',
    stale: !!e.invalid_at || e.label === 'superseded_by' || e.label === 'supersedes',
    support: e.label === 'supports',
  }))

  return { nodes, edges, clusters, worldW, worldH }
}

export function docFromNode(n) {
  return {
    title: n.title || n.entity || n.id,
    badge: n.type === 'exogenous' ? 'agent' : 'source',
    meta:
      n.status && n.status !== 'active'
        ? `Status: ${n.status}. Kept for history.`
        : n.original_document_name
          ? `Source: ${n.original_document_name}`
          : 'Click Edit to change this note.',
    markdown: n.body || `# ${n.title || n.id}\n\n${n.summary || ''}`,
  }
}
