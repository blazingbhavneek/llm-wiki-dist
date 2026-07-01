// Build a document library from the flat graph (/api/graph).
// Backend has no "list documents" endpoint, but every node carries
// `original_document_name`, `cluster`, `source_ranges`, and the pages of one
// document are chain-linked with `follows` edges (source = prev, target = next).
// So we reconstruct documents and their page order entirely client-side.

const AGENT_DOC = 'Agent Notes'

function rangeStart(n) {
  const r = n.source_ranges
  return Array.isArray(r) && r.length ? Number(r[0][0]) || 0 : 0
}

// Order a document's nodes: walk the `follows` chain from its head; append any
// nodes not on the chain by source range so nothing is dropped.
function orderChain(group, nextOf, prevOf) {
  const ids = new Set(group.map((n) => n.id))
  const byId = new Map(group.map((n) => [n.id, n]))

  const heads = group.filter((n) => {
    const p = prevOf.get(n.id)
    return !p || !ids.has(p)
  })
  heads.sort((a, b) => rangeStart(a) - rangeStart(b))

  const ordered = []
  const seen = new Set()
  for (const head of heads) {
    let cur = head.id
    while (cur && ids.has(cur) && !seen.has(cur)) {
      seen.add(cur)
      ordered.push(byId.get(cur))
      cur = nextOf.get(cur)
    }
  }
  // stragglers (cycles / orphans)
  for (const n of group) if (!seen.has(n.id)) ordered.push(n)
  return ordered
}

function mode(values) {
  const counts = new Map()
  let best = null
  let bestN = 0
  for (const v of values) {
    const c = (counts.get(v) || 0) + 1
    counts.set(v, c)
    if (c > bestN) {
      bestN = c
      best = v
    }
  }
  return best
}

// -> { documents: [{name, cluster, type, nodes[]}], topics: [{cluster, docs[]}] }
export function buildLibrary(nodes = [], edges = []) {
  const nextOf = new Map()
  const prevOf = new Map()
  for (const e of edges) {
    if (e.label !== 'follows') continue
    nextOf.set(e.source_node_id, e.target_node_id)
    prevOf.set(e.target_node_id, e.source_node_id)
  }

  const groups = new Map()
  for (const n of nodes) {
    const name =
      n.original_document_name || (n.type === 'exogenous' ? AGENT_DOC : 'Untitled')
    if (!groups.has(name)) groups.set(name, [])
    groups.get(name).push(n)
  }

  const documents = []
  for (const [name, group] of groups) {
    const ordered = orderChain(group, nextOf, prevOf)
    const cluster = mode(group.map((n) => n.cluster).filter(Boolean)) || 'Uncategorized'
    documents.push({
      name,
      cluster,
      type: group.some((n) => n.type === 'exogenous') ? 'exogenous' : 'endogenous',
      nodes: ordered,
    })
  }
  documents.sort((a, b) => a.name.localeCompare(b.name))

  // Topic view: every cluster present at the NODE level (not one-per-doc), each
  // listing the documents that have >=1 node in it. A document reappears under
  // each topic it touches -- expected. This surfaces all clusters, not just the
  // dominant one per doc.
  const topicMap = new Map() // cluster -> Map(docName -> nodes[])
  for (const n of nodes) {
    const cluster = n.cluster || 'Uncategorized'
    const docName =
      n.original_document_name || (n.type === 'exogenous' ? AGENT_DOC : 'Untitled')
    if (!topicMap.has(cluster)) topicMap.set(cluster, new Map())
    const docs = topicMap.get(cluster)
    if (!docs.has(docName)) docs.set(docName, [])
    docs.get(docName).push(n)
  }
  const topics = [...topicMap.entries()]
    .map(([cluster, docsMap]) => ({
      cluster,
      docs: [...docsMap.entries()]
        .map(([name, ns]) => ({
          name,
          cluster,
          type: ns.some((x) => x.type === 'exogenous') ? 'exogenous' : 'endogenous',
          nodes: orderChain(ns, nextOf, prevOf),
        }))
        .sort((a, b) => a.name.localeCompare(b.name)),
    }))
    .sort((a, b) => a.cluster.localeCompare(b.cluster))

  return { documents, topics, nextOf, prevOf }
}

// prev/next node ids for one page, restricted to its own document.
export function chainNeighbors(node, lib) {
  return {
    prev: lib.prevOf.get(node.id) || null,
    next: lib.nextOf.get(node.id) || null,
  }
}

// Reconstruct the full original document by appending page bodies in order.
// Returns markdown plus a position map so the reader can show page boundaries.
export function reconstructDocument(doc) {
  const parts = []
  const positions = []
  let line = 0
  doc.nodes.forEach((n, i) => {
    const body = (n.body || '').trim()
    positions.push({ index: i + 1, id: n.id, title: n.title || n.entity || n.id, line })
    const block = `${body}\n`
    parts.push(block)
    line += block.split('\n').length
  })
  return { markdown: parts.join('\n'), positions }
}
