export function detectAppPrefix() {
  const parts = window.location.pathname.split('/').filter(Boolean)

  // The final path segment is the wiki database name:
  // /llm-wiki/wiki or /agent/llm-wiki/wiki
  if (parts.length >= 2) return `/${parts.slice(0, -1).join('/')}`

  // /admin or /wiki when no reverse proxy prefix
  return ''
}

export function faviconUrl() {
  return `${detectAppPrefix()}/favicon.svg`
}
