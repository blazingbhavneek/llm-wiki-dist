export function buildDocumentMarkdown(title, pages) {
  const sections = (Array.isArray(pages) ? pages : []).map((page) => {
    const body = String(page?.body || '').trim()
    return body || `# ${page?.title || title || 'Untitled'}`
  })
  return sections.filter(Boolean).join('\n\n---\n\n')
}

export function downloadMarkdown(title, markdown, fallbackTitle = 'untitled') {
  if (typeof document === 'undefined') return

  const filename = `${safeFilename(title || fallbackTitle)}.md`
  const blob = new Blob([String(markdown || '')], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

function safeFilename(value) {
  return String(value || 'untitled').trim()
    .replace(/[\\/:*?"<>|]+/g, '-')
    .replace(/\s+/g, ' ')
    .slice(0, 90) || 'untitled'
}
