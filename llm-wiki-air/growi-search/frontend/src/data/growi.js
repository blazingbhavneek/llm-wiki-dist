export function growiPageUrl(base, path) {
  const pagePath = String(path || '').trim()
  if (!pagePath) return ''

  let url
  try {
    url = new URL(String(base || ''))
  } catch {
    return ''
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return ''

  const page = pagePath.split('/').map((part) => {
    try {
      return encodeURIComponent(decodeURIComponent(part))
    } catch {
      return encodeURIComponent(part)
    }
  }).join('/')
  url.pathname = `${url.pathname.replace(/\/+$/, '')}/${page.replace(/^\/+/, '')}`
  url.search = ''
  url.hash = ''
  return url.href
}

export function growiLinkFor(doc, connection) {
  if (!connection?.enabled) return null
  const view = growiPageUrl(connection.url, doc?.path || doc?.source_path)
  return view ? { view } : null
}
