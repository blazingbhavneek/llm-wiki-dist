// Markdown export helpers.
//
// Exported files carry the markdown and nothing else: no frontmatter, no ids,
// no timestamps. What you read in the app is what lands on disk.

// Turn a title into a safe-ish filename stem.
function toFileStem(name, fallback = 'document') {
  const stem = String(name || '')
    .trim()
    .replace(/\.(md|markdown)$/i, '')
    .replace(/[\\/:*?"<>|]/g, '-')
    .replace(/\s+/g, ' ')
    .slice(0, 120)
    .trim()

  return stem || fallback
}

export function downloadMarkdown(name, markdown, fallbackName) {
  const text = String(markdown ?? '')
  const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)

  const a = document.createElement('a')
  a.href = url
  a.download = `${toFileStem(name, fallbackName)}.md`

  document.body.appendChild(a)
  a.click()
  a.remove()

  // Revoke on the next tick: Safari cancels the download if the URL dies first.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

// Chunk bodies already start with their own heading. Strip it so the exported
// document has exactly one heading per chunk, at a level below the doc title.
function splitLeadingHeading(body) {
  const text = String(body || '').trim()
  const match = text.match(/^#{1,6}[ \t]+(.*)(?:\n|$)/)

  if (!match) return { heading: '', rest: text }

  return {
    heading: match[1].trim(),
    rest: text.slice(match[0].length).trim(),
  }
}

function nodeTitle(node) {
  return (
    node?.title ||
    node?.label ||
    node?.entity ||
    node?.name ||
    node?.heading ||
    ''
  )
}

/**
 * Rebuild one document as a single markdown file:
 *
 *   # Document title
 *
 *   ## Chunk title
 *   <chunk body>
 *
 *   ## Next chunk title
 *   <chunk body>
 *
 * `nodes` must already be in reading order (buildLibrary orders them by the
 * `follows` chain).
 */
export function buildDocumentMarkdown(documentTitle, nodes = []) {
  const parts = [`# ${String(documentTitle || '').trim()}`.trim()]

  for (const node of nodes) {
    const { heading, rest } = splitLeadingHeading(node?.body ?? node?.markdown)
    const title = nodeTitle(node) || heading

    if (title) parts.push(`## ${title}`)
    if (rest) parts.push(rest)
  }

  return `${parts.filter(Boolean).join('\n\n')}\n`
}
