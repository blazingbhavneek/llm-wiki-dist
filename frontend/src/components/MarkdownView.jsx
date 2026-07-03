import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
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
import MermaidDiagram from './MermaidDiagram'
import { useT } from '../i18n.jsx'

const STR = {
  ja: {
    unsafeImg: '安全でない画像ソースをブロックしました。',
    imgFailed:
      '画像の読み込みまたはデコードに失敗しました。base64 データが不完全、または破損している可能性があります。',
    editingStatus: 'Markdown を直接編集しています。画像ブロックは専用エディタで編集できます。',
    unsavedAnswer: '未保存の回答。編集して wiki に追加してください。',
    draftStatus: '下書き。編集してグラフに追加してください。',
    dirtyStatus: '未保存の変更。保存してグラフのこのノードを更新します。',
    editButtonOnly: '編集するには編集ボタンを押してください。',
    editing: '編集中',
    answerDraft: '回答の下書き',
    draft: '下書き',
    agentNote: 'エージェントノート',
    sourceNote: 'ソースノート',
    edit: '編集',
    cancel: 'キャンセル',
    adding: '追加中...',
    addToWiki: 'wiki に追加',
    addToGraph: 'グラフに追加',
    saving: '保存中...',
    save: '保存',
    del: '削除',
    pagesChain: 'ページ、チェーン順に追加:',
    references: '参照',
    imageBlock: '画像',
    uploadReplaceImage: '画像をアップロード / 置換',
    removeImageData: '画像データを削除',
    imageDescription: '画像説明',
    imageDescriptionRequired: '画像がある場合、説明は必須です。',
    noImageData: '画像データはありません。',
    invalidImages: '説明がない画像があります。',
  },
  en: {
    unsafeImg: 'Blocked an unsafe image source.',
    imgFailed:
      'The image failed to load or decode. The base64 data may be incomplete or corrupted.',
    editingStatus:
      'Editing Markdown directly. Image blocks can be edited with the dedicated image editor.',
    unsavedAnswer: 'Unsaved answer. Edit it, then add it to the wiki.',
    draftStatus: 'Draft. Edit it, then add it to the graph.',
    dirtyStatus: 'Unsaved changes. Save to update this node in the graph.',
    editButtonOnly: 'Use the Edit button to edit this document.',
    editing: 'Editing',
    answerDraft: 'Answer draft',
    draft: 'Draft',
    agentNote: 'Agent note',
    sourceNote: 'Source note',
    edit: 'Edit',
    cancel: 'Cancel',
    adding: 'Adding...',
    addToWiki: 'Add to wiki',
    addToGraph: 'Add to graph',
    saving: 'Saving...',
    save: 'Save',
    del: 'Delete',
    pagesChain: 'pages, appended in chain order:',
    references: 'References',
    imageBlock: 'Image',
    uploadReplaceImage: 'Upload / replace image',
    removeImageData: 'Remove image data',
    imageDescription: 'Image description',
    imageDescriptionRequired: 'Description is required when an image exists.',
    noImageData: 'No image data.',
    invalidImages: 'Some images are missing descriptions.',
  },
}

const markdownSchema = {
  ...defaultSchema,

  tagNames: [
    ...(defaultSchema.tagNames || []),

    'div',
    'span',
    'p',
    'br',
    'hr',
    'blockquote',
    'pre',
    'code',
    'strong',
    'em',
    'u',
    's',
    'sub',
    'sup',

    'ul',
    'ol',
    'li',

    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',

    'a',
    'img',

    'table',
    'thead',
    'tbody',
    'tfoot',
    'tr',
    'th',
    'td',
    'caption',
    'colgroup',
    'col',
  ],

  attributes: {
    ...defaultSchema.attributes,

    '*': [
      ...(defaultSchema.attributes?.['*'] || []),
      'className',
      'class',
      'id',
      'title',
      'align',
    ],

    a: [
      ...(defaultSchema.attributes?.a || []),
      'href',
      'title',
      'target',
      'rel',
    ],

    img: [
      ...(defaultSchema.attributes?.img || []),
      'src',
      'alt',
      'title',
      'width',
      'height',
      'loading',
    ],

    table: [
      ...(defaultSchema.attributes?.table || []),
      'align',
      'border',
      'cellpadding',
      'cellspacing',
      'width',
    ],

    th: [
      ...(defaultSchema.attributes?.th || []),
      'align',
      'colspan',
      'rowspan',
      'width',
    ],

    td: [
      ...(defaultSchema.attributes?.td || []),
      'align',
      'colspan',
      'rowspan',
      'width',
    ],

    col: [
      ...(defaultSchema.attributes?.col || []),
      'align',
      'span',
      'width',
    ],

    colgroup: [
      ...(defaultSchema.attributes?.colgroup || []),
      'align',
      'span',
      'width',
    ],
  },

  protocols: {
    ...defaultSchema.protocols,

    href: [
      ...(defaultSchema.protocols?.href || []),
      'http',
      'https',
      'mailto',
      'tel',
    ],

    src: [
      ...(defaultSchema.protocols?.src || []),
      'http',
      'https',
      'data',
    ],
  },
}

function stopPreviewInteractionPropagation(event) {
  event.stopPropagation()
}

function getPreviewInteractionProps() {
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

function normalizeImageSrc(src) {
  if (typeof src !== 'string') return ''

  const trimmed = src.trim()

  if (/^data:image\//i.test(trimmed)) {
    return trimmed.replace(/\s+/g, '')
  }

  return trimmed
}

function isSafeImageSrc(src) {
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

  const titleAttr = part.title
    ? ` title="${escapeAttr(part.title)}"`
    : ''

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
      const cells = [
        ...row[1].matchAll(/<t[dh]\b[^>]*>([\s\S]*?)<\/t[dh]>/gi),
      ]

      return cells.map((cell) =>
        unescapeHtml(
          cell[1]
            .replace(/<br\s*\/?>/gi, '\n')
            .replace(/<[^>]+>/g, ''),
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
  return String(value)
    .replaceAll('|', '\\|')
    .replace(/\r?\n/g, '<br>')
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
    ...body.map(
      (row) => `| ${row.map(escapeMarkdownTableCell).join(' | ')} |`,
    ),
  ].join('\n')
}

function convertHtmlTablesToMarkdownTables(markdown = '') {
  return markdown.replace(
    /<table\b[^>]*>[\s\S]*?<\/table>/gi,
    (tableHtml) => {
      const rows = parseHtmlTableToRows(tableHtml)
      return rowsToMarkdownTable(rows)
    },
  )
}

function splitMarkdownForRichEditing(markdown = '') {
  const parts = []
  const imageUnitRegex = /<image-unit\b[^>]*>[\s\S]*?<\/image-unit>/gi

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
    parts.push({
      type: 'markdown',
      markdown: '',
    })
  }

  return parts
}

function serializeRichEditParts(parts) {
  return parts
    .map((part) => {
      if (part.type === 'image') {
        return serializeImageUnit(part)
      }

      return part.markdown || ''
    })
    .join('\n\n')
}

function hasImageValidationErrors(markdown = '') {
  const parts = splitMarkdownForRichEditing(markdown)

  return parts.some(
    (part) =>
      part.type === 'image' &&
      normalizeImageSrc(part.src) &&
      !String(part.description || '').trim(),
  )
}

function splitMarkdownByImageUnits(markdown = '') {
  const parts = []
  const imageUnitRegex = /<image-unit\b[^>]*>[\s\S]*?<\/image-unit>/gi

  let lastIndex = 0
  let match

  while ((match = imageUnitRegex.exec(markdown)) !== null) {
    const before = markdown.slice(lastIndex, match.index)

    if (before) {
      parts.push({
        type: 'markdown',
        content: before,
      })
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
    parts.push({
      type: 'markdown',
      content: after,
    })
  }

  return parts
}

function SafeImage({
  src,
  alt = '',
  title,
  className = '',
  width,
  height,
}) {
  const t = useT(STR)
  const [failed, setFailed] = useState(false)

  const normalizedSrc = normalizeImageSrc(src)

  useEffect(() => {
    setFailed(false)
  }, [normalizedSrc])

  if (!normalizedSrc) {
    return null
  }

  if (!isSafeImageSrc(normalizedSrc)) {
    return (
      <div className="my-4 rounded-lg border border-red/25 bg-red/10 p-3 text-[13px] text-[#7c1230]">
        {t.unsafeImg}
      </div>
    )
  }

  if (failed) {
    return (
      <div className="my-4 rounded-lg border border-red/25 bg-red/10 p-3 text-[13px] leading-[1.45] text-[#7c1230]">
        {t.imgFailed}
      </div>
    )
  }

  return (
    <img
      src={normalizedSrc}
      alt={alt || ''}
      title={title}
      width={width}
      height={height}
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
      className={`my-4 block max-h-[620px] max-w-full object-contain ${className}`}
    />
  )
}

function PreviewImageUnit({ src, alt, title }) {
  return (
    <div className="my-4 flex justify-center overflow-auto">
      <SafeImage src={src} alt={alt} title={title} />
    </div>
  )
}

const markdownComponents = {
  img: ({ node, src = '', alt = '', title, width, height }) => (
    <SafeImage
      src={src}
      alt={alt}
      title={title}
      width={width}
      height={height}
    />
  ),

  a: ({ node, href = '', children, ...props }) => {
    const isExternal = /^https?:\/\//i.test(href)

    return (
      <a
        href={href}
        target={isExternal ? '_blank' : undefined}
        rel={isExternal ? 'noreferrer noopener' : undefined}
        className="font-semibold text-blue underline underline-offset-2 hover:opacity-80"
        {...props}
      >
        {children}
      </a>
    )
  },

  table: ({ children }) => (
    <div className="my-5 overflow-x-auto rounded-lg border border-line">
      <table className="w-full border-collapse text-sm">
        {children}
      </table>
    </div>
  ),

  thead: ({ children, ...props }) => (
    <thead className="bg-soft" {...props}>
      {children}
    </thead>
  ),

  tbody: ({ children, ...props }) => (
    <tbody {...props}>
      {children}
    </tbody>
  ),

  tr: ({ children, ...props }) => (
    <tr className="border-b border-line last:border-b-0" {...props}>
      {children}
    </tr>
  ),

  th: ({ children, ...props }) => (
    <th
      className="border-r border-line bg-soft px-3 py-2 text-left font-bold last:border-r-0"
      {...props}
    >
      {children}
    </th>
  ),

  td: ({ children, ...props }) => (
    <td
      className="border-r border-line px-3 py-2 align-top last:border-r-0"
      {...props}
    >
      {children}
    </td>
  ),

  code: ({ node, inline, className = '', children, ...props }) => {
    if (!inline && className.includes('language-mermaid')) {
      return <MermaidDiagram code={String(children).replace(/\n$/, '')} />
    }

    if (inline) {
      return (
        <code
          className="rounded bg-soft px-1.5 py-0.5 font-mono text-[0.9em]"
          {...props}
        >
          {children}
        </code>
      )
    }

    return (
      <code className={className} {...props}>
        {children}
      </code>
    )
  },

  pre: ({ children, ...props }) => {
    const child = Array.isArray(children) ? children[0] : children

    if ((child?.props?.className || '').includes('language-mermaid')) {
      return <>{children}</>
    }

    return (
      <pre
        className="my-4 overflow-x-auto rounded-lg border border-line bg-[#0f172a] p-4 text-sm text-white"
        {...props}
      >
        {children}
      </pre>
    )
  },

  blockquote: ({ children, ...props }) => (
    <blockquote
      className="my-4 border-l-4 border-blue/40 bg-blue/5 px-4 py-2 text-muted"
      {...props}
    >
      {children}
    </blockquote>
  ),
}

function MarkdownChunk({ markdown }) {
  if (!markdown) return null

  return (
    <ReactMarkdown
      remarkPlugins={[
        remarkGfm,
        remarkMath,
      ]}
      rehypePlugins={[
        rehypeRaw,
        [rehypeSanitize, markdownSchema],
        [
          rehypeKatex,
          {
            throwOnError: false,
            strict: false,
            trust: false,
          },
        ],
      ]}
      components={markdownComponents}
    >
      {markdown}
    </ReactMarkdown>
  )
}

function MarkdownRenderer({ markdown }) {
  const parts = useMemo(
    () => splitMarkdownByImageUnits(markdown || ''),
    [markdown],
  )

  return (
    <>
      {parts.map((part, index) => {
        if (part.type === 'image') {
          return (
            <PreviewImageUnit
              key={`image-${index}`}
              src={part.src}
              alt={part.alt}
              title={part.title}
            />
          )
        }

        return (
          <MarkdownChunk
            key={`markdown-${index}`}
            markdown={part.content}
          />
        )
      })}
    </>
  )
}

export default function MarkdownView({
  doc,
  draft,
  isEditing,
  dirty,
  mode = 'node',
  positions,
  busy,
  busyMessage,
  refs,
  onStartEdit,
  onCancelEdit,
  onConfirm,
  onDelete,
  onAddToGraph,
  onOpenNode,
  onChangeTitle,
  onChangeBody,
}) {
  const t = useT(STR)

  if (!doc) return null

  const readOnly = mode === 'fulldoc'
  const isAnswer = mode === 'answer'
  const isDraft = mode === 'draft' || isAnswer
  const canEdit = !readOnly
  const imageErrors = isEditing && hasImageValidationErrors(draft.markdown || '')

  const badgeText = String(doc.badge || '').toLowerCase()
  const isAgent =
    badgeText === 'agent' ||
    badgeText === 'agent note' ||
    badgeText === 'エージェントノート'

  const badgeClass = isAgent
    ? 'bg-orange/15 text-[#925d00] border-orange/25'
    : 'bg-green/10 text-[#08785a] border-green/20'

  const statusText = busy && busyMessage
    ? busyMessage
    : imageErrors
      ? t.invalidImages
      : readOnly
        ? doc.meta
        : isEditing
          ? t.editingStatus
          : isAnswer
            ? t.unsavedAnswer
            : isDraft
              ? t.draftStatus
              : dirty
                ? t.dirtyStatus
                : t.editButtonOnly

  const saveDisabled = !dirty || busy || imageErrors
  const addDisabled = busy || imageErrors

  const handleCancelEdit = () => {
    onCancelEdit?.()
  }

  const titlePreviewInteractionProps = !isEditing
    ? getPreviewInteractionProps()
    : {}

  return (
    <div className="grid h-full w-full grid-rows-[auto_minmax(0,1fr)] bg-white">
      <div className="border-b border-line bg-white px-[26px] pb-[14px] pt-[18px]">
        <div className="mb-[8px] flex flex-wrap items-center gap-2 text-[12px] text-muted">
          <span
            className={`inline-flex items-center gap-[6px] border px-[8px] py-[5px] font-bold ${
              isEditing ? 'border-blue/20 bg-blue/10 text-[#244a9d]' : badgeClass
            }`}
          >
            {isEditing
              ? t.editing
              : isAnswer
                ? t.answerDraft
                : isDraft
                  ? t.draft
                  : isAgent
                    ? t.agentNote
                    : t.sourceNote}
          </span>

          <span className="inline-flex items-center gap-2">
            {busy && <InlineSpinner />}
            {statusText}
          </span>
        </div>

        <div className="flex items-center gap-3">
          <input
            {...titlePreviewInteractionProps}
            className="w-full border-0 bg-transparent p-0 text-[27px] font-extrabold tracking-tight text-ink outline-none read-only:cursor-text"
            value={draft.title}
            readOnly={!isEditing}
            onChange={(e) => onChangeTitle?.(e.target.value)}
          />
        </div>

        {!readOnly && (
          <div className="mt-[12px] flex flex-wrap items-center gap-2">
            <SmallBtn onClick={onStartEdit} active={isEditing} disabled={!canEdit || busy}>
              {isEditing ? t.editing : t.edit}
            </SmallBtn>

            {isEditing && (
              <SmallBtn onClick={handleCancelEdit} disabled={busy}>
                {t.cancel}
              </SmallBtn>
            )}

            {isDraft ? (
              <SmallBtn onClick={onAddToGraph} disabled={addDisabled} confirm>
                {busy ? t.adding : isAnswer ? t.addToWiki : t.addToGraph}
              </SmallBtn>
            ) : (
              <>
                <SmallBtn onClick={onConfirm} disabled={saveDisabled} confirm>
                  {busy ? t.saving : t.save}
                </SmallBtn>

                <SmallBtn onClick={onDelete} danger disabled={busy}>
                  {t.del}
                </SmallBtn>
              </>
            )}
          </div>
        )}
      </div>

      <div className="min-h-0 overflow-auto bg-gradient-to-b from-white to-[#fbfdff]">
        {isEditing ? (
          <RichMarkdownEditor
            markdown={draft.markdown}
            onChange={onChangeBody}
          />
        ) : (
          <PreviewModeContent
            readOnly={readOnly}
            positions={positions}
            markdown={draft.markdown}
            refs={refs}
            onOpenNode={onOpenNode}
          />
        )}
      </div>
    </div>
  )
}

function PreviewModeContent({
  readOnly,
  positions,
  markdown,
  refs,
  onOpenNode,
}) {
  const t = useT(STR)

  return (
    <div
      className="h-full w-full"
      {...getPreviewInteractionProps()}
    >
      {readOnly && positions?.length > 0 && (
        <div className="w-full px-[36px] pt-[24px]">
          <div className="border border-line bg-soft px-[14px] py-[10px] text-[12px] text-muted">
            <span className="font-bold text-ink">{positions.length}</span>{' '}
            {t.pagesChain}
            <span className="ml-[6px]">
              {positions.map((p) => (
                <span key={p.id} className="mr-[6px] whitespace-nowrap">
                  [{p.index}] {p.title}
                </span>
              ))}
            </span>
          </div>
        </div>
      )}

      <article className="md w-full max-w-none px-[36px] pb-[90px] pt-[36px]">
        <MarkdownRenderer markdown={markdown} />
        <References refs={refs} onOpenNode={onOpenNode} />
      </article>
    </div>
  )
}

function RichMarkdownEditor({ markdown, onChange }) {
  const [parts, setParts] = useState(() =>
    splitMarkdownForRichEditing(markdown || ''),
  )

  const lastExternalMarkdownRef = useRef(markdown || '')

  useEffect(() => {
    if ((markdown || '') === lastExternalMarkdownRef.current) return

    const nextParts = splitMarkdownForRichEditing(markdown || '')
    setParts(nextParts)
    lastExternalMarkdownRef.current = markdown || ''
  }, [markdown])

  const updateParts = (nextParts) => {
    setParts(nextParts)

    const nextMarkdown = serializeRichEditParts(nextParts)
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

          return (
            <InlineMarkdownEditor
              key={`markdown-${index}`}
              markdown={part.markdown || ''}
              onChange={(value) => updatePart(index, { markdown: value })}
            />
          )
        })}
      </div>
    </div>
  )
}

function InlineMarkdownEditor({ markdown, onChange }) {
  const editorRef = useRef(null)
  const lastMarkdownRef = useRef(markdown || '')

  useEffect(() => {
    if ((markdown || '') === lastMarkdownRef.current) return

    lastMarkdownRef.current = markdown || ''
    editorRef.current?.setMarkdown?.(markdown || '')
  }, [markdown])

  const handleChange = (value) => {
    lastMarkdownRef.current = value
    onChange?.(value)
  }

  return (
    <div className="bg-white">
      <MDXEditor
        ref={editorRef}
        markdown={markdown || ''}
        onChange={handleChange}
        contentEditableClassName="md w-full max-w-none min-h-[180px] px-1 py-2 outline-none"
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
  )
}

function ImageUnitEditor({ part, onChange }) {
  const t = useT(STR)
  const fileInputRef = useRef(null)

  const normalizedSrc = normalizeImageSrc(part.src)
  const hasImage = Boolean(normalizedSrc)
  const missingDescription =
    hasImage && !String(part.description || '').trim()

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
        <div className="text-[13px] font-bold text-muted">
          {t.imageBlock}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <SmallBtn
            onClick={() => fileInputRef.current?.click()}
            confirm
          >
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
            <SafeImage
              src={part.src}
              alt={part.alt}
              title={part.title}
            />
          ) : (
            <div className="text-[13px] text-muted">
              {t.noImageData}
            </div>
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
            onChange={(e) =>
              onChange?.({
                description: e.target.value,
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

function References({ refs, onOpenNode }) {
  const t = useT(STR)

  if (!refs?.length) return null

  return (
    <div className="mt-[28px] border-t border-line pt-[14px] text-[13px] text-muted">
      <h3 className="mb-[8px] text-[12px] font-bold uppercase tracking-wider text-muted">
        {t.references}
      </h3>

      <ol className="m-0 list-decimal pl-[20px]">
        {refs.map((r) => (
          <li key={r.id} className="my-[6px]">
            <button
              type="button"
              className="border-b border-dotted border-[#244a9d]/40 text-left text-[#244a9d] hover:text-blue"
              onClick={() => onOpenNode?.(r.id)}
            >
              {r.label}
            </button>{' '}
            - {r.note}
          </li>
        ))}
      </ol>
    </div>
  )
}

function InlineSpinner() {
  return (
    <span className="inline-block h-[8px] w-[8px] animate-pulse rounded-full bg-blue" />
  )
}

function SmallBtn({ children, onClick, active, disabled, confirm, danger }) {
  const base = 'border px-[11px] py-[8px] text-[12px] font-bold'

  const tone = disabled
    ? 'cursor-not-allowed border-line bg-white text-muted opacity-45'
    : danger
      ? 'border-red/25 bg-red/10 text-[#7c1230] hover:bg-red/15'
      : confirm && !disabled
        ? 'border-green/25 bg-[#ecfdf5] text-[#065f46]'
        : active
          ? 'border-blue/25 bg-blue/10 text-[#244a9d]'
          : 'border-line bg-white text-muted hover:border-line2 hover:text-ink'

  return (
    <button
      type="button"
      className={`${base} ${tone}`}
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  )
}
