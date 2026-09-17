import { useEffect, useMemo, useState } from 'react'

import { api } from '../api'
import { useT } from '../i18n.jsx'

const STR = {
  ja: {
    loading: '設定を読み込み中…',
    loadErr: (e) => `設定を読み込めませんでした: ${e}`,
    chatModel: 'チャットモデル',
    chatHint: '回答に使う LLM の接続先です。API キー以外はこのブラウザーに保存されます。',
    agent: 'エージェントの振る舞い',
    agentHint: '探索量を増やすほど回答は詳しくなりますが、時間と LLM 呼び出しも増えます。',
    baseUrl: 'ベース URL',
    model: 'モデル',
    apiKey: 'API キー',
    temperature: '温度',
    depth: 'リサーチの深さ',
    depthHelp: (a, b) => `各エクスプローラーは回答前に ${a}〜${b} 個のノードを読みます。`,
    net: '検索の網',
    netHelp: (k, top) => `各検索は Elasticsearch 候補 ${k} 件と索引カード ${top} 件を再ランクします。`,
    subagents: 'サブエージェント',
    subHelp: (n, at) => `${n} 個の並列エクスプローラー（同時最大 ${at}）。`,
    cloudWarn: '⚠ クラウドエンドポイントです。探索量を増やすと費用も増える可能性があります。',
    reset: 'デフォルトに戻す',
    depthLevels: ['クイック', '軽量', '標準', '深い', '徹底的'],
    netLevels: ['狭い', '絞り込み', 'バランス', '広い', '非常に広い'],
  },
  en: {
    loading: 'Loading settings…',
    loadErr: (e) => `Could not load settings: ${e}`,
    chatModel: 'Chat model',
    chatHint: 'The LLM endpoint used for answers. Everything except the API key is saved in this browser.',
    agent: 'Agent behaviour',
    agentHint: 'More exploration can improve answers, but uses more time and LLM calls.',
    baseUrl: 'Base URL',
    model: 'Model',
    apiKey: 'API key',
    temperature: 'Temperature',
    depth: 'Research depth',
    depthHelp: (a, b) => `Each explorer reads ${a}–${b} nodes before answering.`,
    net: 'Search breadth',
    netHelp: (k, top) => `Each search reranks ${k} Elasticsearch candidates and ${top} index cards.`,
    subagents: 'Sub-agents',
    subHelp: (n, at) => `${n} parallel explorers (up to ${at} concurrent).`,
    cloudWarn: '⚠ This is a cloud endpoint. More exploration may increase cost.',
    reset: 'Reset to defaults',
    depthLevels: ['Quick', 'Light', 'Standard', 'Deep', 'Exhaustive'],
    netLevels: ['Narrow', 'Focused', 'Balanced', 'Wide', 'Very wide'],
  },
}

const FALLBACK_DEFAULTS = {
  chat_base_url: '',
  chat_api_key: '',
  chat_model: '',
  chat_temperature: 0.2,
  subagent_min_reads: 1,
  subagent_max_reads: 4,
  subagent_max_steps: 20,
  agent_max_steps: 40,
  agent_patience: 20,
  search_candidates: 30,
  rerank_top_k: 8,
  index_map_top_k: 20,
  subagent_count: 2,
  subagent_concurrency: 2,
}

const COOKIE_PREFIX = 'llm_wiki_setting_'
const COOKIE_FIELDS = ['chat_base_url', 'chat_model', 'chat_temperature', 'depth_level', 'net_level', 'subagent_count']
const DEPTH = [
  { fields: { subagent_min_reads: 1, subagent_max_reads: 3, subagent_max_steps: 10, agent_max_steps: 20, agent_patience: 10 } },
  { fields: { subagent_min_reads: 1, subagent_max_reads: 4, subagent_max_steps: 16, agent_max_steps: 30, agent_patience: 15 } },
  { fields: { subagent_min_reads: 2, subagent_max_reads: 6, subagent_max_steps: 20, agent_max_steps: 40, agent_patience: 20 } },
  { fields: { subagent_min_reads: 3, subagent_max_reads: 10, subagent_max_steps: 26, agent_max_steps: 50, agent_patience: 25 } },
  { fields: { subagent_min_reads: 4, subagent_max_reads: 16, subagent_max_steps: 32, agent_max_steps: 60, agent_patience: 30 } },
]
const NET = [
  { fields: { search_candidates: 15, rerank_top_k: 6, index_map_top_k: 8 } },
  { fields: { search_candidates: 25, rerank_top_k: 8, index_map_top_k: 14 } },
  { fields: { search_candidates: 30, rerank_top_k: 12, index_map_top_k: 20 } },
  { fields: { search_candidates: 40, rerank_top_k: 20, index_map_top_k: 30 } },
  { fields: { search_candidates: 50, rerank_top_k: 30, index_map_top_k: 40 } },
]
const MAX_AGENTS = 6

function getCookie(name) {
  if (typeof document === 'undefined') return null
  const prefix = `${name}=`
  const row = document.cookie.split('; ').find((item) => item.startsWith(prefix))
  return row ? decodeURIComponent(row.slice(prefix.length)) : null
}

function setCookie(name, value) {
  if (typeof document === 'undefined') return
  document.cookie = `${name}=${encodeURIComponent(String(value))}; path=/; max-age=31536000; SameSite=Lax`
}

function clean(value) {
  return String(value ?? '').trim()
}

function nearestLevel(table, field, value) {
  const target = Number(value)
  if (!Number.isFinite(target)) return 0
  return table.reduce((best, item, index) => {
    const distance = Math.abs(Number(item.fields[field]) - target)
    return distance < best.distance ? { index, distance } : best
  }, { index: 0, distance: Infinity }).index
}

const agentsFields = (n) => ({ subagent_count: n, subagent_concurrency: Math.min(n, 4) })

function buildPatch({ chat, depth, net, agents }) {
  return {
    ...DEPTH[depth].fields,
    ...NET[net].fields,
    ...agentsFields(agents),
    chat_base_url: clean(chat.chat_base_url),
    // The server key is never sent to the browser; only override it when the user typed one.
    ...(clean(chat.chat_api_key) ? { chat_api_key: clean(chat.chat_api_key) } : {}),
    chat_model: clean(chat.chat_model),
    chat_temperature: Number(chat.chat_temperature),
  }
}

export default function SettingsView({ overrides, onApply }) {
  const t = useT(STR)
  const [defaults, setDefaults] = useState(FALLBACK_DEFAULTS)
  const [chat, setChat] = useState(FALLBACK_DEFAULTS)
  const [depth, setDepth] = useState(0)
  const [net, setNet] = useState(0)
  const [agents, setAgents] = useState(2)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [initialized, setInitialized] = useState(false)

  useEffect(() => {
    let live = true
    api.settings().then((server) => {
      if (!live) return
      const base = { ...FALLBACK_DEFAULTS, ...(server || {}) }
      const cookieValues = Object.fromEntries(
        COOKIE_FIELDS.map((field) => [field, getCookie(`${COOKIE_PREFIX}${field}`)]).filter(([, value]) => value !== null),
      )
      const values = { ...base, ...cookieValues, ...(overrides || {}) }
      setDefaults(base)
      setChat(values)
      setDepth(nearestLevel(DEPTH, 'subagent_max_reads', values.subagent_max_reads))
      setNet(nearestLevel(NET, 'rerank_top_k', values.rerank_top_k))
      setAgents(Math.max(1, Math.min(MAX_AGENTS, Number(values.subagent_count) || 2)))
      setLoading(false)
      setInitialized(true)
    }).catch((e) => {
      if (!live) return
      setError(e.message)
      setLoading(false)
      setInitialized(true)
    })
    return () => { live = false }
  }, [])

  const patch = useMemo(() => buildPatch({ chat, depth, net, agents }), [chat, depth, net, agents])

  useEffect(() => {
    if (!initialized) return
    onApply?.(patch)
    for (const field of COOKIE_FIELDS) {
      const value = field === 'depth_level' ? depth : field === 'net_level' ? net : field === 'subagent_count' ? agents : patch[field]
      setCookie(`${COOKIE_PREFIX}${field}`, value)
    }
    // onApply is intentionally omitted: its parent callback is not stateful configuration.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialized, patch, depth, net, agents])

  const cloud = /openai|anthropic|api\.groq|googleapis|azure/i.test(chat.chat_base_url || '')
  const reset = () => {
    setChat(defaults)
    setDepth(nearestLevel(DEPTH, 'subagent_max_reads', defaults.subagent_max_reads))
    setNet(nearestLevel(NET, 'rerank_top_k', defaults.rerank_top_k))
    setAgents(Math.max(1, Math.min(MAX_AGENTS, Number(defaults.subagent_count) || 2)))
  }

  if (loading) return <p className="rounded-xl border border-neutral-200 bg-white p-5 text-[13px] text-neutral-500">{t.loading}</p>
  if (error) return <p className="rounded-xl border border-red-200 bg-red-50 p-5 text-[13px] text-red-700">{t.loadErr(error)}</p>

  return (
    <div className="space-y-5">
      <section className="rounded-2xl border border-neutral-200 bg-white p-5 shadow-sm">
        <h2 className="text-[16px] font-semibold text-neutral-950">{t.chatModel}</h2>
        <p className="mt-1 text-[13px] leading-5 text-neutral-500">{t.chatHint}</p>
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <Text label={t.baseUrl} value={chat.chat_base_url} onChange={(v) => setChat((x) => ({ ...x, chat_base_url: v }))} />
          <Text label={t.model} value={chat.chat_model} onChange={(v) => setChat((x) => ({ ...x, chat_model: v }))} />
          <Text label={t.apiKey} type="password" value={chat.chat_api_key} onChange={(v) => setChat((x) => ({ ...x, chat_api_key: v }))} />
          <Slider label={t.temperature} min="0" max="2" step="0.05" value={chat.chat_temperature} onChange={(v) => setChat((x) => ({ ...x, chat_temperature: v }))} />
        </div>
        {cloud && <p className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] font-semibold text-amber-800">{t.cloudWarn}</p>}
      </section>

      <section className="rounded-2xl border border-neutral-200 bg-white p-5 shadow-sm">
        <h2 className="text-[16px] font-semibold text-neutral-950">{t.agent}</h2>
        <p className="mt-1 text-[13px] leading-5 text-neutral-500">{t.agentHint}</p>
        <div className="mt-5 space-y-6">
          <LevelSlider label={t.depth} value={depth} labels={t.depthLevels} onChange={setDepth} />
          <p className="-mt-4 text-[12px] text-neutral-500">{t.depthHelp(DEPTH[depth].fields.subagent_min_reads, DEPTH[depth].fields.subagent_max_reads)}</p>
          <LevelSlider label={t.net} value={net} labels={t.netLevels} onChange={setNet} />
          <p className="-mt-4 text-[12px] text-neutral-500">{t.netHelp(NET[net].fields.search_candidates, NET[net].fields.index_map_top_k)}</p>
          <Slider label={t.subagents} min="1" max={MAX_AGENTS} step="1" value={agents} onChange={(v) => setAgents(Math.max(1, Math.min(MAX_AGENTS, Number(v))))} />
          <p className="-mt-4 text-[12px] text-neutral-500">{t.subHelp(agents, Math.min(agents, 4))}</p>
        </div>
      </section>

      <button type="button" onClick={reset} className="rounded-lg border border-neutral-300 bg-white px-3 py-2 text-[12px] font-bold text-neutral-600 hover:bg-neutral-50">{t.reset}</button>
    </div>
  )
}

function LevelSlider({ label, value, labels, onChange }) {
  return (
    <label className="block">
      <div className="mb-2 flex items-center justify-between gap-3 text-[13px] font-bold text-neutral-700">
        <span>{label}</span><span className="text-blue-700">{labels[value]}</span>
      </div>
      <input className="w-full accent-blue-600" type="range" min="0" max={labels.length - 1} step="1" value={value} onChange={(e) => onChange(Number(e.target.value))} />
    </label>
  )
}

function Slider({ label, value, onChange, ...props }) {
  return (
    <label className="block">
      <div className="mb-2 flex items-center justify-between gap-3 text-[13px] font-bold text-neutral-700"><span>{label}</span><span className="text-blue-700">{value}</span></div>
      <input className="w-full accent-blue-600" type="range" value={value} onChange={(e) => onChange(e.target.value)} {...props} />
    </label>
  )
}

function Text({ label, value, onChange, type = 'text' }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[12px] font-bold text-neutral-600">{label}</span>
      <input type={type} value={value ?? ''} onChange={(e) => onChange(e.target.value)} className="h-10 w-full rounded-lg border border-neutral-300 bg-white px-3 text-[13px] text-neutral-800 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100" />
    </label>
  )
}
