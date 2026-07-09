import { useEffect, useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'

import MermaidDiagram from '../MermaidDiagram'
import { useT } from '../../i18n.jsx'
import { STR } from './strings.js'
import {
  isSafeImageSrc,
  normalizeImageSrc,
  splitMarkdownByImageUnits,
} from './imageUnits.js'

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

    a: [...(defaultSchema.attributes?.a || []), 'href', 'title', 'target', 'rel'],

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

    col: [...(defaultSchema.attributes?.col || []), 'align', 'span', 'width'],

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

    src: [...(defaultSchema.protocols?.src || []), 'http', 'https', 'data'],
  },
}

export function SafeImage({ src, alt = '', title, className = '', width, height }) {
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
    <SafeImage src={src} alt={alt} title={title} width={width} height={height} />
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
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  ),

  thead: ({ children, ...props }) => (
    <thead className="bg-soft" {...props}>
      {children}
    </thead>
  ),

  tbody: ({ children, ...props }) => <tbody {...props}>{children}</tbody>,

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
      remarkPlugins={[remarkGfm, remarkMath]}
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

export function MarkdownRenderer({ markdown }) {
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

        return <MarkdownChunk key={`markdown-${index}`} markdown={part.content} />
      })}
    </>
  )
}
