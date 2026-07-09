import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { useT } from '../i18n.jsx'

const COOKIE_PREFIX = 'llm_wiki_setting_'

const COOKIE_FIELDS = [
  'chat_base_url',
  'chat_api_key',
  'chat_model',
  'chat_temperature',

  'embed_base_url',
  'embed_model',

  'rerank_base_url',
  'rerank_model',
]

function getCookie(name) {
  const prefix = `${name}=`
  const row = document.cookie
    .split('; ')
    .find((item) => item.startsWith(prefix))

  if (!row) return null
  return decodeURIComponent(row.slice(prefix.length))
}

function setCookie(name, value, days = 365) {
  const maxAge = days * 24 * 60 * 60
  document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=${maxAge}; SameSite=Lax`
}

function deleteCookie(name) {
  document.cookie = `${name}=; path=/; max-age=0; SameSite=Lax`
}

function settingCookieName(field) {
  return `${COOKIE_PREFIX}${field}`
}

function getCookieOverrides() {
  const out = {}

  for (const field of COOKIE_FIELDS) {
    const value = getCookie(settingCookieName(field))

    if (value !== null) {
      out[field] = value
    }
  }

  return out
}

const STR = {
  ja: {
    loadErr: (e) => `設定を読み込めませんでした: ${e}`,
    loading: '設定を読み込み中…',
    settings: '設定',
    subtitle: 'エージェントの検索と回答の方法を調整します。',

    chatModel: 'チャットモデル',
    chatHint:
      'チャット用 LLM の接続先を設定します。変更した値はこのブラウザーの Cookie に保存され、次回以降も優先して使われます。',
    baseUrl: 'ベース URL',
    model: 'モデル',
    apiKey: 'API キー',
    temperature: '温度',

    modelEndpoints: 'モデルエンドポイント',
    modelEndpointsHint:
      '埋め込みモデルと再ランカーの接続先を設定します。変更した値はこのブラウザーの Cookie に保存され、次回以降も優先して使われます。',
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
    reset: 'デフォルトに戻す',

    depthLevels: ['クイック', '軽量', '標準', '深い', '徹底的'],
    netLevels: ['狭い', '絞り込み', 'バランス', '広い', '非常に広い'],
  },
  en: {
    loadErr: (e) => `Could not load settings: ${e}`,
    loading: 'Loading settings…',
    settings: 'Settings',
    subtitle: 'Tune how the agent searches and answers.',

    chatModel: 'Chat model',
    chatHint:
      'Configure the chat LLM endpoint. Edited values are saved in this browser using cookies and will be preferred next time.',
    baseUrl: 'Base URL',
    model: 'Model',
    apiKey: 'API key',
    temperature: 'Temperature',

    modelEndpoints: 'Model endpoints',
    modelEndpointsHint:
      'Configure the embedding model and reranker endpoints. Edited values are saved in this browser using cookies and will be preferred next time.',
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
    reset: 'Reset to defaults',

    depthLevels: ['Quick', 'Light', 'Standard', 'Deep', 'Exhaustive'],
    netLevels: ['Narrow', 'Focused', 'Balanced', 'Wide', 'Very wide'],
  },
}

// Research depth: how many nodes each sub-agent must read + its loop budget.
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

// Search net: how wide each search casts.
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

const agentsFields = (n) => ({
  subagent_count: n,
  subagent_concurrency: Math.min(n, 4),
})

const MAX_AGENTS = 6

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

function isCloud(url = '') {
  return /openai|anthropic|api\.groq|googleapis|azure/i.test(url)
}

function clean(value) {
  return String(value ?? '').trim()
}

function buildPatch({ chat, modelEndpoints, depth, net, agents }) {
  const out = {
    ...DEPTH[depth].fields,
    ...NET[net].fields,
    ...agentsFields(agents),
  }

  out.chat_base_url = clean(chat.chat_base_url)
  out.chat_api_key = clean(chat.chat_api_key)
  out.chat_model = clean(chat.chat_model)
  out.chat_temperature = Number(chat.chat_temperature)

  out.embed_backend = 'server'
  out.embed_base_url = clean(modelEndpoints.embed_base_url)
  out.embed_model = clean(modelEndpoints.embed_model)

  out.rerank_backend = 'server'
  out.rerank_base_url = clean(modelEndpoints.rerank_base_url)
  out.rerank_model = clean(modelEndpoints.rerank_model)

  return out
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

  const [depth, setDepth] = useState(2)
  const [net, setNet] = useState(2)
  const [agents, setAgents] = useState(3)

  const [loadErr, setLoadErr] = useState(null)

  // Skip only the hydration render. After that, every edit applies instantly.
  const firstRun = useRef(true)
  const skipNext = useRef(false)

  useEffect(() => {
    api
      .settings()
      .then((defaults) => {
        setLoaded(defaults)

        const cookieOverrides = getCookieOverrides()

        // Priority:
        // cookie value > parent overrides > backend settings defaults
        const effective = {
          ...defaults,
          ...(overrides || {}),
          ...cookieOverrides,
        }

        const nextChat = {
          chat_base_url: effective.chat_base_url ?? '',
          chat_api_key: effective.chat_api_key ?? '',
          chat_model: effective.chat_model ?? '',
          chat_temperature: Number(
            effective.chat_temperature ?? defaults.chat_temperature ?? 0.4,
          ),
        }

        const nextModelEndpoints = {
          embed_base_url: effective.embed_base_url ?? '',
          embed_model: effective.embed_model ?? '',
          rerank_base_url: effective.rerank_base_url ?? '',
          rerank_model: effective.rerank_model ?? '',
        }

        const nextDepth = nearestLevel(
          DEPTH,
          'subagent_max_reads',
          effective.subagent_max_reads ?? 10,
        )

        const nextNet = nearestLevel(
          NET,
          'rerank_top_k',
          effective.rerank_top_k ?? 20,
        )

        const nextAgents = Math.min(
          MAX_AGENTS,
          Math.max(1, effective.subagent_count ?? 3),
        )

        setChat(nextChat)
        setModelEndpoints(nextModelEndpoints)
        setDepth(nextDepth)
        setNet(nextNet)
        setAgents(nextAgents)

        // If cookie overrides exist, apply them immediately so the saved
        // browser settings are actually used without requiring another edit.
        if (Object.keys(cookieOverrides).length > 0) {
          onApply(
            buildPatch({
              chat: nextChat,
              modelEndpoints: nextModelEndpoints,
              depth: nextDepth,
              net: nextNet,
              agents: nextAgents,
            }),
          )
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

  const cloud = isCloud(chat.chat_base_url)

  const patch = useMemo(() => {
    return buildPatch({
      chat,
      modelEndpoints,
      depth,
      net,
      agents,
    })
  }, [chat, modelEndpoints, depth, net, agents])

  useEffect(() => {
    if (firstRun.current) {
      firstRun.current = false
      return
    }

    if (skipNext.current) {
      skipNext.current = false
      return
    }

    onApply(patch)

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [patch])

  const resetToDefaults = () => {
    if (!loaded) return

    skipNext.current = true
    onApply(null)

    for (const field of COOKIE_FIELDS) {
      deleteCookie(settingCookieName(field))
    }

    setChat({
      chat_base_url: loaded.chat_base_url ?? '',
      chat_api_key: loaded.chat_api_key ?? '',
      chat_model: loaded.chat_model ?? '',
      chat_temperature: Number(loaded.chat_temperature ?? 0.4),
    })

    setModelEndpoints({
      embed_base_url: loaded.embed_base_url ?? '',
      embed_model: loaded.embed_model ?? '',
      rerank_base_url: loaded.rerank_base_url ?? '',
      rerank_model: loaded.rerank_model ?? '',
    })

    setDepth(
      nearestLevel(
        DEPTH,
        'subagent_max_reads',
        loaded.subagent_max_reads ?? 10,
      ),
    )

    setNet(
      nearestLevel(
        NET,
        'rerank_top_k',
        loaded.rerank_top_k ?? 20,
      ),
    )

    setAgents(
      Math.min(
        MAX_AGENTS,
        Math.max(1, loaded.subagent_count ?? 3),
      ),
    )
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
      <div className="mx-auto max-w-[720px] px-[28px] py-[26px]">
        <h1 className="m-0 text-[20px] font-extrabold tracking-tight text-ink">
          {t.settings}
        </h1>

        <p className="mt-[6px] text-[13px] text-muted">
          {t.subtitle}
        </p>

        {/* LLM endpoint */}
        <section className="mt-[22px] border border-line bg-white p-[18px] shadow-sm">
          <h2 className="m-0 text-[14px] font-extrabold text-ink">
            {t.chatModel}
          </h2>

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

        {/* Embedding / reranker endpoints */}
        <section className="mt-[18px] border border-line bg-white p-[18px] shadow-sm">
          <h2 className="m-0 text-[14px] font-extrabold text-ink">
            {t.modelEndpoints}
          </h2>

          <p className="mb-[14px] mt-[3px] text-[12px] text-muted">
            {t.modelEndpointsHint}
          </p>

          <div className="grid grid-cols-1 gap-[14px]">
            <div>
              <h3 className="mb-[8px] mt-0 text-[13px] font-extrabold text-ink">
                {t.embedding}
              </h3>

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
              <h3 className="mb-[8px] mt-[4px] text-[13px] font-extrabold text-ink">
                {t.reranker}
              </h3>

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

        {/* Tuning sliders */}
        <section className="mt-[18px] border border-line bg-white p-[18px] shadow-sm">
          <h2 className="m-0 text-[14px] font-extrabold text-ink">
            {t.behaviour}
          </h2>

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

        <div className="mt-[20px]">
          <button
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

function Text({ label, value, onChange, password }) {
  return (
    <label className="block">
      <Label>{label}</Label>

      <input
        type={password ? 'password' : 'text'}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-[36px] w-full border border-line bg-white px-[11px] text-[13px] text-ink outline-none focus:border-blue/50"
      />
    </label>
  )
}
