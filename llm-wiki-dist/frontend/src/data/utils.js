export function detectAppPrefix() {
  const parts = window.location.pathname.split('/').filter(Boolean)

  // /llm-wiki/admin or /llm-wiki/wiki
  if (parts.length >= 2) return `/${parts[0]}`

  // /admin or /wiki when no reverse proxy prefix
  return ''
}

export function faviconUrl() {
  return `${detectAppPrefix()}/favicon.svg`
}