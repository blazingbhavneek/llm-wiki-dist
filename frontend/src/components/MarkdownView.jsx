import { useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize'
import MermaidDiagram from './MermaidDiagram'
import { useT } from '../i18n.jsx'

const STR = {
  ja: {
    unsafeImg: '安全でない画像ソースをブロックしました。',
    imgFailed:
      '画像の読み込みまたはデコードに失敗しました。base64 データが不完全、または破損している可能性があります。',
    editingStatus: '左側で生 Markdown を編集し、右側でプレビューします。',
    unsavedAnswer: '未保存の回答。ダブルクリックで編集し、wiki に追加してください。',
    draftStatus: '下書き。ダブルクリックで編集し、グラフに追加してください。',
    dirtyStatus: '未保存の変更。保存してグラフのこのノードを更新します。',
    doubleClickEdit: 'ドキュメントをダブルクリックして編集します。',
    editing: '編集中',
    answerDraft: '回答の下書き',
    draft: '下書き',
    agentNote: 'エージェントノート',
    sourceNote: 'ソースノート',
    edit: '編集',
    adding: '追加中...',
    addToWiki: 'wiki に追加',
    addToGraph: 'グラフに追加',
    saving: '保存中...',
    save: '保存',
    del: '削除',
    pagesChain: 'ページ、チェーン順に追加:',
    references: '参照',
  },
  en: {
    unsafeImg: 'Blocked an unsafe image source.',
    imgFailed:
      'The image failed to load or decode. The base64 data may be incomplete or corrupted.',
    editingStatus: 'Editing raw markdown on the left, preview on the right.',
    unsavedAnswer: 'Unsaved answer. Double-click to edit, then add it to the wiki.',
    draftStatus: 'Draft. Double-click to edit, then add it to the graph.',
    dirtyStatus: 'Unsaved changes. Save to update this node in the graph.',
    doubleClickEdit: 'Double-click the document to edit.',
    editing: 'Editing',
    answerDraft: 'Answer draft',
    draft: 'Draft',
    agentNote: 'Agent note',
    sourceNote: 'Source note',
    edit: 'Edit',
    adding: 'Adding...',
    addToWiki: 'Add to wiki',
    addToGraph: 'Add to graph',
    saving: 'Saving...',
    save: 'Save',
    del: 'Delete',
    pagesChain: 'pages, appended in chain order:',
    references: 'References',
  },
}

const markdownSchema = {
  ...defaultSchema,

  tagNames: [
    ...(defaultSchema.tagNames || []),

    // 一般的な HTML タグ
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

    // リスト
    'ul',
    'ol',
    'li',

    // 見出し
    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',

    // リンクとメディア
    'a',
    'img',

    // HTML テーブル対応
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

function normalizeImageSrc(src) {
  if (typeof src !== 'string') return ''

  const trimmed = src.trim()

  // data URL に改行やスペースが含まれている場合は削除します。
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

    const imageUnitHtml = match[0]
    const imgMatch = imageUnitHtml.match(/<img\b[^>]*>/i)
    const imgTag = imgMatch?.[0] || ''

    const src = normalizeImageSrc(extractAttribute(imgTag, 'src'))
    const alt = extractAttribute(imgTag, 'alt')
    const title = extractAttribute(imgTag, 'title')

    if (src) {
      parts.push({
        type: 'image',
        src,
        alt,
        title,
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
      className={`block max-h-[620px] max-w-full rounded-lg border border-line bg-white object-contain ${className}`}
    />
  )
}

function ImageUnit({ src, alt, title }) {
  return (
    <figure className="my-6 overflow-hidden rounded-xl border border-line bg-white p-4 shadow-sm">
      <div className="flex justify-center overflow-auto">
        <SafeImage src={src} alt={alt} title={title} />
      </div>
    </figure>
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
    // Let a mermaid code child render itself (MermaidDiagram) instead of
    // wrapping it in a raw <pre> code block.
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
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[
        rehypeRaw,
        [rehypeSanitize, markdownSchema],
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
            <ImageUnit
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
  mode = 'node', // 'node' | 'draft' | 'fulldoc' | 'answer'
  positions,
  busy,
  busyMessage,
  refs,
  onStartEdit,
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

  const isAgent = doc.badge === 'agent' || doc.badge === 'Agent note'
  const badgeClass = isAgent
    ? 'bg-orange/15 text-[#925d00] border-orange/25'
    : 'bg-green/10 text-[#08785a] border-green/20'

  const statusText = busy && busyMessage
    ? busyMessage
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
              : t.doubleClickEdit

  return (
    <div className="grid h-full grid-rows-[auto_minmax(0,1fr)] bg-white">
      <div className="border-b border-line bg-white px-[26px] pb-[14px] pt-[18px]">
        <div className="mb-[8px] flex flex-wrap items-center gap-2 text-[12px] text-muted">
          <span
            className={`inline-flex items-center gap-[6px] border px-[8px] py-[5px] font-bold ${
              isEditing ? 'border-blue/20 bg-blue/10 text-[#244a9d]' : badgeClass
            }`}
          >
            {isEditing ? t.editing : isAnswer ? t.answerDraft : isDraft ? t.draft : isAgent ? t.agentNote : t.sourceNote}
          </span>
          <span className="inline-flex items-center gap-2">
            {busy && <InlineSpinner />}
            {statusText}
          </span>
        </div>

        <div
          className="flex items-center gap-3"
          onDoubleClick={canEdit && !isEditing ? onStartEdit : undefined}
        >
          <input
            className="w-full border-0 bg-transparent p-0 text-[27px] font-extrabold tracking-tight text-ink outline-none read-only:cursor-default"
            value={draft.title}
            readOnly={!isEditing}
            onChange={(e) => onChangeTitle(e.target.value)}
            onDoubleClick={canEdit && !isEditing ? onStartEdit : undefined}
          />
        </div>

        {!readOnly && (
          <div className="mt-[12px] flex flex-wrap items-center gap-2">
            <SmallBtn onClick={onStartEdit} active={isEditing} disabled={!canEdit}>
              {isEditing ? t.editing : t.edit}
            </SmallBtn>

            {isDraft ? (
              <SmallBtn onClick={onAddToGraph} disabled={busy} confirm>
                {busy ? t.adding : isAnswer ? t.addToWiki : t.addToGraph}
              </SmallBtn>
            ) : (
              <>
                <SmallBtn onClick={onConfirm} disabled={!dirty || busy} confirm>
                  {busy ? t.saving : t.save}
                </SmallBtn>
                <SmallBtn onClick={onDelete} danger>
                  {t.del}
                </SmallBtn>
              </>
            )}
          </div>
        )}
      </div>

      <div className="min-h-0 overflow-auto bg-gradient-to-b from-white to-[#fbfdff]">
        {isEditing ? (
          <div className="grid h-full grid-cols-2 gap-0">
            <textarea
              className="h-full w-full resize-none border-r border-line bg-soft p-[26px] font-mono text-[13px] leading-[1.6] text-ink outline-none"
              value={draft.markdown}
              onChange={(e) => onChangeBody(e.target.value)}
              spellCheck={false}
            />
            <div className="h-full overflow-auto p-[36px]">
              <article className="md mx-auto max-w-[760px]">
                <MarkdownRenderer markdown={draft.markdown} />
                <References refs={refs} onOpenNode={onOpenNode} />
              </article>
            </div>
          </div>
        ) : (
          <div
            className="h-full"
            onDoubleClick={canEdit && !isEditing ? onStartEdit : undefined}
          >
            {readOnly && positions?.length > 0 && (
              <div className="mx-auto max-w-[860px] px-[36px] pt-[24px]">
                <div className="border border-line bg-soft px-[14px] py-[10px] text-[12px] text-muted">
                  <span className="font-bold text-ink">{positions.length}</span> {t.pagesChain}
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
            <article className="md mx-auto max-w-[860px] px-[36px] pb-[90px] pt-[36px]">
              <MarkdownRenderer markdown={draft.markdown} />
              <References refs={refs} onOpenNode={onOpenNode} />
            </article>
          </div>
        )}
      </div>
    </div>
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
      className={`${base} ${tone}`}
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  )
}
