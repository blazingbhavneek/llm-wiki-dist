import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { useT } from '../i18n.jsx'

const COOKIE_PREFIX = 'llm_wiki_setting_'
const LEGACY_PDF_API_COOKIE = 'pdf_parser_api_base'

const FALLBACK_DEFAULTS = {
  chat_base_url: 'http://10.160.144.101:51029/v1',
  chat_api_key: 'sk-dummy',
  chat_model: 'gemma-4-31B',
  chat_temperature: 0.4,

  embed_base_url: 'http://10.160.144.101:51024/v1',
  embed_model: 'cl-nagoya/ruri-v3-310m',

  rerank_base_url: 'http://10.160.144.101:51025/v1',
  rerank_model: 'cl-nagoya/ruri-v3-reranker-310m',

  pdf_parser_api_base: 'http://10.160.144.101:51023',

  subagent_min_reads: 5,
  subagent_max_reads: 10,
  subagent_max_steps: 20,
  agent_max_steps: 40,
  agent_patience: 20,

  vector_query_k: 50,
  search_candidate_pool: 50,
  rerank_top_k: 20,

  subagent_count: 3,
  subagent_concurrency: 3,
}

const COOKIE_FIELDS = [
  // Graph LLM
  'chat_base_url',
  'chat_api_key',
  'chat_model',
  'chat_temperature',

  // Embedding
  'embed_base_url',
  'embed_model',

  // Reranker
  'rerank_base_url',
  'rerank_model',

  // Agent UI levels
  'depth_level',
  'net_level',
  'subagent_count',

  // PDF parser server
  'pdf_parser_api_base',

  // PDF multimodal image analyzer
  'pdf_image_base_url',
  'pdf_image_api_key',
  'pdf_image_model',
]

const TEST_IMAGE_DATA_URL =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/6X6N8sAAAAASUVORK5CYII='

function getCookie(name) {
  if (typeof document === 'undefined') return null

  const prefix = `${name}=`
  const row = document.cookie
    .split('; ')
    .find((item) => item.startsWith(prefix))

  if (!row) return null

  return decodeURIComponent(row.slice(prefix.length))
}

function setCookie(name, value, days = 365) {
  if (typeof document === 'undefined') return

  const maxAge = days * 24 * 60 * 60
  document.cookie = `${name}=${encodeURIComponent(String(value))}; path=/; max-age=${maxAge}; SameSite=Lax`
}

function deleteCookie(name) {
  if (typeof document === 'undefined') return

  document.cookie = `${name}=; path=/; max-age=0; SameSite=Lax`
}

function settingCookieName(field) {
  return `${COOKIE_PREFIX}${field}`
}

function getCookieOverrides() {
  const out = {}

  for (const field of COOKIE_FIELDS) {
    let value = getCookie(settingCookieName(field))

    if (field === 'pdf_parser_api_base' && value === null) {
      value = getCookie(LEGACY_PDF_API_COOKIE)
    }

    if (value !== null) {
      out[field] = value
    }
  }

  return out
}

function clean(value) {
  return String(value ?? '').trim()
}

function numberValue(value, fallback) {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

function clampInt(value, min, max, fallback) {
  const n = Math.round(numberValue(value, fallback))
  return Math.min(max, Math.max(min, n))
}

function isCloud(url = '') {
  return /openai|anthropic|api\.groq|googleapis|azure/i.test(url)
}

function joinUrl(base, path) {
  const cleanBase = String(base || '').trim().replace(/\/+$/, '')
  const cleanPath = path.startsWith('/') ? path : `/${path}`

  return cleanBase ? `${cleanBase}${cleanPath}` : cleanPath
}

function chatCompletionsUrl(baseUrl) {
  const base = clean(baseUrl).replace(/\/+$/, '')

  if (!base) return ''

  if (base.endsWith('/chat/completions')) {
    return base
  }

  return `${base}/chat/completions`
}

function emitSettingsChanged(patch) {
  if (typeof window === 'undefined') return

  window.dispatchEvent(
    new CustomEvent('llm-wiki-settings-changed', {
      detail: patch,
    }),
  )
}

async function checkOpenAiCompatibleMultimodal({
  baseUrl,
  apiKey,
  model,
}) {
  const url = chatCompletionsUrl(baseUrl)

  if (!url || !model) {
    throw new Error('Missing base URL or model.')
  }

  const headers = {
    'Content-Type': 'application/json',
    Accept: 'application/json',
  }

  if (apiKey) {
    headers.Authorization = `Bearer ${apiKey}`
  }

  const res = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      model,
      temperature: 0,
      max_tokens: 16,
      messages: [
        {
          role: 'user',
          content: [
            {
              type: 'text',
              text: 'Reply with exactly: ok',
            },
            {
              type: 'image_url',
              image_url: {
                url: TEST_IMAGE_DATA_URL,
              },
            },
          ],
        },
      ],
    }),
  })

  const text = await res.text()

  if (!res.ok) {
    throw new Error(text || `HTTP ${res.status}`)
  }

  let data = null

  try {
    data = JSON.parse(text)
  } catch {
    throw new Error('The endpoint did not return JSON.')
  }

  if (!data?.choices?.length) {
    throw new Error('The endpoint did not return chat completion choices.')
  }

  return data
}

const STR = {
  ja: {
    loadErr: (e) => `設定を読み込めませんでした: ${e}`,
    loading: '設定を読み込み中…',
    settings: '設定',
    subtitle: 'グラフ検索と PDF 変換の設定を管理します。',

    graphSettings: 'グラフ設定',
    graphSettingsHint:
      'グラフ検索、回答生成、埋め込み、再ランク、エージェントの探索量を設定します。',

    chatModel: 'チャットモデル',
    chatHint:
      'グラフ回答用 LLM の接続先を設定します。変更した値はこのブラウザーの Cookie に保存されます。',
    baseUrl: 'ベース URL',
    model: 'モデル',
    apiKey: 'API キー',
    temperature: '温度',

    modelEndpoints: 'モデルエンドポイント',
    modelEndpointsHint:
      '埋め込みモデルと再ランカーの接続先を設定します。',
    embedding: '埋め込み',
    embeddingBaseUrl: '埋め込みベース URL',
    embeddingModel: '埋め込みモデル',
    reranker: '再ランカー',
    rerankerBaseUrl: '再ランカーのベース URL',
    rerankerModel: '再ランカーモデル',

    cloudWarn:
      '⚠ クラウドエンドポイントを検出しました。サブエージェントの各ステップは有料呼び出しです。十分なクレジットがない限り、サブエージェントは 1〜2、深さは低めにしてください。',

    behaviour: 'エージェントの振る舞い',
    behaviourHint:
      '高いほど徹底的になりますが、遅くなり、LLM 呼び出しも増えます。',
    depth: 'リサーチの深さ',
    depthHelp: (a, b) =>
      `各エクスプローラーは回答前に ${a}〜${b} 個のノードを読みます。`,
    net: '検索の網',
    netHelp: (k, top) =>
      `各検索は約 ${k} 件のベクトル候補を取得し、上位 ${top} 件を再ランクします。`,
    subagents: 'サブエージェント',
    subHelp: (n, at) =>
      `${n} 個の並列エクスプローラー（同時最大 ${at}）。`,

    pdfParserSettings: 'PDF パーサー設定',
    pdfParserSettingsHint:
      'PDF 変換サーバーと、画像解析に使うマルチモーダル LLM を設定します。画像説明や Mermaid 生成は PDF 変換画面で毎回選択できます。',

    pdfParserServer: 'PDF パーサーサーバー',
    pdfParserServerHint:
      '独自の PDF パーサーサーバーを使う場合は、ここで URL を設定してください。保存前に /queue に接続できるか確認します。',
    pdfParserServerPlaceholder: '例: http://localhost:51023',
    checkAndSaveParserUrl: '確認して保存',
    checkingParserUrl: '確認中...',
    parserUrlSaved: 'PDF パーサー URL を確認して保存しました。',
    parserUrlRequired: 'PDF パーサー URL を入力してください。',
    parserUrlCheckFailed:
      'PDF パーサーサーバーが起動していない、または /queue に接続できません。URL は保存されませんでした。',

    imageAnalyzer: '画像解析用マルチモーダル LLM',
    imageAnalyzerHint:
      'PDF 内の画像説明を生成するときに使う LLM です。デフォルトではチャットモデルと同じベース URL・モデルを使い、API キーは sk-dummy です。',
    imageBaseUrl: '画像解析ベース URL',
    imageApiKey: '画像解析 API キー',
    imageModel: '画像解析モデル',
    checkMultimodal: 'マルチモーダル対応を確認',
    checkingMultimodal: '確認中...',
    multimodalSupported:
      'このモデルは画像入力に対応しているようです。',
    multimodalMissingFields:
      '画像解析ベース URL と画像解析モデルを入力してください。',
    multimodalFailed:
      'マルチモーダル対応を確認できませんでした。モデルが画像入力非対応、URL が違う、API キーが違う、または CORS の可能性があります。',

    reset: 'デフォルトに戻す',

    depthLevels: ['クイック', '軽量', '標準', '深い', '徹底的'],
    netLevels: ['狭い', '絞り込み', 'バランス', '広い', '非常に広い'],
  },
  en: {
    loadErr: (e) => `Could not load settings: ${e}`,
    loading: 'Loading settings…',
    settings: 'Settings',
    subtitle: 'Manage graph search and PDF conversion settings.',

    graphSettings: 'Graph settings',
    graphSettingsHint:
      'Configure graph search, answer generation, embeddings, reranking, and agent effort.',

    chatModel: 'Chat model',
    chatHint:
      'Configure the LLM used for graph answers. Edited values are saved in this browser using cookies.',
    baseUrl: 'Base URL',
    model: 'Model',
    apiKey: 'API key',
    temperature: 'Temperature',

    modelEndpoints: 'Model endpoints',
    modelEndpointsHint:
      'Configure the embedding model and reranker endpoints.',
    embedding: 'Embedding',
    embeddingBaseUrl: 'Embedding base URL',
    embeddingModel: 'Embedding model',
    reranker: 'Reranker',
    rerankerBaseUrl: 'Reranker base URL',
    rerankerModel: 'Reranker model',

    cloudWarn:
      '⚠ Cloud endpoint detected. Every sub-agent step is a paid call. Keep sub-agents at 1–2 and depth low unless you have generous credits.',

    behaviour: 'Agent behaviour',
    behaviourHint: 'Higher = more thorough, slower, and more LLM calls.',
    depth: 'Research depth',
    depthHelp: (a, b) =>
      `Each explorer reads ${a}–${b} nodes before answering.`,
    net: 'Search net',
    netHelp: (k, top) =>
      `Each search pulls ~${k} vector candidates and reranks the top ${top}.`,
    subagents: 'Sub-agents',
    subHelp: (n, at) =>
      `${n} parallel explorer${n > 1 ? 's' : ''} (up to ${at} at once).`,

    pdfParserSettings: 'PDF parser settings',
    pdfParserSettingsHint:
      'Configure the PDF conversion server and the multimodal LLM used for image analysis. Image-description and Mermaid options remain selectable on the PDF conversion screen for each ingestion.',

    pdfParserServer: 'PDF parser server',
    pdfParserServerHint:
      'Set this if you want to use your own PDF parser server. The URL is saved only after /queue is reachable.',
    pdfParserServerPlaceholder: 'e.g. http://10.160.144.101:51023',
    checkAndSaveParserUrl: 'Check and save',
    checkingParserUrl: 'Checking...',
    parserUrlSaved: 'PDF parser URL checked and saved.',
    parserUrlRequired: 'Please enter a PDF parser URL.',
    parserUrlCheckFailed:
      'PDF parser server is not running or /queue is unreachable. URL was not saved.',

    imageAnalyzer: 'Multimodal LLM for image analysis',
    imageAnalyzerHint:
      'This LLM is used when generating descriptions for images inside PDFs. By default it uses the same base URL and model as the chat model, with API key sk-dummy.',
    imageBaseUrl: 'Image-analysis base URL',
    imageApiKey: 'Image-analysis API key',
    imageModel: 'Image-analysis model',
    checkMultimodal: 'Check multimodal support',
    checkingMultimodal: 'Checking...',
    multimodalSupported:
      'This model appears to support image input.',
    multimodalMissingFields:
      'Please enter an image-analysis base URL and image-analysis model.',
    multimodalFailed:
      'Could not verify multimodal support. The model may not support image input, the URL/API key may be wrong, or the browser may be blocked by CORS.',

    reset: 'Reset to defaults',

    depthLevels: ['Quick', 'Light', 'Standard', 'Deep', 'Exhaustive'],
    netLevels: ['Narrow', 'Focused', 'Balanced', 'Wide', 'Very wide'],
  },
}

const DEPTH = [
  {
    label: 'Quick',
    fields: {
      subagent_min_reads: 1,
      subagent_max_reads: 5,
      subagent_max_steps: 10,
      agent_max_steps: 20,
      agent_patience: 10,
    },
  },
  {
    label: 'Light',
    fields: {
      subagent_min_reads: 4,
      subagent_max_reads: 8,
      subagent_max_steps: 16,
      agent_max_steps: 30,
      agent_patience: 15,
    },
  },
  {
    label: 'Standard',
    fields: {
      subagent_min_reads: 5,
      subagent_max_reads: 10,
      subagent_max_steps: 20,
      agent_max_steps: 40,
      agent_patience: 20,
    },
  },
  {
    label: 'Deep',
    fields: {
      subagent_min_reads: 8,
      subagent_max_reads: 16,
      subagent_max_steps: 26,
      agent_max_steps: 50,
      agent_patience: 25,
    },
  },
  {
    label: 'Exhaustive',
    fields: {
      subagent_min_reads: 10,
      subagent_max_reads: 20,
      subagent_max_steps: 32,
      agent_max_steps: 60,
      agent_patience: 30,
    },
  },
]

const NET = [
  {
    label: 'Narrow',
    fields: {
      vector_query_k: 20,
      search_candidate_pool: 20,
      rerank_top_k: 8,
    },
  },
  {
    label: 'Focused',
    fields: {
      vector_query_k: 35,
      search_candidate_pool: 40,
      rerank_top_k: 14,
    },
  },
  {
    label: 'Balanced',
    fields: {
      vector_query_k: 50,
      search_candidate_pool: 50,
      rerank_top_k: 20,
    },
  },
  {
    label: 'Wide',
    fields: {
      vector_query_k: 75,
      search_candidate_pool: 80,
      rerank_top_k: 30,
    },
  },
  {
    label: 'Very wide',
    fields: {
      vector_query_k: 100,
      search_candidate_pool: 120,
      rerank_top_k: 40,
    },
  },
]

const MAX_AGENTS = 6

const agentsFields = (n) => ({
  subagent_count: n,
  subagent_concurrency: Math.min(n, 4),
})

function nearestLevel(table, key, value) {
  let best = 0
  let bestD = Infinity

  table.forEach((lvl, i) => {
    const d = Math.abs(lvl.fields[key] - value)

    if (d < bestD) {
      bestD = d
      best = i
    }
  })

  return best
}

function buildPatch({
  chat,
  modelEndpoints,
  parserServer,
  imageAnalyzer,
  depth,
  net,
  agents,
}) {
  return {
    ...DEPTH[depth].fields,
    ...NET[net].fields,
    ...agentsFields(agents),

    chat_base_url: clean(chat.chat_base_url),
    chat_api_key: clean(chat.chat_api_key),
    chat_model: clean(chat.chat_model),
    chat_temperature: Number(chat.chat_temperature),

    embed_backend: 'server',
    embed_base_url: clean(modelEndpoints.embed_base_url),
    embed_model: clean(modelEndpoints.embed_model),

    rerank_backend: 'server',
    rerank_base_url: clean(modelEndpoints.rerank_base_url),
    rerank_model: clean(modelEndpoints.rerank_model),

    pdf_parser_api_base: clean(parserServer.pdf_parser_api_base),

    pdf_image_base_url: clean(imageAnalyzer.pdf_image_base_url),
    pdf_image_api_key: clean(imageAnalyzer.pdf_image_api_key),
    pdf_image_model: clean(imageAnalyzer.pdf_image_model),
  }
}

export default function SettingsView({ overrides, onApply }) {
  const t = useT(STR)

  const [loaded, setLoaded] = useState(null)

  const [chat, setChat] = useState({
    chat_base_url: '',
    chat_api_key: '',
    chat_model: '',
    chat_temperature: 0.4,
  })

  const [modelEndpoints, setModelEndpoints] = useState({
    embed_base_url: '',
    embed_model: '',
    rerank_base_url: '',
    rerank_model: '',
  })

  const [parserServer, setParserServer] = useState({
    pdf_parser_api_base: '',
  })

  const [parserInput, setParserInput] = useState('')
  const [parserChecking, setParserChecking] = useState(false)
  const [parserStatus, setParserStatus] = useState('')
  const [parserError, setParserError] = useState('')

  const [imageAnalyzer, setImageAnalyzer] = useState({
    pdf_image_base_url: '',
    pdf_image_api_key: '',
    pdf_image_model: '',
  })

  const [imageChecking, setImageChecking] = useState(false)
  const [imageStatus, setImageStatus] = useState('')
  const [imageError, setImageError] = useState('')

  const [depth, setDepthState] = useState(2)
  const [net, setNetState] = useState(2)
  const [agents, setAgentsState] = useState(3)

  const [loadErr, setLoadErr] = useState(null)

  const firstRun = useRef(true)
  const skipNext = useRef(false)

  useEffect(() => {
    api
      .settings()
      .then((backendDefaults) => {
        const defaults = {
          ...FALLBACK_DEFAULTS,
          ...(backendDefaults || {}),
        }

        setLoaded(defaults)

        const cookieOverrides = getCookieOverrides()

        const effective = {
          ...defaults,
          ...(overrides || {}),
          ...cookieOverrides,
        }

        const nextChat = {
          chat_base_url: effective.chat_base_url ?? '',
          chat_api_key: effective.chat_api_key ?? 'sk-dummy',
          chat_model: effective.chat_model ?? '',
          chat_temperature: numberValue(
            effective.chat_temperature,
            defaults.chat_temperature ?? 0.4,
          ),
        }

        const nextModelEndpoints = {
          embed_base_url: effective.embed_base_url ?? '',
          embed_model: effective.embed_model ?? '',
          rerank_base_url: effective.rerank_base_url ?? '',
          rerank_model: effective.rerank_model ?? '',
        }

        const nextParserServer = {
          pdf_parser_api_base: effective.pdf_parser_api_base ?? '',
        }

        const nextImageAnalyzer = {
          pdf_image_base_url:
            effective.pdf_image_base_url ?? nextChat.chat_base_url,
          pdf_image_api_key:
            effective.pdf_image_api_key ?? 'sk-dummy',
          pdf_image_model:
            effective.pdf_image_model ?? nextChat.chat_model,
        }

        const nextDepth =
          effective.depth_level !== undefined
            ? clampInt(effective.depth_level, 0, DEPTH.length - 1, 2)
            : nearestLevel(
                DEPTH,
                'subagent_max_reads',
                effective.subagent_max_reads ?? 10,
              )

        const nextNet =
          effective.net_level !== undefined
            ? clampInt(effective.net_level, 0, NET.length - 1, 2)
            : nearestLevel(
                NET,
                'rerank_top_k',
                effective.rerank_top_k ?? 20,
              )

        const nextAgents = clampInt(
          effective.subagent_count,
          1,
          MAX_AGENTS,
          3,
        )

        setChat(nextChat)
        setModelEndpoints(nextModelEndpoints)
        setParserServer(nextParserServer)
        setParserInput(nextParserServer.pdf_parser_api_base)
        setImageAnalyzer(nextImageAnalyzer)
        setDepthState(nextDepth)
        setNetState(nextNet)
        setAgentsState(nextAgents)

        if (Object.keys(cookieOverrides).length > 0) {
          const patch = buildPatch({
            chat: nextChat,
            modelEndpoints: nextModelEndpoints,
            parserServer: nextParserServer,
            imageAnalyzer: nextImageAnalyzer,
            depth: nextDepth,
            net: nextNet,
            agents: nextAgents,
          })

          onApply?.(patch)
          emitSettingsChanged(patch)
        }
      })
      .catch((e) => setLoadErr(String(e.message || e)))

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const setChatField = (field, value) => {
    setCookie(settingCookieName(field), value)

    setChat((current) => ({
      ...current,
      [field]: value,
    }))
  }

  const setModelEndpointField = (field, value) => {
    setCookie(settingCookieName(field), value)

    setModelEndpoints((current) => ({
      ...current,
      [field]: value,
    }))
  }

  const setImageAnalyzerField = (field, value) => {
    setCookie(settingCookieName(field), value)

    setImageAnalyzer((current) => ({
      ...current,
      [field]: value,
    }))

    setImageStatus('')
    setImageError('')
  }

  const setDepth = (value) => {
    setCookie(settingCookieName('depth_level'), value)
    setDepthState(value)
  }

  const setNet = (value) => {
    setCookie(settingCookieName('net_level'), value)
    setNetState(value)
  }

  const setAgents = (value) => {
    setCookie(settingCookieName('subagent_count'), value)
    setAgentsState(value)
  }

  const checkAndSaveParserBase = async () => {
    const next = clean(parserInput)

    if (!next) {
      setParserError(t.parserUrlRequired)
      setParserStatus('')
      return
    }

    setParserChecking(true)
    setParserError('')
    setParserStatus('')

    try {
      const queueUrl = joinUrl(next, '/queue')

      const res = await fetch(queueUrl, {
        method: 'GET',
        headers: {
          Accept: 'application/json',
        },
      })

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`)
      }

      await res.json()

      setCookie(settingCookieName('pdf_parser_api_base'), next)
      setCookie(LEGACY_PDF_API_COOKIE, next)

      setParserServer({
        pdf_parser_api_base: next,
      })

      setParserStatus(t.parserUrlSaved)
      setParserError('')
    } catch {
      setParserError(t.parserUrlCheckFailed)
      setParserStatus('')
    } finally {
      setParserChecking(false)
    }
  }

  const checkMultimodalSupport = async () => {
    const baseUrl = clean(imageAnalyzer.pdf_image_base_url)
    const apiKey = clean(imageAnalyzer.pdf_image_api_key)
    const model = clean(imageAnalyzer.pdf_image_model)

    if (!baseUrl || !model) {
      setImageError(t.multimodalMissingFields)
      setImageStatus('')
      return
    }

    setImageChecking(true)
    setImageError('')
    setImageStatus('')

    try {
      await checkOpenAiCompatibleMultimodal({
        baseUrl,
        apiKey,
        model,
      })

      setImageStatus(t.multimodalSupported)
      setImageError('')
    } catch (e) {
      setImageError(`${t.multimodalFailed}${e?.message ? ` (${e.message})` : ''}`)
      setImageStatus('')
    } finally {
      setImageChecking(false)
    }
  }

  const cloud = isCloud(chat.chat_base_url)

  const patch = useMemo(() => {
    return buildPatch({
      chat,
      modelEndpoints,
      parserServer,
      imageAnalyzer,
      depth,
      net,
      agents,
    })
  }, [chat, modelEndpoints, parserServer, imageAnalyzer, depth, net, agents])

  useEffect(() => {
    if (firstRun.current) {
      firstRun.current = false
      return
    }

    if (skipNext.current) {
      skipNext.current = false
      return
    }

    onApply?.(patch)
    emitSettingsChanged(patch)

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [patch])

  const resetToDefaults = () => {
    if (!loaded) return

    skipNext.current = true

    for (const field of COOKIE_FIELDS) {
      deleteCookie(settingCookieName(field))
    }

    deleteCookie(LEGACY_PDF_API_COOKIE)

    const nextChat = {
      chat_base_url: loaded.chat_base_url ?? FALLBACK_DEFAULTS.chat_base_url,
      chat_api_key: loaded.chat_api_key ?? 'sk-dummy',
      chat_model: loaded.chat_model ?? FALLBACK_DEFAULTS.chat_model,
      chat_temperature: numberValue(
        loaded.chat_temperature,
        FALLBACK_DEFAULTS.chat_temperature,
      ),
    }

    const nextModelEndpoints = {
      embed_base_url: loaded.embed_base_url ?? FALLBACK_DEFAULTS.embed_base_url,
      embed_model: loaded.embed_model ?? FALLBACK_DEFAULTS.embed_model,
      rerank_base_url: loaded.rerank_base_url ?? FALLBACK_DEFAULTS.rerank_base_url,
      rerank_model: loaded.rerank_model ?? FALLBACK_DEFAULTS.rerank_model,
    }

    const nextParserServer = {
      pdf_parser_api_base:
        loaded.pdf_parser_api_base ?? FALLBACK_DEFAULTS.pdf_parser_api_base,
    }

    const nextImageAnalyzer = {
      pdf_image_base_url:
        loaded.pdf_image_base_url ?? nextChat.chat_base_url,
      pdf_image_api_key:
        loaded.pdf_image_api_key ?? 'sk-dummy',
      pdf_image_model:
        loaded.pdf_image_model ?? nextChat.chat_model,
    }

    const nextDepth = nearestLevel(
      DEPTH,
      'subagent_max_reads',
      loaded.subagent_max_reads ?? FALLBACK_DEFAULTS.subagent_max_reads,
    )

    const nextNet = nearestLevel(
      NET,
      'rerank_top_k',
      loaded.rerank_top_k ?? FALLBACK_DEFAULTS.rerank_top_k,
    )

    const nextAgents = clampInt(
      loaded.subagent_count ?? FALLBACK_DEFAULTS.subagent_count,
      1,
      MAX_AGENTS,
      3,
    )

    setChat(nextChat)
    setModelEndpoints(nextModelEndpoints)
    setParserServer(nextParserServer)
    setParserInput(nextParserServer.pdf_parser_api_base)
    setImageAnalyzer(nextImageAnalyzer)
    setDepthState(nextDepth)
    setNetState(nextNet)
    setAgentsState(nextAgents)
    setParserStatus('')
    setParserError('')
    setImageStatus('')
    setImageError('')

    const resetPatch = buildPatch({
      chat: nextChat,
      modelEndpoints: nextModelEndpoints,
      parserServer: nextParserServer,
      imageAnalyzer: nextImageAnalyzer,
      depth: nextDepth,
      net: nextNet,
      agents: nextAgents,
    })

    onApply?.(resetPatch)
    emitSettingsChanged(resetPatch)
  }

  if (loadErr) {
    return (
      <div className="p-[28px] text-[13px] text-red">
        {t.loadErr(loadErr)}
      </div>
    )
  }

  if (!loaded) {
    return (
      <div className="p-[28px] text-[13px] text-muted">
        {t.loading}
      </div>
    )
  }

  return (
    <div className="h-full overflow-auto bg-white">
      <div className="mx-auto max-w-[760px] px-[28px] py-[26px]">
        <h1 className="m-0 text-[20px] font-extrabold tracking-tight text-ink">
          {t.settings}
        </h1>

        <p className="mt-[6px] text-[13px] text-muted">
          {t.subtitle}
        </p>

        <section className="mt-[22px] border border-line bg-gradient-to-b from-white to-[#fbfdff] p-[18px] shadow-sm">
          <h2 className="m-0 text-[17px] font-extrabold text-ink">
            {t.graphSettings}
          </h2>

          <p className="mt-[4px] text-[12.5px] leading-[1.5] text-muted">
            {t.graphSettingsHint}
          </p>
        </section>

        <section className="mt-[14px] border border-line bg-white p-[18px] shadow-sm">
          <h3 className="m-0 text-[14px] font-extrabold text-ink">
            {t.chatModel}
          </h3>

          <p className="mb-[14px] mt-[3px] text-[12px] text-muted">
            {t.chatHint}
          </p>

          <div className="grid grid-cols-1 gap-[12px]">
            <Text
              label={t.baseUrl}
              value={chat.chat_base_url}
              onChange={(v) => setChatField('chat_base_url', v)}
            />

            <div className="grid grid-cols-1 gap-[12px] md:grid-cols-2">
              <Text
                label={t.model}
                value={chat.chat_model}
                onChange={(v) => setChatField('chat_model', v)}
              />

              <Text
                label={t.apiKey}
                password
                value={chat.chat_api_key}
                onChange={(v) => setChatField('chat_api_key', v)}
              />
            </div>

            <div>
              <Label>
                {t.temperature} ·{' '}
                <b className="text-ink">
                  {Number(chat.chat_temperature).toFixed(1)}
                </b>
              </Label>

              <input
                type="range"
                min={0}
                max={1}
                step={0.1}
                value={chat.chat_temperature}
                onChange={(e) =>
                  setChatField(
                    'chat_temperature',
                    Number(e.target.value),
                  )
                }
                className="w-full accent-blue"
              />
            </div>
          </div>

          {cloud && (
            <div className="mt-[12px] border border-orange/30 bg-[#fff6df] px-[12px] py-[9px] text-[12px] leading-[1.5] text-[#7a4b00]">
              {t.cloudWarn}
            </div>
          )}
        </section>

        <section className="mt-[14px] border border-line bg-white p-[18px] shadow-sm">
          <h3 className="m-0 text-[14px] font-extrabold text-ink">
            {t.modelEndpoints}
          </h3>

          <p className="mb-[14px] mt-[3px] text-[12px] text-muted">
            {t.modelEndpointsHint}
          </p>

          <div className="grid grid-cols-1 gap-[14px]">
            <div>
              <h4 className="mb-[8px] mt-0 text-[13px] font-extrabold text-ink">
                {t.embedding}
              </h4>

              <div className="grid grid-cols-1 gap-[12px]">
                <Text
                  label={t.embeddingBaseUrl}
                  value={modelEndpoints.embed_base_url}
                  onChange={(v) =>
                    setModelEndpointField('embed_base_url', v)
                  }
                />

                <Text
                  label={t.embeddingModel}
                  value={modelEndpoints.embed_model}
                  onChange={(v) =>
                    setModelEndpointField('embed_model', v)
                  }
                />
              </div>
            </div>

            <div>
              <h4 className="mb-[8px] mt-[4px] text-[13px] font-extrabold text-ink">
                {t.reranker}
              </h4>

              <div className="grid grid-cols-1 gap-[12px]">
                <Text
                  label={t.rerankerBaseUrl}
                  value={modelEndpoints.rerank_base_url}
                  onChange={(v) =>
                    setModelEndpointField('rerank_base_url', v)
                  }
                />

                <Text
                  label={t.rerankerModel}
                  value={modelEndpoints.rerank_model}
                  onChange={(v) =>
                    setModelEndpointField('rerank_model', v)
                  }
                />
              </div>
            </div>
          </div>
        </section>

        <section className="mt-[14px] border border-line bg-white p-[18px] shadow-sm">
          <h3 className="m-0 text-[14px] font-extrabold text-ink">
            {t.behaviour}
          </h3>

          <p className="mb-[16px] mt-[3px] text-[12px] text-muted">
            {t.behaviourHint}
          </p>

          <Slider
            title={t.depth}
            levels={t.depthLevels}
            value={depth}
            onChange={setDepth}
            help={t.depthHelp(
              DEPTH[depth].fields.subagent_min_reads,
              DEPTH[depth].fields.subagent_max_reads,
            )}
          />

          <Slider
            title={t.net}
            levels={t.netLevels}
            value={net}
            onChange={setNet}
            help={t.netHelp(
              NET[net].fields.vector_query_k,
              NET[net].fields.rerank_top_k,
            )}
          />

          <Slider
            title={t.subagents}
            levels={Array.from({ length: MAX_AGENTS }, (_, i) =>
              String(i + 1),
            )}
            value={agents - 1}
            onChange={(i) => setAgents(i + 1)}
            help={t.subHelp(agents, Math.min(agents, 4))}
            danger={agents > 2}
          />
        </section>

        <section className="mt-[26px] border border-line bg-gradient-to-b from-white to-[#fbfdff] p-[18px] shadow-sm">
          <h2 className="m-0 text-[17px] font-extrabold text-ink">
            {t.pdfParserSettings}
          </h2>

          <p className="mt-[4px] text-[12.5px] leading-[1.5] text-muted">
            {t.pdfParserSettingsHint}
          </p>
        </section>

        <section className="mt-[14px] border border-line bg-white p-[18px] shadow-sm">
          <h3 className="m-0 text-[14px] font-extrabold text-ink">
            {t.pdfParserServer}
          </h3>

          <p className="mb-[12px] mt-[4px] text-[12px] leading-[1.45] text-muted">
            {t.pdfParserServerHint}
          </p>

          <div className="flex items-center gap-[10px]">
            <input
              type="text"
              value={parserInput}
              disabled={parserChecking}
              onChange={(e) => {
                setParserInput(e.target.value)
                setParserStatus('')
                setParserError('')
              }}
              placeholder={t.pdfParserServerPlaceholder}
              className="min-w-0 flex-1 border border-line bg-soft px-[12px] py-[9px] text-[13px] text-ink outline-none focus:border-blue/50 disabled:cursor-not-allowed disabled:opacity-60"
            />

            <button
              type="button"
              disabled={parserChecking}
              onClick={checkAndSaveParserBase}
              className="border border-blue/30 bg-blue px-[15px] py-[9px] text-[13px] font-extrabold text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
            >
              {parserChecking
                ? t.checkingParserUrl
                : t.checkAndSaveParserUrl}
            </button>
          </div>

          {parserStatus && (
            <div className="mt-[10px] border border-line bg-soft px-[10px] py-[8px] text-[12.5px] text-muted">
              {parserStatus}
            </div>
          )}

          {parserError && (
            <div className="mt-[10px] border border-red/25 bg-red/10 px-[10px] py-[8px] text-[12.5px] text-[#7c1230]">
              {parserError}
            </div>
          )}
        </section>

        <section className="mt-[14px] border border-line bg-white p-[18px] shadow-sm">
          <h3 className="m-0 text-[14px] font-extrabold text-ink">
            {t.imageAnalyzer}
          </h3>

          <p className="mb-[14px] mt-[4px] text-[12px] leading-[1.45] text-muted">
            {t.imageAnalyzerHint}
          </p>

          <div className="grid grid-cols-1 gap-[12px]">
            <Text
              label={t.imageBaseUrl}
              value={imageAnalyzer.pdf_image_base_url}
              onChange={(v) =>
                setImageAnalyzerField('pdf_image_base_url', v)
              }
            />

            <div className="grid grid-cols-1 gap-[12px] md:grid-cols-2">
              <Text
                label={t.imageModel}
                value={imageAnalyzer.pdf_image_model}
                onChange={(v) =>
                  setImageAnalyzerField('pdf_image_model', v)
                }
              />

              <Text
                label={t.imageApiKey}
                password
                value={imageAnalyzer.pdf_image_api_key}
                onChange={(v) =>
                  setImageAnalyzerField('pdf_image_api_key', v)
                }
              />
            </div>

            <div>
              <button
                type="button"
                disabled={imageChecking}
                onClick={checkMultimodalSupport}
                className="border border-blue/30 bg-blue px-[15px] py-[9px] text-[13px] font-extrabold text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
              >
                {imageChecking
                  ? t.checkingMultimodal
                  : t.checkMultimodal}
              </button>
            </div>

            {imageStatus && (
              <div className="border border-emerald-200 bg-emerald-50 px-[10px] py-[8px] text-[12.5px] text-emerald-700">
                {imageStatus}
              </div>
            )}

            {imageError && (
              <div className="border border-red/25 bg-red/10 px-[10px] py-[8px] text-[12.5px] leading-[1.45] text-[#7c1230]">
                {imageError}
              </div>
            )}
          </div>
        </section>

        <div className="mt-[20px]">
          <button
            type="button"
            onClick={resetToDefaults}
            className="rounded-md border border-line bg-white px-[14px] py-[10px] text-[13px] font-bold text-muted hover:text-ink"
          >
            {t.reset}
          </button>
        </div>
      </div>
    </div>
  )
}

function Slider({ title, levels, value, onChange, help, danger }) {
  return (
    <div className="mb-[20px]">
      <div className="mb-[6px] flex items-baseline justify-between">
        <span className="text-[13px] font-bold text-ink">
          {title}
        </span>

        <span
          className={`text-[12px] font-bold ${
            danger ? 'text-red' : 'text-[#244a9d]'
          }`}
        >
          {levels[value]}
        </span>
      </div>

      <input
        type="range"
        min={0}
        max={levels.length - 1}
        step={1}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className={`w-full ${danger ? 'accent-red' : 'accent-blue'}`}
      />

      <div className="mt-[3px] flex justify-between text-[10px] text-muted2">
        {levels.map((l) => (
          <span key={l}>{l}</span>
        ))}
      </div>

      <p className="mt-[6px] text-[12px] text-muted">
        {help}
      </p>
    </div>
  )
}

function Label({ children }) {
  return (
    <span className="mb-[5px] block text-[11.5px] font-bold uppercase tracking-wider text-muted">
      {children}
    </span>
  )
}

function Text({
  label,
  value,
  onChange,
  password,
  placeholder,
}) {
  return (
    <label className="block">
      <Label>{label}</Label>

      <input
        type={password ? 'password' : 'text'}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="h-[36px] w-full border border-line bg-white px-[11px] text-[13px] text-ink outline-none focus:border-blue/50"
      />
    </label>
  )
}
