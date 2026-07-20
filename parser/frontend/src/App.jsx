import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  AlertTriangle,
  CheckCircle2,
  Clipboard,
  Code2,
  Download,
  Globe2,
  RefreshCcw,
  Trash2,
  UploadCloud,
} from 'lucide-react'
import Lenis from 'lenis'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const QUEUE_STORAGE_KEY = 'standalone-pdf-parser-queue-v3'
const DONE_TTL_MS = 72 * 60 * 60 * 1000
const POLL_MS = 2500
const MAX_STATUS_CHECKS_PER_POLL = 10
const COOKIE_DAYS = 180

const COOKIE_KEYS = {
  lang: 'pdf_parser_lang',
  codeLang: 'pdf_parser_code_lang',
  llmBaseUrl: 'pdf_parser_llm_base_url',
  llmApiKey: 'pdf_parser_llm_api_key',
  llmModel: 'pdf_parser_llm_model',
}

const IMAGE_UNIT_EXAMPLE = `<image-unit>
  <image-media>
    <img src="data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD..." alt="">
  </image-media>
  <image-description>
    Optional LLM-generated description goes here.
  </image-description>
</image-unit>`

const PYTHON_UTILS_CODE = String.raw`import re

_IMAGE_UNIT_RE = re.compile(
    r"<image-unit\b[^>]*>(?P<body>.*?)</image-unit>",
    re.IGNORECASE | re.DOTALL,
)

_IMAGE_DESCRIPTION_RE = re.compile(
    r"<image-description\b[^>]*>(?P<description>.*?)</image-description>",
    re.IGNORECASE | re.DOTALL,
)

_IMAGE_MEDIA_RE = re.compile(
    r"<image-media\b[^>]*>.*?</image-media>",
    re.IGNORECASE | re.DOTALL,
)

_IMAGE_SRC_RE = re.compile(
    r"""<img\b[^>]*\bsrc=["'](?P<src>data:image/[^"']+)["'][^>]*>""",
    re.IGNORECASE | re.DOTALL,
)


def has_image_units(text: str) -> bool:
    """Return True if the text contains at least one image-unit block."""
    if not isinstance(text, str) or not text:
        return False

    return _IMAGE_UNIT_RE.search(text) is not None


def count_image_units(text: str) -> int:
    """Return the number of image-unit blocks."""
    if not isinstance(text, str) or not text:
        return 0

    return len(list(_IMAGE_UNIT_RE.finditer(text)))


def strip_image_media(text: str) -> str:
    """
    Remove embedded image media payloads from image-unit blocks.

    If an image-description exists, keep only the description.
    If no description exists, remove only the image-media block and keep
    any remaining non-media content inside the image-unit.
    """
    if not isinstance(text, str) or not text:
        return text

    def replace_image_unit(match: re.Match[str]) -> str:
        body = match.group("body")

        description = _IMAGE_DESCRIPTION_RE.search(body)
        if description:
            return description.group("description").strip()

        return _IMAGE_MEDIA_RE.sub("", body).strip()

    return _IMAGE_UNIT_RE.sub(replace_image_unit, text).strip()


def replace_image_media_with_marker(
    text: str,
    marker: str = "[image omitted]",
) -> str:
    """
    Replace image-media blocks with a marker while preserving image-unit structure.

    This is useful when the caller wants to keep a visible placeholder instead
    of deleting image payloads completely.
    """
    if not isinstance(text, str) or not text:
        return text

    return _IMAGE_MEDIA_RE.sub(marker, text).strip()


def extract_image_descriptions(text: str) -> list[str]:
    """Return non-empty image-description values from image-unit blocks."""
    if not isinstance(text, str) or not text:
        return []

    descriptions: list[str] = []

    for unit_match in _IMAGE_UNIT_RE.finditer(text):
        body = unit_match.group("body")
        description = _IMAGE_DESCRIPTION_RE.search(body)

        if description:
            value = description.group("description").strip()
            if value:
                descriptions.append(value)

    return descriptions


def extract_image_data_urls(text: str) -> list[str]:
    """Return data:image/... URLs from image-media blocks."""
    if not isinstance(text, str) or not text:
        return []

    urls: list[str] = []

    for unit_match in _IMAGE_UNIT_RE.finditer(text):
        body = unit_match.group("body")
        media = _IMAGE_MEDIA_RE.search(body)

        if not media:
            continue

        src = _IMAGE_SRC_RE.search(media.group(0))

        if src:
            urls.append(src.group("src").strip())

    return urls


def extract_image_base64(text: str) -> list[str]:
    """Return raw base64 payloads from image data URLs."""
    values: list[str] = []

    for data_url in extract_image_data_urls(text):
        if "," in data_url:
            values.append(data_url.split(",", 1)[1])

    return values`

const JS_UTILS_CODE = String.raw`function imageUnitRegex() {
  return /<image-unit\b[^>]*>(?<body>[\s\S]*?)<\/image-unit>/gi
}

const IMAGE_DESCRIPTION_RE =
  /<image-description\b[^>]*>(?<description>[\s\S]*?)<\/image-description>/i

const IMAGE_MEDIA_RE =
  /<image-media\b[^>]*>[\s\S]*?<\/image-media>/gi

const IMAGE_MEDIA_SINGLE_RE =
  /<image-media\b[^>]*>[\s\S]*?<\/image-media>/i

const IMAGE_SRC_RE =
  /<img\b[^>]*\bsrc=["'](?<src>data:image\/[^"']+)["'][^>]*>/i

export function hasImageUnits(text) {
  if (typeof text !== 'string' || !text) return false

  return imageUnitRegex().test(text)
}

export function countImageUnits(text) {
  if (typeof text !== 'string' || !text) return 0

  return Array.from(text.matchAll(imageUnitRegex())).length
}

export function stripImageMedia(text) {
  if (typeof text !== 'string' || !text) return text

  return text
    .replace(imageUnitRegex(), (...args) => {
      const groups = args[args.length - 1]
      const body = groups?.body || ''

      const description = IMAGE_DESCRIPTION_RE.exec(body)

      if (description?.groups?.description) {
        return description.groups.description.trim()
      }

      return body.replace(IMAGE_MEDIA_SINGLE_RE, '').trim()
    })
    .trim()
}

export function replaceImageMediaWithMarker(text, marker = '[image omitted]') {
  if (typeof text !== 'string' || !text) return text

  return text.replace(IMAGE_MEDIA_RE, marker).trim()
}

export function extractImageDescriptions(text) {
  if (typeof text !== 'string' || !text) return []

  const descriptions = []

  for (const match of text.matchAll(imageUnitRegex())) {
    const body = match.groups?.body || ''
    const description = IMAGE_DESCRIPTION_RE.exec(body)
    const value = description?.groups?.description?.trim()

    if (value) descriptions.push(value)
  }

  return descriptions
}

export function extractImageDataUrls(text) {
  if (typeof text !== 'string' || !text) return []

  const urls = []

  for (const match of text.matchAll(imageUnitRegex())) {
    const body = match.groups?.body || ''
    const media = IMAGE_MEDIA_SINGLE_RE.exec(body)?.[0] || ''
    const src = IMAGE_SRC_RE.exec(media)?.groups?.src

    if (src) urls.push(src.trim())
  }

  return urls
}

export function extractImageBase64(text) {
  return extractImageDataUrls(text)
    .map((url) => {
      const commaIndex = url.indexOf(',')
      return commaIndex >= 0 ? url.slice(commaIndex + 1) : ''
    })
    .filter(Boolean)
}`

const STR = {
  ja: {
    languageName: '日本語',
    switchLanguage: 'English',
    appTitle: 'ドキュメント解析ツール',
    appSubtitle:
      'PDF をアップロードして、生成された Markdown をダウンロードできます。',
    latestTask: '最新タスク',

    uploadPdf: 'PDF アップロード',
    pdfFile: 'PDF ファイル',
    choosePdf: 'PDF を選択',
    noFile: 'ファイル未選択',
    imageDescriptions: '画像説明を生成する',
    imageDescriptionsHelp:
      'OpenAI 互換の画像対応モデルを使える場合のみ有効にしてください。処理時間が長くなることがあります。',
    imageDescriptionMaintenanceBanner:
      '画像説明パイプラインは現在メンテナンス中のため、当面は画像説明が空で返ります。',
    mermaidDiagrams: 'Mermaid 図を生成する',
    mermaidDiagramsHelp:
      'フローチャートなどを Mermaid テキストとして保持できます。ただし検証と修復のため追加時間がかかります。',
    llmBaseUrl: 'LLM ベース URL',
    model: 'モデル',
    apiKey: 'API キー',
    checkVision: '画像対応 LLM を確認',
    checkingEndpoint: 'エンドポイント確認中…',
    visionProbeHelp: 'テキスト確認を 1 回、小さな base64 画像確認を 1 回送信します。',
    visionOk: 'テキスト確認と画像確認に成功しました。このエンドポイントは画像対応の可能性があります。',
    visionPartial: (e) =>
      `テキスト確認は成功しましたが、画像確認に失敗しました。このモデルは画像非対応の可能性があります。詳細: ${e}`,
    llmCheckFailed: (e) =>
      `LLM エンドポイントの確認に失敗しました。ブラウザの CORS エラーでも、Python バックエンドからは動作する場合があります。詳細: ${e}`,

    processPdf: 'PDF を処理',
    processing: '処理中…',
    uploading: 'アップロード中…',
    selectPdf: 'PDF ファイルを選択してください。',
    fillLlm:
      '画像処理を有効にする場合は、LLM ベース URL、API キー、モデルを入力してください。',
    submittedQueued: '送信しました。キューで待機中です…',
    submittedProcessing: '送信しました。処理中です…',
    cacheReady: 'キャッシュ済みの結果があります。ダウンロードできます。',
    submitted: '送信しました。下のキューを確認してください。',

    queueTitle: '解析キュー',
    queueHelp:
      '処理中、待機中、完了、失敗したジョブを表示します。完了した Markdown はダウンロードできます。',
    refresh: '更新',
    refreshing: '更新中…',
    queueEmpty: 'キュー項目はありません。',
    activeWaiting: '処理中・待機中',
    recentFinished: '最近終了した項目',
    completed: '完了',
    failed: '失敗',
    queued: '待機中',
    processingStatus: '処理中',
    position: '順番',
    task: 'タスク',
    unknownFile: 'ファイル名不明',
    download: 'Markdown をダウンロード',
    downloading: 'ダウンロード中…',
    delete: '削除',
    deleting: '削除中…',
    resultNotReady: '結果はまだ準備できていません。',
    downloaded: (name) => `${name} をダウンロードしました。`,
    deleteConfirm:
      'この項目を解析サーバーから削除し、キャッシュ結果も削除しますか？同じ PDF を再処理できるようになります。',

    guideTitle: 'Markdown 画像形式ガイド',
    guideIntro:
      '解析結果に画像が含まれる場合、画像は独自の image-unit ブロックとして Markdown に埋め込まれます。画像本体は base64 data URL として保存され、画像説明がある場合は image-description に入ります。',
    guideUsageTitle: '使い方',
    guideUsageBody:
      'プレビューには画像 URL 抽出関数を使います。テキスト専用 LLM に渡す場合は、巨大な base64 を取り除き、画像説明だけを残してください。',
    codeLanguage: 'コード言語',
    returnedFormat: '返される画像形式',
    utilsTitle: '画像ブロック処理ユーティリティ',
    promptTitle: 'コピー可能な LLM 指示文',
    copyPrompt: '指示文をコピー',
    copy: 'コピー',
    copied: 'コピーしました',
    fencedNote:
      '以下の例は Markdown の fenced code block として表示されるため、そのままドキュメントやプロンプトへ貼り付けられます。',
    python: 'Python',
    javascript: 'JavaScript',
  },

  en: {
    languageName: 'English',
    switchLanguage: '日本語',
    appTitle: 'Document Parser',
    appSubtitle:
      'Upload a PDF and download the generated Markdown.',
    latestTask: 'Latest task',

    uploadPdf: 'Upload PDF',
    pdfFile: 'PDF File',
    choosePdf: 'Choose PDF',
    noFile: 'No file selected',
    imageDescriptions: 'Generate image descriptions',
    imageDescriptionsHelp:
      'Enable this only when you have an OpenAI-compatible vision model. Processing may take longer.',
    imageDescriptionMaintenanceBanner:
      'Image description pipeline is currently under maintenance, so image descriptions will be empty for now.',
    mermaidDiagrams: 'Generate Mermaid diagrams',
    mermaidDiagramsHelp:
      'Flowcharts can be preserved as Mermaid text, but validation and repair add extra processing time.',
    llmBaseUrl: 'LLM Base URL',
    model: 'Model',
    apiKey: 'API Key',
    checkVision: 'Check vision-capable LLM',
    checkingEndpoint: 'Checking endpoint…',
    visionProbeHelp: 'Sends one text probe and one tiny base64 image probe.',
    visionOk: 'Text probe and image probe passed. This endpoint appears image-capable.',
    visionPartial: (e) =>
      `Text probe passed, but image probe failed. This model may not support images. Details: ${e}`,
    llmCheckFailed: (e) =>
      `LLM endpoint check failed. If this is a browser CORS issue, the Python backend may still work. Details: ${e}`,

    processPdf: 'Process PDF',
    processing: 'Processing…',
    uploading: 'Uploading…',
    selectPdf: 'Please select a PDF file.',
    fillLlm: 'When image processing is enabled, enter LLM Base URL, API Key, and Model.',
    submittedQueued: 'Submitted. Waiting in queue…',
    submittedProcessing: 'Submitted. Processing…',
    cacheReady: 'Returned from cache. Ready to download.',
    submitted: 'Submitted. Check the queue below.',

    queueTitle: 'Parser Queue',
    queueHelp:
      'Active, waiting, completed, and failed jobs are shown here. Completed Markdown can be downloaded.',
    refresh: 'Refresh',
    refreshing: 'Refreshing…',
    queueEmpty: 'No queue items.',
    activeWaiting: 'Active / Waiting',
    recentFinished: 'Recently Finished',
    completed: 'Completed',
    failed: 'Failed',
    queued: 'Queued',
    processingStatus: 'Processing',
    position: 'Position',
    task: 'Task',
    unknownFile: 'Unknown filename',
    download: 'Download Markdown',
    downloading: 'Downloading…',
    delete: 'Delete',
    deleting: 'Deleting…',
    resultNotReady: 'The result is not ready yet.',
    downloaded: (name) => `Downloaded ${name}.`,
    deleteConfirm:
      'Delete this item from the parser server and remove cached output? This allows the same PDF to be processed again.',

    guideTitle: 'Markdown Image Format Guide',
    guideIntro:
      'When the parsed output contains images, each image is embedded as a custom image-unit block. The image payload is stored as a base64 data URL. If an image description exists, it is placed inside image-description.',
    guideUsageTitle: 'How to use',
    guideUsageBody:
      'Use the image URL extraction utility for previews. For text-only LLM calls, remove large base64 payloads and keep only the image descriptions.',
    codeLanguage: 'Code language',
    returnedFormat: 'Returned image format',
    utilsTitle: 'Image block utility code',
    promptTitle: 'Copyable LLM instruction',
    copyPrompt: 'Copy instruction',
    copy: 'Copy',
    copied: 'Copied',
    fencedNote:
      'The examples below are shown as Markdown fenced code blocks, so they can be pasted directly into docs or prompts.',
    python: 'Python',
    javascript: 'JavaScript',
  },
}

function getCodeForLang(codeLang) {
  return codeLang === 'js' ? JS_UTILS_CODE : PYTHON_UTILS_CODE
}

function getFenceLang(codeLang) {
  return codeLang === 'js' ? 'javascript' : 'python'
}

function buildFormatMarkdown(t) {
  return [
    `### ${t.returnedFormat}`,
    '',
    t.fencedNote,
    '',
    '```xml',
    IMAGE_UNIT_EXAMPLE,
    '```',
  ].join('\n')
}

function buildUtilsMarkdown(t, codeLang) {
  return [
    `### ${t.utilsTitle}`,
    '',
    `**${t.codeLanguage}:** ${codeLang === 'js' ? t.javascript : t.python}`,
    '',
    `\`\`\`${getFenceLang(codeLang)}`,
    getCodeForLang(codeLang),
    '```',
  ].join('\n')
}

function buildGuideMarkdown(t) {
  return [`### ${t.guideUsageTitle}`, '', t.guideUsageBody].join('\n')
}

function buildPromptText(lang, codeLang) {
  const isJa = lang === 'ja'
  const code = getCodeForLang(codeLang)
  const fence = getFenceLang(codeLang)

  if (isJa) {
    return [
      'Markdown には、次のような独自の画像ブロックが含まれる場合があります。',
      '',
      '```xml',
      IMAGE_UNIT_EXAMPLE,
      '```',
      '',
      '## ルール',
      '',
      '1. `<image-media>` 内の base64 data URL は画像本体です。',
      '2. `<image-description>` が空でない場合、その内容を画像の意味的な説明として使用してください。',
      '3. `<image-description>` が空で、画像を直接確認できない場合は「画像は存在するが、説明は生成されていない」と扱ってください。',
      '4. テキスト専用 LLM に渡す前に、巨大な base64 を含む `<image-media>` を削除してください。',
      '5. 明示的に必要な場合を除き、base64 画像データをテキスト専用 LLM に送信しないでください。',
      '6. 出力は、入力 Markdown の構造をできるだけ保った自然な Markdown にしてください。',
      '',
      '## 使用する正規表現・ユーティリティ',
      '',
      `以下は ${codeLang === 'js' ? 'JavaScript' : 'Python'} 用のユーティリティです。必要に応じてそのまま使用してください。`,
      '',
      `\`\`\`${fence}`,
      code,
      '```',
    ].join('\n')
  }

  return [
    'The Markdown may contain custom image blocks like this:',
    '',
    '```xml',
    IMAGE_UNIT_EXAMPLE,
    '```',
    '',
    '## Rules',
    '',
    '1. The base64 data URL inside `<image-media>` is the embedded image payload.',
    '2. If `<image-description>` is not empty, use it as the semantic description of the image.',
    '3. If `<image-description>` is empty and you cannot inspect the image directly, treat it as an image with no generated description.',
    '4. Before sending content to a text-only LLM, remove `<image-media>` blocks that contain large base64 payloads.',
    '5. Do not send base64 image data to a text-only LLM unless explicitly required.',
    '6. Preserve the input Markdown structure as much as possible.',
    '',
    '## Regex and utility code',
    '',
    `Use or adapt the following ${codeLang === 'js' ? 'JavaScript' : 'Python'} utilities.`,
    '',
    `\`\`\`${fence}`,
    code,
    '```',
  ].join('\n')
}

function getCookie(name) {
  if (typeof document === 'undefined') return ''

  const encodedName = `${encodeURIComponent(name)}=`
  const parts = document.cookie.split(';')

  for (const part of parts) {
    const trimmed = part.trim()
    if (trimmed.startsWith(encodedName)) {
      return decodeURIComponent(trimmed.slice(encodedName.length))
    }
  }

  return ''
}

function setCookie(name, value, days = COOKIE_DAYS) {
  if (typeof document === 'undefined') return

  const maxAge = Math.floor(days * 24 * 60 * 60)
  document.cookie = `${encodeURIComponent(name)}=${encodeURIComponent(
    value || ''
  )}; Max-Age=${maxAge}; Path=/; SameSite=Lax`
}

function nowMs() {
  return Date.now()
}

function parseTimeMs(value) {
  if (!value) return null

  if (typeof value === 'number') {
    return value > 1000000000000 ? value : value * 1000
  }

  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}

function isDoneStatus(status) {
  return status === 'completed' || status === 'failed'
}

function sortQueueItems(a, b) {
  const rank = {
    processing: 0,
    queued: 1,
    completed: 2,
    failed: 3,
  }

  const ar = rank[a.status] ?? 9
  const br = rank[b.status] ?? 9

  if (ar !== br) return ar - br

  if (a.status === 'queued' && b.status === 'queued') {
    return (a.position || 999999) - (b.position || 999999)
  }

  return (b.updatedAt || 0) - (a.updatedAt || 0)
}

function pruneQueueItems(items) {
  const cutoff = nowMs() - DONE_TTL_MS

  return items
    .filter((item) => {
      if (!item?.task_id) return false
      if (!isDoneStatus(item.status)) return true

      const doneAt = item.doneAt || item.finishedAt || item.updatedAt || item.lastSeenAt
      return !doneAt || doneAt >= cutoff
    })
    .sort(sortQueueItems)
}

function loadStoredQueue() {
  if (typeof localStorage === 'undefined') return []

  try {
    const raw = localStorage.getItem(QUEUE_STORAGE_KEY)
    if (!raw) return []

    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? pruneQueueItems(parsed) : []
  } catch {
    return []
  }
}

function saveStoredQueue(items) {
  if (typeof localStorage === 'undefined') return

  try {
    localStorage.setItem(QUEUE_STORAGE_KEY, JSON.stringify(pruneQueueItems(items)))
  } catch {
    // ignore
  }
}

function normalizeQueueItem(raw, fallback = {}) {
  if (!raw) return null

  const taskId = raw.task_id || raw.taskId || fallback.task_id
  if (!taskId) return null

  const status = raw.status || fallback.status || 'queued'
  const finishedAt = parseTimeMs(raw.finished_at || raw.finishedAt)
  const startedAt = parseTimeMs(raw.started_at || raw.startedAt)
  const createdAt = parseTimeMs(raw.created_at || raw.createdAt)
  const ts = nowMs()

  return {
    task_id: taskId,
    filename: raw.filename || fallback.filename || raw.name || fallback.name || '',
    status,
    position: raw.position ?? raw.queue_position ?? fallback.position ?? null,
    result_url: raw.result_url || raw.resultUrl || fallback.result_url || null,
    error: raw.error || fallback.error || null,
    createdAt: createdAt || fallback.createdAt || ts,
    startedAt: startedAt || fallback.startedAt || null,
    finishedAt: finishedAt || fallback.finishedAt || null,
    doneAt: isDoneStatus(status) ? finishedAt || fallback.doneAt || ts : fallback.doneAt || null,
    updatedAt: ts,
    lastSeenAt: ts,
  }
}

function normalizeQueueResponse(data) {
  const items = []

  if (data?.processing) {
    const item = normalizeQueueItem(data.processing, { status: 'processing' })
    if (item) items.push(item)
  }

  for (const raw of data?.queued || []) {
    const item = normalizeQueueItem(raw, { status: 'queued' })
    if (item) items.push(item)
  }

  for (const raw of data?.completed || data?.completed_items || []) {
    const item = normalizeQueueItem(raw, { status: 'completed' })
    if (item) items.push(item)
  }

  for (const raw of data?.failed || data?.failed_items || []) {
    const item = normalizeQueueItem(raw, { status: 'failed' })
    if (item) items.push(item)
  }

  for (const raw of data?.items || data?.tasks || []) {
    const item = normalizeQueueItem(raw)
    if (item) items.push(item)
  }

  return items
}

function mergeQueueItems(oldItems, newItems) {
  const map = new Map()

  for (const oldItem of pruneQueueItems(oldItems)) {
    map.set(oldItem.task_id, oldItem)
  }

  for (const newItem of newItems) {
    if (!newItem?.task_id) continue

    const oldItem = map.get(newItem.task_id)

    map.set(newItem.task_id, {
      ...oldItem,
      ...newItem,
      filename: newItem.filename || oldItem?.filename || '',
      error: newItem.error || oldItem?.error || null,
      result_url: newItem.result_url || oldItem?.result_url || null,
      doneAt: isDoneStatus(newItem.status)
        ? newItem.doneAt || oldItem?.doneAt || nowMs()
        : oldItem?.doneAt || null,
    })
  }

  return pruneQueueItems([...map.values()])
}

const PARSER_API = {
  queue: '/queue',
  upload: '/upload',
  status: (taskId) => `/status/${encodeURIComponent(taskId)}`,
  result: (taskId) => `/result/${encodeURIComponent(taskId)}`,
  queueItem: (taskId) => `/queue/${encodeURIComponent(taskId)}`,
}

function statusLabel(status, t) {
  if (status === 'queued') return t.queued
  if (status === 'processing') return t.processingStatus
  if (status === 'completed') return t.completed
  if (status === 'failed') return t.failed
  return status || '-'
}

function safeMarkdownFilename(item) {
  const original = item.filename || `${item.task_id}.md`

  if (original.toLowerCase().endsWith('.pdf')) {
    return original.replace(/\.pdf$/i, '.md')
  }

  if (original.toLowerCase().endsWith('.md')) {
    return original
  }

  return `${original}.md`
}

async function readErrorResponse(res) {
  try {
    const text = await res.text()
    return text || `${res.status} ${res.statusText}`
  } catch {
    return `${res.status} ${res.statusText}`
  }
}

const UI = {
  page:
    'min-h-screen bg-gradient-to-br from-white via-slate-50 to-blue-50/50 text-slate-900',

  shell:
    'mx-auto max-w-[1040px] px-[28px] py-[26px] animate-[fadeIn_420ms_ease-out]',

  pageCard:
    'rounded-[28px] border border-slate-200/80 bg-white/90 p-[18px] shadow-[0_18px_55px_rgba(15,23,42,0.08)] backdrop-blur-sm',

  insetCard:
    'rounded-2xl border border-slate-200/80 bg-slate-50/80 shadow-sm',

  softCard:
    'rounded-2xl border border-slate-200/80 bg-white/80 shadow-sm',

  input:
    'h-[38px] w-full rounded-xl border border-slate-300 bg-white px-[11px] text-[13px] text-slate-950 shadow-sm outline-none transition-all duration-200 placeholder:text-slate-400 focus:border-blue-400 focus:ring-4 focus:ring-blue-100 disabled:opacity-60',

  secondaryButton:
    'rounded-xl border border-slate-300 bg-white px-[12px] py-[8px] text-[12px] font-bold text-slate-700 shadow-sm transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-slate-400 hover:shadow-md active:translate-y-0 active:scale-[0.97] disabled:opacity-50 disabled:hover:translate-y-0 disabled:hover:shadow-none disabled:active:scale-100',

  secondaryButtonLarge:
    'rounded-xl border border-slate-300 bg-white px-[13px] py-[9px] text-[13px] font-bold text-slate-700 shadow-sm transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-slate-400 hover:shadow-md active:translate-y-0 active:scale-[0.97] disabled:opacity-50 disabled:hover:translate-y-0 disabled:hover:shadow-none disabled:active:scale-100',

  primaryButton:
    'rounded-xl border border-blue-700 bg-blue-600 px-[16px] py-[10px] text-[13px] font-extrabold text-white shadow-sm transition-all duration-200 ease-out hover:-translate-y-0.5 hover:bg-blue-700 hover:shadow-lg hover:shadow-blue-600/20 active:translate-y-0 active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:translate-y-0 disabled:hover:shadow-none disabled:active:scale-100',

  smallPrimaryButton:
    'rounded-xl border border-blue-700 bg-blue-600 px-[11px] py-[8px] text-[12px] font-extrabold text-white shadow-sm transition-all duration-200 ease-out hover:-translate-y-0.5 hover:bg-blue-700 hover:shadow-md hover:shadow-blue-600/20 active:translate-y-0 active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:translate-y-0 disabled:hover:shadow-none disabled:active:scale-100',

  smallSecondaryButton:
    'rounded-xl border border-slate-300 bg-white px-[11px] py-[8px] text-[12px] font-bold text-slate-700 shadow-sm transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-slate-400 hover:shadow-md active:translate-y-0 active:scale-[0.97] disabled:opacity-50 disabled:hover:translate-y-0 disabled:hover:shadow-none disabled:active:scale-100',
}

export default function App() {
  const fileRef = useRef(null)
  const queueRef = useRef([])

  const [lang, setLang] = useState(() => getCookie(COOKIE_KEYS.lang) || 'ja')
  const [codeLang, setCodeLang] = useState(() => getCookie(COOKIE_KEYS.codeLang) || 'python')

  const t = STR[lang] || STR.ja

  const [llmBaseUrl, setLlmBaseUrl] = useState(
    () => getCookie(COOKIE_KEYS.llmBaseUrl) || import.meta.env.VITE_OPENAI_BASE_URL || ''
  )

  const [llmApiKey, setLlmApiKey] = useState(
    () => getCookie(COOKIE_KEYS.llmApiKey) || import.meta.env.VITE_OPENAI_API_KEY || ''
  )

  const [llmModel, setLlmModel] = useState(
    () => getCookie(COOKIE_KEYS.llmModel) || import.meta.env.VITE_MODEL || ''
  )

  const [file, setFile] = useState(null)

  const [busy, setBusy] = useState(false)
  const [queueBusy, setQueueBusy] = useState(false)
  const [downloadingTaskId, setDownloadingTaskId] = useState(null)
  const [deletingTaskId, setDeletingTaskId] = useState(null)

  const [status, setStatus] = useState('')
  const [taskId, setTaskId] = useState(null)
  const [error, setError] = useState(null)

  const [queueItems, setQueueItems] = useState(() => loadStoredQueue())

  const promptText = useMemo(() => buildPromptText(lang, codeLang), [lang, codeLang])

  useEffect(() => {
    const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches

    if (prefersReducedMotion) return

    const lenis = new Lenis({
      duration: 1.05,
      smoothWheel: true,
      wheelMultiplier: 0.9,
      touchMultiplier: 1.2,
    })

    let rafId

    const raf = (time) => {
      lenis.raf(time)
      rafId = requestAnimationFrame(raf)
    }

    rafId = requestAnimationFrame(raf)

    return () => {
      cancelAnimationFrame(rafId)
      lenis.destroy()
    }
  }, [])

  useEffect(() => setCookie(COOKIE_KEYS.lang, lang), [lang])
  useEffect(() => setCookie(COOKIE_KEYS.codeLang, codeLang), [codeLang])
  useEffect(() => setCookie(COOKIE_KEYS.llmBaseUrl, llmBaseUrl), [llmBaseUrl])
  useEffect(() => setCookie(COOKIE_KEYS.llmApiKey, llmApiKey), [llmApiKey])
  useEffect(() => setCookie(COOKIE_KEYS.llmModel, llmModel), [llmModel])

  useEffect(() => {
    queueRef.current = queueItems
    saveStoredQueue(queueItems)
  }, [queueItems])

  const toggleLang = () => {
    setLang((prev) => (prev === 'ja' ? 'en' : 'ja'))
  }

  const upsertQueueItems = (items) => {
    setQueueItems((prev) => mergeQueueItems(prev, items))
  }

  const refreshQueue = async ({ silent = true } = {}) => {
    if (!silent) {
      setQueueBusy(true)
      setError(null)
    }

    try {
      const incoming = []

      const queueRes = await fetch(PARSER_API.queue)

      if (queueRes.ok) {
        const queueData = await queueRes.json()
        incoming.push(...normalizeQueueResponse(queueData))
      }

      const pending = queueRef.current
        .filter((item) => item.status === 'queued' || item.status === 'processing')
        .slice(0, MAX_STATUS_CHECKS_PER_POLL)

      const statusItems = await Promise.all(
        pending.map(async (item) => {
          try {
            const res = await fetch(PARSER_API.status(item.task_id))
            if (!res.ok) return null

            const data = await res.json()

            return normalizeQueueItem(data, {
              task_id: item.task_id,
              filename: item.filename,
              status: item.status,
            })
          } catch {
            return null
          }
        })
      )

      incoming.push(...statusItems.filter(Boolean))

      if (incoming.length > 0) {
        upsertQueueItems(incoming)
      } else {
        setQueueItems((prev) => pruneQueueItems(prev))
      }
    } catch (e) {
      if (!silent) {
        setError(e.message || String(e))
      }
    } finally {
      if (!silent) setQueueBusy(false)
    }
  }

  useEffect(() => {
    let cancelled = false

    const tick = async () => {
      if (cancelled) return
      await refreshQueue({ silent: true })
    }

    tick()

    const timer = setInterval(tick, POLL_MS)

    return () => {
      cancelled = true
      clearInterval(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const pickFile = (e) => {
    const next = e.target.files?.[0] || null
    setFile(next)
    setError(null)
    setStatus('')
    setTaskId(null)
  }

  const upload = async () => {
    if (!file) {
      setError(t.selectPdf)
      return
    }

    setBusy(true)
    setError(null)
    setStatus(t.uploading)
    setTaskId(null)

    try {
      const fd = new FormData()
      fd.append('file', file)

      fd.append('base_url', llmBaseUrl.trim())
      fd.append('api_key', llmApiKey.trim())
      fd.append('model', llmModel.trim())
      // Temporarily forced off in frontend to match backend maintenance mode.
      fd.append('describe_images', 'false')
      fd.append('generate_mermaid', 'false')

      const uploadRes = await fetch(PARSER_API.upload, {
        method: 'POST',
        body: fd,
      })

      if (!uploadRes.ok) {
        const text = await readErrorResponse(uploadRes)
        throw new Error(text || `HTTP ${uploadRes.status}`)
      }

      const uploaded = await uploadRes.json()
      const id = uploaded.task_id

      if (!id) throw new Error('Backend did not return task_id.')

      setTaskId(id)

      const item = normalizeQueueItem(uploaded, {
        task_id: id,
        filename: file.name,
        status: uploaded.status || 'queued',
      })

      if (item) upsertQueueItems([item])

      if (uploaded.status === 'queued') {
        setStatus(t.submittedQueued)
      } else if (uploaded.status === 'processing') {
        setStatus(t.submittedProcessing)
      } else if (uploaded.status === 'completed') {
        setStatus(t.cacheReady)
      } else {
        setStatus(t.submitted)
      }

      await sleep(300)
      await refreshQueue({ silent: true })
    } catch (e) {
      setError(e.message || String(e))
      setStatus('')
    } finally {
      setBusy(false)
    }
  }

  const downloadMarkdown = async (item) => {
    if (item.status !== 'completed') {
      setError(t.resultNotReady)
      return
    }

    setDownloadingTaskId(item.task_id)
    setError(null)

    try {
      const resultRes = await fetch(PARSER_API.result(item.task_id))

      if (!resultRes.ok) {
        const text = await readErrorResponse(resultRes)
        throw new Error(text || `HTTP ${resultRes.status}`)
      }

      const blob = await resultRes.blob()
      const url = URL.createObjectURL(blob)
      const filename = safeMarkdownFilename(item)
      const a = document.createElement('a')

      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()

      URL.revokeObjectURL(url)
      setStatus(t.downloaded(filename))
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setDownloadingTaskId(null)
    }
  }

  const deleteQueueItem = async (item) => {
    const ok = window.confirm(t.deleteConfirm)
    if (!ok) return

    setDeletingTaskId(item.task_id)
    setError(null)

    try {
      const res = await fetch(PARSER_API.queueItem(item.task_id), {
        method: 'DELETE',
      })

      if (!res.ok) {
        const text = await readErrorResponse(res)
        throw new Error(text || `HTTP ${res.status}`)
      }

      setQueueItems((prev) => prev.filter((x) => x.task_id !== item.task_id))
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setDeletingTaskId(null)
    }
  }

  const copyPrompt = async () => {
    await navigator.clipboard.writeText(promptText)
  }

  const activeItems = queueItems.filter(
    (item) => item.status === 'processing' || item.status === 'queued'
  )

  const recentDoneItems = queueItems.filter(
    (item) => item.status === 'completed' || item.status === 'failed'
  )

  return (
    <div className={UI.page}>
      <div className={UI.shell}>
        <header className={`mb-[18px] ${UI.pageCard}`}>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="flex items-center gap-5">
              <img
                src="/favicon.svg"
                alt="Logo"
                className="h-[150px] w-[150px] shrink-0 rounded-[30px] object-contain transition-transform duration-300 hover:scale-[1.03]"
              />

              <div>
                <h1 className="m-0 text-[22px] font-extrabold tracking-tight text-slate-950">
                  {t.appTitle}
                </h1>

                <p className="mt-[7px] max-w-[720px] text-[13px] leading-[1.55] text-slate-500">
                  {t.appSubtitle}
                </p>
              </div>
            </div>

            <div className="flex flex-wrap items-start gap-[8px]">
              {taskId && (
                <div className="rounded-2xl border border-slate-200 bg-slate-50 px-[10px] py-[8px] text-right text-[11px] text-slate-500 shadow-sm">
                  <div className="font-bold uppercase tracking-wider">{t.latestTask}</div>
                  <div className="mt-[3px] max-w-[260px] truncate font-mono">{taskId}</div>
                </div>
              )}

              <button
                type="button"
                onClick={toggleLang}
                className={`inline-flex items-center gap-[7px] ${UI.secondaryButton}`}
              >
                <Globe2 size={15} />
                {t.switchLanguage}
              </button>
            </div>
          </div>
        </header>

        <div className="grid gap-[16px]">
          <section className={UI.pageCard}>
            <SectionTitle icon={<UploadCloud size={17} />} title={t.uploadPdf} />

            <div className="mt-[14px] grid gap-[14px]">
              <div>
                <label className="mb-[6px] block text-[12px] font-extrabold uppercase tracking-wider text-slate-500">
                  {t.pdfFile}
                </label>

                <div className="flex items-center gap-[10px]">
                  <input
                    ref={fileRef}
                    type="file"
                    accept="application/pdf,.pdf"
                    onChange={pickFile}
                    className="hidden"
                  />

                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => fileRef.current?.click()}
                    className={UI.secondaryButtonLarge}
                  >
                    {t.choosePdf}
                  </button>

                  <div className="min-w-0 flex-1 truncate rounded-xl border border-slate-200 bg-slate-50 px-[12px] py-[9px] text-[13px] text-slate-500 shadow-sm">
                    {file ? file.name : t.noFile}
                  </div>
                </div>
              </div>

              <div className="grid gap-[8px]">
                <p className="m-0 rounded-xl border border-[#facc15]/45 bg-[#fef9c3] px-[10px] py-[7px] text-[12px] leading-[1.45] text-[#854d0e]">
                  {t.imageDescriptionMaintenanceBanner}
                </p>

                {/* <div className="mt-[6px] rounded-2xl border border-slate-200 bg-slate-50/80 p-[12px] shadow-sm">
                  <div className="grid grid-cols-1 gap-[12px] md:grid-cols-2">
                    <Field
                      label={t.llmBaseUrl}
                      value={llmBaseUrl}
                      onChange={setLlmBaseUrl}
                      placeholder={import.meta.env.VITE_OPENAI_BASE_URL || ''}
                      disabled={busy}
                    />

                    <Field
                      label={t.model}
                      value={llmModel}
                      onChange={setLlmModel}
                      placeholder={import.meta.env.VITE_MODEL || ''}
                      disabled={busy}
                    />
                  </div>

                  <div className="mt-[12px]">
                    <Field
                      label={t.apiKey}
                      value={llmApiKey}
                      onChange={setLlmApiKey}
                      placeholder={import.meta.env.VITE_OPENAI_API_KEY || ''}
                      disabled={busy}
                      password
                    />
                  </div>
                </div> */}
              </div>

              <div className="mt-[4px] flex flex-wrap items-center gap-[10px]">
                <button
                  type="button"
                  disabled={busy || !file}
                  onClick={upload}
                  className={`inline-flex items-center gap-[8px] ${UI.primaryButton}`}
                >
                  <UploadCloud size={16} />
                  {busy ? t.processing : t.processPdf}
                </button>

                {status && (
                  <span className="rounded-xl border border-slate-200 bg-slate-50 px-[10px] py-[8px] text-[12px] text-slate-500 shadow-sm">
                    {status}
                  </span>
                )}
              </div>

              {error && <Notice type="error">{error}</Notice>}
            </div>
          </section>

          <section className={UI.pageCard}>
            <div className="mb-[12px] flex flex-wrap items-start justify-between gap-[10px]">
              <div>
                <h2 className="m-0 text-[16px] font-extrabold text-slate-950">
                  {t.queueTitle}
                </h2>

                <p className="mt-[4px] text-[12px] text-slate-500">{t.queueHelp}</p>
              </div>

              <button
                type="button"
                disabled={queueBusy}
                onClick={() => refreshQueue({ silent: false })}
                className={`inline-flex items-center gap-[7px] ${UI.secondaryButton}`}
              >
                <RefreshCcw size={14} />
                {queueBusy ? t.refreshing : t.refresh}
              </button>
            </div>

            {queueItems.length === 0 ? (
              <div className="rounded-2xl border border-slate-200 bg-slate-50 px-[12px] py-[10px] text-[13px] text-slate-500 shadow-sm">
                {t.queueEmpty}
              </div>
            ) : (
              <div className="grid gap-[14px]">
                <QueueGroup
                  title={t.activeWaiting}
                  items={activeItems}
                  t={t}
                  downloadingTaskId={downloadingTaskId}
                  deletingTaskId={deletingTaskId}
                  onDownload={downloadMarkdown}
                  onDelete={deleteQueueItem}
                />

                <QueueGroup
                  title={t.recentFinished}
                  items={recentDoneItems}
                  t={t}
                  downloadingTaskId={downloadingTaskId}
                  deletingTaskId={deletingTaskId}
                  onDownload={downloadMarkdown}
                  onDelete={deleteQueueItem}
                />
              </div>
            )}
          </section>

          <GuideSection
            t={t}
            lang={lang}
            codeLang={codeLang}
            setCodeLang={setCodeLang}
            promptText={promptText}
            onCopyPrompt={copyPrompt}
          />
        </div>
      </div>
    </div>
  )
}

function SectionTitle({ icon, title }) {
  return (
    <div className="flex items-center gap-[8px]">
      <div className="text-slate-500">{icon}</div>
      <h2 className="m-0 text-[16px] font-extrabold text-slate-950">{title}</h2>
    </div>
  )
}

function Notice({ type, children }) {
  const cls =
    type === 'error'
      ? 'border-red-200 bg-red-50/90 text-red-700 shadow-red-950/5'
      : 'border-emerald-200 bg-emerald-50/90 text-emerald-700 shadow-emerald-950/5'

  return (
    <div
      className={`mt-[10px] flex gap-[8px] rounded-2xl border px-[12px] py-[9px] text-[13px] leading-[1.45] shadow-sm ${cls}`}
    >
      {type === 'error' ? <AlertTriangle size={16} /> : <CheckCircle2 size={16} />}
      <div>{children}</div>
    </div>
  )
}

function QueueGroup({
  title,
  items,
  t,
  downloadingTaskId,
  deletingTaskId,
  onDownload,
  onDelete,
}) {
  if (!items.length) return null

  return (
    <div>
      <div className="mb-[7px] text-[12px] font-extrabold uppercase tracking-wider text-slate-500">
        {title}
      </div>

      <div className="grid gap-[8px]">
        {items.map((item) => (
          <QueueItem
            key={item.task_id}
            item={item}
            t={t}
            downloading={downloadingTaskId === item.task_id}
            deleting={deletingTaskId === item.task_id}
            onDownload={() => onDownload(item)}
            onDelete={() => onDelete(item)}
          />
        ))}
      </div>
    </div>
  )
}

function QueueItem({ item, t, downloading, deleting, onDownload, onDelete }) {
  const canDownload = item.status === 'completed'

  return (
    <div className="rounded-2xl border border-slate-200/80 bg-slate-50/80 p-[11px] shadow-sm transition-all duration-200 hover:-translate-y-0.5 hover:bg-white hover:shadow-md">
      <div className="flex flex-wrap items-start justify-between gap-[10px]">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-[8px]">
            <span
              className={[
                'rounded-full border px-[9px] py-[4px] text-[11px] font-extrabold uppercase tracking-wider shadow-sm',
                item.status === 'completed'
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                  : item.status === 'failed'
                    ? 'border-red-200 bg-red-50 text-red-700'
                    : item.status === 'processing'
                      ? 'border-blue-200 bg-blue-50 text-blue-700'
                      : 'border-slate-200 bg-white text-slate-500',
              ].join(' ')}
            >
              {statusLabel(item.status, t)}
            </span>

            {item.position && (
              <span className="text-[11px] text-slate-500">
                {t.position}: {item.position}
              </span>
            )}
          </div>

          <div className="mt-[7px] truncate text-[13px] font-bold text-slate-950">
            {item.filename || t.unknownFile}
          </div>

          <div className="mt-[4px] truncate font-mono text-[11px] text-slate-500">
            {t.task}: {item.task_id}
          </div>

          {item.error && (
            <div className="mt-[6px] text-[12px] leading-[1.45] text-red-700">
              {item.error}
            </div>
          )}
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-[8px]">
          <button
            type="button"
            disabled={!canDownload || downloading || deleting}
            onClick={onDownload}
            className={`inline-flex items-center gap-[7px] ${UI.smallPrimaryButton}`}
          >
            <Download size={14} />
            {downloading ? t.downloading : t.download}
          </button>

          <button
            type="button"
            disabled={downloading || deleting}
            onClick={onDelete}
            className={`inline-flex items-center gap-[7px] ${UI.smallSecondaryButton}`}
          >
            <Trash2 size={14} />
            {deleting ? t.deleting : t.delete}
          </button>
        </div>
      </div>
    </div>
  )
}

function Field({ label, value, onChange, placeholder, disabled, password = false }) {
  return (
    <label className="block">
      <span className="mb-[6px] block text-[12px] font-extrabold uppercase tracking-wider text-slate-500">
        {label}
      </span>

      <input
        type={password ? 'password' : 'text'}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className={UI.input}
      />
    </label>
  )
}

function GuideSection({ t, lang, codeLang, setCodeLang, promptText, onCopyPrompt }) {
  return (
    <section className={UI.pageCard}>
      <div className="flex flex-wrap items-start justify-between gap-[12px]">
        <div>
          <h2 className="m-0 text-[16px] font-extrabold text-slate-950">{t.guideTitle}</h2>

          <p className="mt-[6px] max-w-[860px] text-[13px] leading-[1.55] text-slate-500">
            {t.guideIntro}
          </p>
        </div>

        <div className="rounded-2xl border border-slate-200 bg-slate-50 p-[8px] shadow-sm">
          <div className="mb-[6px] flex items-center gap-[6px] text-[11px] font-extrabold uppercase tracking-wider text-slate-500">
            <Code2 size={13} />
            {t.codeLanguage}
          </div>

          <div className="flex gap-[6px]">
            <button
              type="button"
              onClick={() => setCodeLang('python')}
              className={[
                'rounded-xl border px-[10px] py-[6px] text-[12px] font-bold transition-all duration-200 hover:-translate-y-0.5 hover:shadow-md active:translate-y-0 active:scale-[0.97]',
                codeLang === 'python'
                  ? 'border-blue-700 bg-blue-600 text-white shadow-sm shadow-blue-600/20'
                  : 'border-slate-300 bg-white text-slate-700 hover:border-slate-400',
              ].join(' ')}
            >
              {t.python}
            </button>

            <button
              type="button"
              onClick={() => setCodeLang('js')}
              className={[
                'rounded-xl border px-[10px] py-[6px] text-[12px] font-bold transition-all duration-200 hover:-translate-y-0.5 hover:shadow-md active:translate-y-0 active:scale-[0.97]',
                codeLang === 'js'
                  ? 'border-blue-700 bg-blue-600 text-white shadow-sm shadow-blue-600/20'
                  : 'border-slate-300 bg-white text-slate-700 hover:border-slate-400',
              ].join(' ')}
            >
              {t.javascript}
            </button>
          </div>
        </div>
      </div>

      <div className="mt-[12px] rounded-2xl border border-slate-200 bg-slate-50 px-[13px] py-[10px] shadow-sm">
        <MarkdownDoc markdown={buildGuideMarkdown(t)} t={t} />
      </div>

      <div className="mt-[14px]">
        <MarkdownDoc markdown={buildFormatMarkdown(t)} t={t} />
      </div>

      <div className="mt-[14px]">
        <MarkdownDoc markdown={buildUtilsMarkdown(t, codeLang)} t={t} />
      </div>

      <div className="mt-[14px]">
        <div className="mb-[7px] flex flex-wrap items-center justify-between gap-[8px]">
          <div className="text-[24px] font-extrabold uppercase tracking-wider">
            {t.promptTitle}
          </div>

          <CopyButton text={promptText} t={t} onCopy={onCopyPrompt}>
            {t.copyPrompt}
          </CopyButton>
        </div>

        <div className="rounded-2xl border border-slate-200 bg-slate-50 p-[12px] shadow-sm">
          <MarkdownDoc markdown={promptText} t={t} />
        </div>
      </div>
    </section>
  )
}

function MarkdownDoc({ markdown, t }) {
  return (
    <div className="max-w-none text-[13px] leading-[1.6] text-slate-600">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h2: ({ children }) => (
            <h2 className="mb-[8px] mt-[12px] text-[15px] font-extrabold text-slate-900">
              {children}
            </h2>
          ),
          h3: ({ children }) => (
            <h3 className="mb-[8px] mt-0 text-[14px] font-extrabold text-slate-900">
              {children}
            </h3>
          ),
          p: ({ children }) => (
            <p className="my-[7px] text-[13px] leading-[1.6] text-slate-600">{children}</p>
          ),
          ul: ({ children }) => (
            <ul className="my-[7px] list-disc pl-[20px] text-[13px] text-slate-600">
              {children}
            </ul>
          ),
          ol: ({ children }) => (
            <ol className="my-[7px] list-decimal pl-[20px] text-[13px] text-slate-600">
              {children}
            </ol>
          ),
          li: ({ children }) => <li className="my-[3px] leading-[1.55]">{children}</li>,
          strong: ({ children }) => (
            <strong className="font-extrabold text-slate-800">{children}</strong>
          ),
          code: ({ inline, className, children }) => (
            <MarkdownCode inline={inline} className={className}>
              {children}
            </MarkdownCode>
          ),
          pre: ({ children }) => <>{children}</>,
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  )
}

function MarkdownCode({ inline, className, children }) {
  const code = String(children || '').replace(/\n$/, '')
  const match = /language-([a-zA-Z0-9_-]+)/.exec(className || '')
  const language = match?.[1] || ''

  if (inline || !language) {
    return (
      <code className="rounded-md border border-slate-200 bg-white px-[4px] py-[1px] font-mono text-[12px] text-slate-800 shadow-sm">
        {children}
      </code>
    )
  }

  return (
    <div className="my-[10px] overflow-hidden rounded-2xl border border-slate-200 bg-slate-950 shadow-lg shadow-slate-950/10">
      <div className="flex items-center justify-between border-b border-slate-800 bg-slate-900/95 px-[10px] py-[7px]">
        <span className="font-mono text-[11px] font-bold uppercase tracking-wider text-slate-300">
          {language}
        </span>

        <CopyButton
          text={code}
          t={{
            copy: 'Copy',
            copied: 'Copied',
          }}
          small
        />
      </div>

      <pre className="m-0 max-h-[460px] overflow-auto p-[12px] text-[12px] leading-[1.55] text-slate-100">
        <code className={className}>{code}</code>
      </pre>
    </div>
  )
}

function CopyButton({ text, t, children, onCopy, small = false }) {
  const [copied, setCopied] = useState(false)

  const copy = async () => {
    try {
      if (onCopy) {
        await onCopy()
      } else {
        await navigator.clipboard.writeText(text)
      }

      setCopied(true)
      setTimeout(() => setCopied(false), 1300)
    } catch {
      setCopied(false)
    }
  }

  return (
    <button
      type="button"
      onClick={copy}
      className={[
        'inline-flex items-center gap-[7px] rounded-xl border border-slate-300 bg-white font-bold text-slate-700 shadow-sm transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-slate-400 hover:shadow-md active:translate-y-0 active:scale-[0.97]',
        small ? 'px-[8px] py-[5px] text-[11px]' : 'px-[10px] py-[7px] text-[12px]',
      ].join(' ')}
    >
      <Clipboard size={small ? 12 : 14} />
      {copied ? t.copied : children || t.copy}
    </button>
  )
}
