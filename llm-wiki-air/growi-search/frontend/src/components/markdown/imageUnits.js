/**
 * Pure helpers for the `<image-unit>` blocks embedded in node markdown:
 * parsing/serializing them, splitting a document into markdown/image parts,
 * and converting inline HTML tables to markdown tables for the rich editor.
 */

function stopPreviewInteractionPropagation(event) {
  event.stopPropagation()
}

export function getPreviewInteractionProps() {
  return {
    onPointerDown: stopPreviewInteractionPropagation,
    onPointerUp: stopPreviewInteractionPropagation,
    onMouseDown: stopPreviewInteractionPropagation,
    onMouseUp: stopPreviewInteractionPropagation,
    onClick: stopPreviewInteractionPropagation,
    onDoubleClick: stopPreviewInteractionPropagation,
    onTouchStart: stopPreviewInteractionPropagation,
    onTouchEnd: stopPreviewInteractionPropagation,
  }
}

export function normalizeImageSrc(src) {
  if (typeof src !== 'string') return ''

  const trimmed = src.trim()

  if (/^data:image\//i.test(trimmed)) {
    return trimmed.replace(/\s+/g, '')
  }

  return trimmed
}

export function isSafeImageSrc(src) {
  const value = normalizeImageSrc(src)

  return (
    value.startsWith('/') ||
    value.startsWith('./') ||
    value.startsWith('../') ||
    /^https?:\/\//i.test(value) ||
    /^data:image\/(png|jpe?g|gif|webp|bmp);base64,/i.test(value)
  )
}

function extractAttribute(html, attrName) {
  const pattern = new RegExp(
    `${attrName}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s>]+))`,
    'i',
  )

  const match = html.match(pattern)

  return match?.[1] || match?.[2] || match?.[3] || ''
}

function extractTagContent(html, tagName) {
  const pattern = new RegExp(
    `<${tagName}\\b[^>]*>([\\s\\S]*?)<\\/${tagName}>`,
    'i',
  )

  const match = html.match(pattern)

  return match?.[1] || ''
}

function escapeHtml(value = '') {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
}

function escapeAttr(value = '') {
  return escapeHtml(value).replaceAll("'", '&#39;')
}

function unescapeHtml(value = '') {
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    return String(value)
      .replaceAll('&lt;', '<')
      .replaceAll('&gt;', '>')
      .replaceAll('&quot;', '"')
      .replaceAll('&#39;', "'")
      .replaceAll('&amp;', '&')
  }

  const textarea = document.createElement('textarea')
  textarea.innerHTML = value
  return textarea.value
}

function parseImageUnitHtml(html = '') {
  const imgMatch = html.match(/<img\b[^>]*>/i)
  const imgTag = imgMatch?.[0] || ''

  return {
    type: 'image',
    src: normalizeImageSrc(extractAttribute(imgTag, 'src')),
    alt: extractAttribute(imgTag, 'alt'),
    title: extractAttribute(imgTag, 'title'),
    description: extractTagContent(html, 'image-description').trim(),
  }
}

function serializeImageUnit(part) {
  const srcAttr = part.src
    ? ` src="${escapeAttr(normalizeImageSrc(part.src))}"`
    : ''

  const altAttr = ` alt="${escapeAttr(part.alt || '')}"`

  const titleAttr = part.title ? ` title="${escapeAttr(part.title)}"` : ''

  return `<image-unit>
  <image-media>
    <img${srcAttr}${altAttr}${titleAttr}>
  </image-media>
  <image-description>
${part.description || ''}
  </image-description>
</image-unit>`
}

function parseHtmlTableToRows(html = '') {
  if (typeof window === 'undefined' || typeof DOMParser === 'undefined') {
    const rows = [...html.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)]

    return rows.map((row) => {
      const cells = [...row[1].matchAll(/<t[dh]\b[^>]*>([\s\S]*?)<\/t[dh]>/gi)]

      return cells.map((cell) =>
        unescapeHtml(
          cell[1].replace(/<br\s*\/?>/gi, '\n').replace(/<[^>]+>/g, ''),
        ),
      )
    })
  }

  const parser = new DOMParser()
  const doc = parser.parseFromString(html, 'text/html')

  return [...doc.querySelectorAll('tr')].map((row) =>
    [...row.querySelectorAll('th,td')].map((cell) => cell.textContent || ''),
  )
}

function normalizeTableRows(rows) {
  const safeRows = Array.isArray(rows) && rows.length ? rows : [['']]
  const width = Math.max(1, ...safeRows.map((row) => row.length || 0))

  return safeRows.map((row) => {
    const next = [...row]

    while (next.length < width) {
      next.push('')
    }

    return next
  })
}

function escapeMarkdownTableCell(value = '') {
  return String(value).replaceAll('|', '\\|').replace(/\r?\n/g, '<br>')
}

function rowsToMarkdownTable(rows) {
  const safeRows = normalizeTableRows(rows)

  if (!safeRows.length) return ''

  const header = safeRows[0]
  const body = safeRows.slice(1)
  const separator = header.map(() => '---')

  return [
    `| ${header.map(escapeMarkdownTableCell).join(' | ')} |`,
    `| ${separator.join(' | ')} |`,
    ...body.map((row) => `| ${row.map(escapeMarkdownTableCell).join(' | ')} |`),
  ].join('\n')
}

function convertHtmlTablesToMarkdownTables(markdown = '') {
  return markdown.replace(/<table\b[^>]*>[\s\S]*?<\/table>/gi, (tableHtml) => {
    const rows = parseHtmlTableToRows(tableHtml)
    return rowsToMarkdownTable(rows)
  })
}

const IMAGE_UNIT_REGEX = /<image-unit\b[^>]*>[\s\S]*?<\/image-unit>/gi

export function splitMarkdownForRichEditing(markdown = '') {
  const parts = []
  const imageUnitRegex = new RegExp(IMAGE_UNIT_REGEX)

  let lastIndex = 0
  let match

  while ((match = imageUnitRegex.exec(markdown)) !== null) {
    const before = markdown.slice(lastIndex, match.index)

    if (before) {
      parts.push({
        type: 'markdown',
        markdown: convertHtmlTablesToMarkdownTables(before),
      })
    }

    parts.push(parseImageUnitHtml(match[0]))

    lastIndex = match.index + match[0].length
  }

  const after = markdown.slice(lastIndex)

  if (after) {
    parts.push({
      type: 'markdown',
      markdown: convertHtmlTablesToMarkdownTables(after),
    })
  }

  if (!parts.length) {
    parts.push({ type: 'markdown', markdown: '' })
  }

  return parts
}

export function serializeRichEditParts(parts) {
  return parts
    .map((part) => {
      if (part.type === 'image') {
        return serializeImageUnit(part)
      }

      return part.markdown || ''
    })
    .join('\n\n')
}

export function hasImageValidationErrors(markdown = '') {
  const parts = splitMarkdownForRichEditing(markdown)

  return parts.some(
    (part) =>
      part.type === 'image' &&
      normalizeImageSrc(part.src) &&
      !String(part.description || '').trim(),
  )
}

export function splitMarkdownByImageUnits(markdown = '') {
  const parts = []
  const imageUnitRegex = new RegExp(IMAGE_UNIT_REGEX)

  let lastIndex = 0
  let match

  while ((match = imageUnitRegex.exec(markdown)) !== null) {
    const before = markdown.slice(lastIndex, match.index)

    if (before) {
      parts.push({ type: 'markdown', content: before })
    }

    const parsed = parseImageUnitHtml(match[0])

    if (parsed.src) {
      parts.push({
        type: 'image',
        src: parsed.src,
        alt: parsed.alt,
        title: parsed.title,
      })
    }

    lastIndex = match.index + match[0].length
  }

  const after = markdown.slice(lastIndex)

  if (after) {
    parts.push({ type: 'markdown', content: after })
  }

  return parts
}
