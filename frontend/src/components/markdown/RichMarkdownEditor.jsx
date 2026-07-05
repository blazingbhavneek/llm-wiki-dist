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
 * Block-based editor: markdown sections use MDXEditor, `<image-unit>` blocks
 * get a dedicated image editor with mandatory descriptions.
 */
export function RichMarkdownEditor({ markdown, onChange }) {
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
