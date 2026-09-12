/**
 * GROWI hand-off helpers (WP-F5).
 *
 * A wiki can be backed by a registered GROWI connection instead of a local
 * .sqlite file. When it is, `GET /api/growi` describes that connection
 * (url, mode, paths — never the token); when it is not, it returns
 * `{ enabled: false }` and every helper below stays inert.
 *
 * The `prefer_growi_viewer` switch is deliberately a *client* setting kept in
 * localStorage: it decides where this browser opens a document, not what the
 * server stores, and it defaults to off so behaviour is unchanged.
 */

const PREFER_VIEWER_KEY = 'llm_wiki_prefer_growi_viewer'

/** Fired on `window` when a client-only setting changes. */
export const CLIENT_SETTINGS_EVENT = 'llm-wiki-client-settings-changed'

export function readPreferGrowiViewer() {
  if (typeof window === 'undefined') return false

  try {
    return window.localStorage.getItem(PREFER_VIEWER_KEY) === '1'
  } catch {
    return false
  }
}

export function writePreferGrowiViewer(enabled) {
  if (typeof window === 'undefined') return false

  try {
    window.localStorage.setItem(PREFER_VIEWER_KEY, enabled ? '1' : '0')
  } catch {
    /* private mode: the toggle just does not persist */
  }

  window.dispatchEvent(
    new CustomEvent(CLIENT_SETTINGS_EVENT, {
      detail: { prefer_growi_viewer: Boolean(enabled) },
    }),
  )

  return Boolean(enabled)
}

/** A GROWI page path, escaped segment by segment so spaces survive. */
export function growiPageUrl(baseUrl, path) {
  const base = String(baseUrl || '').replace(/\/+$/, '')
  const clean = String(path || '')

  if (!base || !clean.startsWith('/')) return null

  const encoded = clean
    .split('/')
    .map((segment) => encodeURIComponent(segment))
    .join('/')

  return `${base}${encoded}`
}

/**
 * Deep links for a node, or null when the node did not come from GROWI.
 *
 * The sync job (`Librarian.sync_growi`) indexes each GROWI page as a node with
 * `original_document_name = "growi:<connection>"` and `source_path` = the page
 * path, which is exactly the pair needed to build a link back.
 */
export function growiLinkFor(node, connection) {
  if (!connection?.enabled || !connection?.url || !node) return null

  const documentName = node.original_document_name || ''
  const path = node.source_path || ''

  if (!documentName.startsWith('growi:') || !path.startsWith('/')) return null

  const view = growiPageUrl(connection.url, path)
  if (!view) return null

  return { view, edit: `${view}#edit` }
}
