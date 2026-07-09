import { useEffect, useRef, useState } from 'react'
import {
  MDXEditor,
  headingsPlugin,
  listsPlugin,
  quotePlugin,
  thematicBreakPlugin,
  markdownShortcutPlugin,
  tablePlugin,
  linkPlugin,
  linkDialogPlugin,
  codeBlockPlugin,
  codeMirrorPlugin,
  toolbarPlugin,
  UndoRedo,
  BoldItalicUnderlineToggles,
  ListsToggle,
  InsertTable,
  CreateLink,
  InsertThematicBreak,
  CodeToggle,
  Separator,
} from '@mdxeditor/editor'
import '@mdxeditor/editor/style.css'

import { useT } from '../../i18n.jsx'
import { STR } from './strings.js'
import { SafeImage } from './MarkdownRenderer.jsx'
import { SmallBtn } from './ui.jsx'
import {
  normalizeImageSrc,
  serializeRichEditParts,
  splitMarkdownForRichEditing,
} from './imageUnits.js'

/**
 * Hybrid block editor:
 *
 * - <image-unit> blocks use the dedicated ImageUnitEditor.
 * - Markdown tables and HTML tables use MDXEditor.
 * - Everything else uses a plain textarea to avoid MDX parsing problems
 *   with technical text such as "#include <pmf.h>".
 */
export function RichMarkdownEditor({ markdown, onChange }) {
  const [parts, setParts] = useState(() =>
    splitMarkdownForHybridEditing(markdown || ''),
  )

  const lastExternalMarkdownRef = useRef(markdown || '')

  useEffect(() => {
    const externalMarkdown = markdown || ''

    if (externalMarkdown === lastExternalMarkdownRef.current) return

    const nextParts = splitMarkdownForHybridEditing(externalMarkdown)

    setParts(nextParts)
    lastExternalMarkdownRef.current = externalMarkdown
  }, [markdown])

  const updateParts = (nextParts) => {
    setParts(nextParts)

    const nextMarkdown = serializeHybridEditParts(nextParts)

    lastExternalMarkdownRef.current = nextMarkdown
    onChange?.(nextMarkdown)
  }

  const updatePart = (index, patch) => {
    const nextParts = parts.map((part, partIndex) =>
      partIndex === index ? { ...part, ...patch } : part,
    )

    updateParts(nextParts)
  }

  return (
    <div className="h-full overflow-auto bg-white px-[36px] pb-[90px] pt-[28px]">
      <div className="mx-auto w-full max-w-none space-y-6">
        {parts.map((part, index) => {
          if (part.type === 'image') {
            return (
              <ImageUnitEditor
                key={`image-${index}`}
                part={part}
                onChange={(patch) => updatePart(index, patch)}
              />
            )
          }

          if (part.type === 'table') {
            return (
              <TableMarkdownEditor
                key={`table-${index}`}
                markdown={part.markdown || ''}
                onChange={(value) => updatePart(index, { markdown: value })}
              />
            )
          }

          return (
            <PlainMarkdownEditor
              key={`text-${index}`}
              markdown={part.markdown || ''}
              onChange={(value) => updatePart(index, { markdown: value })}
            />
          )
        })}
      </div>
    </div>
  )
}

/**
 * First split by image units using your existing image-unit parser.
 * Then split the non-image markdown sections into:
 *
 * - text parts
 * - table parts
 */
function splitMarkdownForHybridEditing(markdown) {
  const imageSplitParts = splitMarkdownForRichEditing(markdown || '')
  const nextParts = []

  for (const part of imageSplitParts) {
    if (part.type === 'image') {
      nextParts.push(part)
      continue
    }

    const markdownText = part.markdown || ''
    const tableSplitParts = splitTablesFromMarkdownText(markdownText)

    nextParts.push(...tableSplitParts)
  }

  if (!nextParts.length) {
    return [
      {
        type: 'text',
        markdown: '',
      },
    ]
  }

  return mergeAdjacentTextParts(nextParts)
}

/**
 * Serialize hybrid parts back to normal markdown.
 *
 * Image parts are serialized using your existing image-unit serializer.
 * Text and table parts are just markdown strings.
 */
function serializeHybridEditParts(parts) {
  return parts
    .map((part) => {
      if (part.type === 'image') {
        return serializeRichEditParts([part])
      }

      return part.markdown || ''
    })
    .join('')
}

/**
 * Split one markdown string into plain text and table blocks.
 *
 * Detects:
 * - HTML tables: <table>...</table>
 * - Markdown pipe tables:
 *
 *   | A | B |
 *   |---|---|
 *   | 1 | 2 |
 */
function splitTablesFromMarkdownText(markdown) {
  const parts = []
  const source = markdown || ''

  const htmlSplitParts = splitHtmlTables(source)

  for (const part of htmlSplitParts) {
    if (part.type === 'table') {
      parts.push(part)
      continue
    }

    parts.push(...splitMarkdownPipeTables(part.markdown || ''))
  }

  if (!parts.length) {
    return [
      {
        type: 'text',
        markdown: source,
      },
    ]
  }

  return parts
}

function splitHtmlTables(markdown) {
  const parts = []
  const htmlTableRegex = /<table\b[\s\S]*?<\/table>/gi

  let lastIndex = 0
  let match

  while ((match = htmlTableRegex.exec(markdown)) !== null) {
    const before = markdown.slice(lastIndex, match.index)

    if (before) {
      parts.push({
        type: 'text',
        markdown: before,
      })
    }

    parts.push({
      type: 'table',
      markdown: match[0],
    })

    lastIndex = match.index + match[0].length
  }

  const after = markdown.slice(lastIndex)

  if (after) {
    parts.push({
      type: 'text',
      markdown: after,
    })
  }

  if (!parts.length) {
    return [
      {
        type: 'text',
        markdown,
      },
    ]
  }

  return parts
}

function splitMarkdownPipeTables(markdown) {
  const lines = markdown.match(/[^\n]*(?:\n|$)/g)?.filter(Boolean) || []
  const parts = []

  let buffer = ''
  let index = 0
  let inFence = false

  const flushBuffer = () => {
    if (!buffer) return

    parts.push({
      type: 'text',
      markdown: buffer,
    })

    buffer = ''
  }

  while (index < lines.length) {
    const line = lines[index]
    const trimmed = line.trim()

    if (/^(```|~~~)/.test(trimmed)) {
      inFence = !inFence
      buffer += line
      index += 1
      continue
    }

    if (
      !inFence &&
      index + 1 < lines.length &&
      isMarkdownTableHeaderLine(lines[index]) &&
      isMarkdownTableDelimiterLine(lines[index + 1])
    ) {
      flushBuffer()

      const tableLines = [lines[index], lines[index + 1]]
      index += 2

      while (
        index < lines.length &&
        isMarkdownTableBodyLine(lines[index])
      ) {
        tableLines.push(lines[index])
        index += 1
      }

      parts.push({
        type: 'table',
        markdown: tableLines.join(''),
      })

      continue
    }

    buffer += line
    index += 1
  }

  flushBuffer()

  if (!parts.length) {
    return [
      {
        type: 'text',
        markdown,
      },
    ]
  }

  return parts
}

function isMarkdownTableHeaderLine(line) {
  const trimmed = String(line || '').trim()

  if (!trimmed.includes('|')) return false
  if (isMarkdownTableDelimiterLine(trimmed)) return false

  const cells = getMarkdownTableCells(trimmed)

  return cells.length >= 2
}

function isMarkdownTableDelimiterLine(line) {
  const cells = getMarkdownTableCells(line)

  if (cells.length < 2) return false

  return cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()))
}

function isMarkdownTableBodyLine(line) {
  const trimmed = String(line || '').trim()

  if (!trimmed) return false
  if (!trimmed.includes('|')) return false

  const cells = getMarkdownTableCells(trimmed)

  return cells.length >= 2
}

function getMarkdownTableCells(line) {
  const trimmed = String(line || '').trim()

  if (!trimmed.includes('|')) return []

  return trimmed
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim())
}

function mergeAdjacentTextParts(parts) {
  const merged = []

  for (const part of parts) {
    const previous = merged[merged.length - 1]

    if (
      previous &&
      previous.type === 'text' &&
      part.type === 'text'
    ) {
      previous.markdown += part.markdown || ''
      continue
    }

    merged.push(part)
  }

  return merged
}

function PlainMarkdownEditor({ markdown, onChange }) {
  const lineCount = String(markdown || '').split('\n').length
  const minHeight = Math.max(120, Math.min(520, lineCount * 22 + 48))

  return (
    <textarea
      className="w-full resize-y rounded-lg border border-line bg-white p-4 font-mono text-[13px] leading-[1.65] text-slate-900 outline-none focus:border-blue/40 focus:ring-2 focus:ring-blue/10"
      style={{ minHeight }}
      value={markdown || ''}
      onChange={(event) => onChange?.(event.target.value)}
      spellCheck={false}
    />
  )
}

function TableMarkdownEditor({ markdown, onChange }) {
  const editorRef = useRef(null)
  const lastMarkdownRef = useRef(markdown || '')

  useEffect(() => {
    const nextMarkdown = markdown || ''

    if (nextMarkdown === lastMarkdownRef.current) return

    lastMarkdownRef.current = nextMarkdown
    editorRef.current?.setMarkdown?.(nextMarkdown)
  }, [markdown])

  const handleChange = (value) => {
    lastMarkdownRef.current = value
    onChange?.(value)
  }

  return (
    <section className="overflow-hidden rounded-xl border border-line bg-white shadow-sm">
      <div className="border-b border-line bg-soft px-4 py-3 text-[13px] font-bold text-muted">
        Table
      </div>

      <div className="bg-white p-3">
        <MDXEditor
          ref={editorRef}
          markdown={markdown || ''}
          onChange={handleChange}
          contentEditableClassName="md w-full max-w-none min-h-[140px] px-1 py-2 outline-none"
          plugins={[
            headingsPlugin(),
            listsPlugin(),
            quotePlugin(),
            thematicBreakPlugin(),
            markdownShortcutPlugin(),
            tablePlugin(),
            linkPlugin(),
            linkDialogPlugin(),
            codeBlockPlugin({
              defaultCodeBlockLanguage: '',
            }),
            codeMirrorPlugin({
              codeBlockLanguages: {
                js: 'JavaScript',
                jsx: 'JSX',
                ts: 'TypeScript',
                tsx: 'TSX',
                css: 'CSS',
                html: 'HTML',
                mermaid: 'Mermaid',
                text: 'Text',
                markdown: 'Markdown',
              },
            }),
            toolbarPlugin({
              toolbarContents: () => (
                <>
                  <UndoRedo />
                  <Separator />
                  <BoldItalicUnderlineToggles />
                  <CodeToggle />
                  <Separator />
                  <ListsToggle />
                  <Separator />
                  <CreateLink />
                  <InsertTable />
                  <InsertThematicBreak />
                </>
              ),
            }),
          ]}
        />
      </div>
    </section>
  )
}

function ImageUnitEditor({ part, onChange }) {
  const t = useT(STR)
  const fileInputRef = useRef(null)

  const normalizedSrc = normalizeImageSrc(part.src)
  const hasImage = Boolean(normalizedSrc)
  const missingDescription = hasImage && !String(part.description || '').trim()

  const handleUpload = (event) => {
    const file = event.target.files?.[0]
    if (!file) return

    if (!/^image\/(png|jpe?g|gif|webp|bmp)$/i.test(file.type)) {
      event.target.value = ''
      return
    }

    const reader = new FileReader()

    reader.onload = () => {
      const result = String(reader.result || '')

      onChange?.({
        src: result,
        alt: part.alt || '',
        title: part.title || '',
        description: part.description || '',
      })
    }

    reader.readAsDataURL(file)
    event.target.value = ''
  }

  return (
    <section className="overflow-hidden rounded-xl border border-line bg-white shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line bg-soft px-4 py-3">
        <div className="text-[13px] font-bold text-muted">{t.imageBlock}</div>

        <div className="flex flex-wrap items-center gap-2">
          <SmallBtn onClick={() => fileInputRef.current?.click()} confirm>
            {t.uploadReplaceImage}
          </SmallBtn>

          <SmallBtn
            onClick={() => onChange?.({ src: '' })}
            danger
            disabled={!hasImage}
          >
            {t.removeImageData}
          </SmallBtn>

          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp,image/bmp"
            className="hidden"
            onChange={handleUpload}
          />
        </div>
      </div>

      <div className="space-y-4 p-4">
        <div className="rounded-lg border border-line bg-soft p-3">
          {hasImage ? (
            <SafeImage src={part.src} alt={part.alt} title={part.title} />
          ) : (
            <div className="text-[13px] text-muted">{t.noImageData}</div>
          )}
        </div>

        <label className="block">
          <div className="mb-1 text-[12px] font-bold text-muted">
            {t.imageDescription}
            {hasImage && <span className="ml-1 text-[#7c1230]">*</span>}
          </div>

          <textarea
            className={`min-h-[120px] w-full resize-y rounded-lg border bg-white p-3 font-mono text-[13px] leading-[1.55] outline-none ${
              missingDescription ? 'border-red/35' : 'border-line'
            }`}
            value={part.description || ''}
            required={hasImage}
            onChange={(event) =>
              onChange?.({
                description: event.target.value,
              })
            }
            spellCheck={false}
          />

          {missingDescription && (
            <div className="mt-1 text-[12px] font-semibold text-[#7c1230]">
              {t.imageDescriptionRequired}
            </div>
          )}
        </label>
      </div>
    </section>
  )
}