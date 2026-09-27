export function faviconUrl() {
  const prefix = window.location.pathname.replace(/\/+$/, '')
  return `${prefix}/favicon.svg`
}
