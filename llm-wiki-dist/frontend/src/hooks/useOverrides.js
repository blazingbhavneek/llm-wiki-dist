import { useEffect, useState } from 'react'

import { api } from '../api'

const COOKIE_PREFIX = 'llm_wiki_setting_'
const LEGACY_PDF_API_COOKIE = 'pdf_parser_api_base'

const FALLBACK_SETTINGS = {
  chat_base_url: 'http://10.160.144.101:51029/v1',
  chat_api_key: '',
  chat_model: 'gemma-4-31B',
  chat_temperature: 0.4,

  embed_base_url: 'http://10.160.144.101:51024/v1',
  embed_model: 'cl-nagoya/ruri-v3-310m',

  rerank_base_url: 'http://10.160.144.101:51025/v1',
  rerank_model: 'cl-nagoya/ruri-v3-reranker-310m',

  pdf_parser_api_base: 'http://10.160.144.101:51023',
  pdf_image_base_url: '',
  pdf_image_api_key: '',
  pdf_image_model: '',

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
  'chat_base_url',
  'chat_api_key',
  'chat_model',
  'chat_temperature',
  'embed_base_url',
  'embed_model',
  'rerank_base_url',
  'rerank_model',
  'depth_level',
  'net_level',
  'subagent_count',
  'pdf_parser_api_base',
  'pdf_image_base_url',
  'pdf_image_api_key',
  'pdf_image_model',
]

const DEPTH = [
  {
    fields: {
      subagent_min_reads: 1,
      subagent_max_reads: 5,
      subagent_max_steps: 10,
      agent_max_steps: 20,
      agent_patience: 10,
    },
  },
  {
    fields: {
      subagent_min_reads: 4,
      subagent_max_reads: 8,
      subagent_max_steps: 16,
      agent_max_steps: 30,
      agent_patience: 15,
    },
  },
  {
    fields: {
      subagent_min_reads: 5,
      subagent_max_reads: 10,
      subagent_max_steps: 20,
      agent_max_steps: 40,
      agent_patience: 20,
    },
  },
  {
    fields: {
      subagent_min_reads: 8,
      subagent_max_reads: 16,
      subagent_max_steps: 26,
      agent_max_steps: 50,
      agent_patience: 25,
    },
  },
  {
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
    fields: {
      vector_query_k: 20,
      search_candidate_pool: 20,
      rerank_top_k: 8,
    },
  },
  {
    fields: {
      vector_query_k: 35,
      search_candidate_pool: 40,
      rerank_top_k: 14,
    },
  },
  {
    fields: {
      vector_query_k: 50,
      search_candidate_pool: 50,
      rerank_top_k: 20,
    },
  },
  {
    fields: {
      vector_query_k: 75,
      search_candidate_pool: 80,
      rerank_top_k: 30,
    },
  },
  {
    fields: {
      vector_query_k: 100,
      search_candidate_pool: 120,
      rerank_top_k: 40,
    },
  },
]

const MAX_AGENTS = 6

function clean(value) {
  return String(value ?? '').trim()
}

function getCookie(name) {
  if (typeof document === 'undefined') return null

  const prefix = `${name}=`
  const row = document.cookie
    .split('; ')
    .find((item) => item.startsWith(prefix))

  if (!row) return null

  return decodeURIComponent(row.slice(prefix.length))
}

function settingCookieName(field) {
  return `${COOKIE_PREFIX}${field}`
}

function getCookieOverrides() {
  const out = {}

  for (const field of COOKIE_FIELDS) {
    let value = getCookie(settingCookieName(field))

    if (field === 'pdf_parser_api_base' && (!value || !clean(value))) {
      value = getCookie(LEGACY_PDF_API_COOKIE)
    }

    if (value === null) continue

    const normalized = clean(value)

    if (normalized) {
      out[field] = value
    }
  }

  return out
}

function numberValue(value, fallback) {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

function clampInt(value, min, max, fallback) {
  const n = Math.round(numberValue(value, fallback))
  return Math.min(max, Math.max(min, n))
}

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

function agentsFields(n) {
  return {
    subagent_count: n,
    subagent_concurrency: Math.min(n, 4),
  }
}

function buildSettings(settings = {}) {
  const merged = {
    ...FALLBACK_SETTINGS,
    ...(settings || {}),
  }

  const chatBaseUrl = clean(merged.chat_base_url) || FALLBACK_SETTINGS.chat_base_url
  const chatModel = clean(merged.chat_model) || FALLBACK_SETTINGS.chat_model

  return {
    ...merged,
    chat_base_url: chatBaseUrl,
    chat_api_key: clean(merged.chat_api_key),
    chat_model: chatModel,
    embed_base_url: clean(merged.embed_base_url) || FALLBACK_SETTINGS.embed_base_url,
    embed_model: clean(merged.embed_model) || FALLBACK_SETTINGS.embed_model,
    rerank_base_url:
      clean(merged.rerank_base_url) || FALLBACK_SETTINGS.rerank_base_url,
    rerank_model: clean(merged.rerank_model) || FALLBACK_SETTINGS.rerank_model,
    pdf_parser_api_base:
      clean(merged.pdf_parser_api_base) || FALLBACK_SETTINGS.pdf_parser_api_base,
    pdf_image_base_url: clean(merged.pdf_image_base_url) || chatBaseUrl,
    pdf_image_api_key: clean(merged.pdf_image_api_key),
    pdf_image_model: clean(merged.pdf_image_model) || chatModel,
  }
}

function buildCookiePatch(baseSettings = FALLBACK_SETTINGS) {
  const cookieOverrides = getCookieOverrides()

  if (Object.keys(cookieOverrides).length === 0) {
    return null
  }

  const defaults = buildSettings(baseSettings)
  const effective = {
    ...defaults,
    ...cookieOverrides,
  }

  const depth =
    effective.depth_level !== undefined
      ? clampInt(effective.depth_level, 0, DEPTH.length - 1, 2)
      : nearestLevel(
          DEPTH,
          'subagent_max_reads',
          effective.subagent_max_reads ?? defaults.subagent_max_reads,
        )

  const net =
    effective.net_level !== undefined
      ? clampInt(effective.net_level, 0, NET.length - 1, 2)
      : nearestLevel(
          NET,
          'rerank_top_k',
          effective.rerank_top_k ?? defaults.rerank_top_k,
        )

  const agents = clampInt(
    effective.subagent_count,
    1,
    MAX_AGENTS,
    defaults.subagent_count ?? 3,
  )

  return {
    ...DEPTH[depth].fields,
    ...NET[net].fields,
    ...agentsFields(agents),

    chat_base_url: clean(effective.chat_base_url) || defaults.chat_base_url,
    chat_api_key: clean(effective.chat_api_key),
    chat_model: clean(effective.chat_model) || defaults.chat_model,
    chat_temperature: numberValue(
      effective.chat_temperature,
      defaults.chat_temperature ?? 0.4,
    ),

    embed_backend: 'server',
    embed_base_url: clean(effective.embed_base_url) || defaults.embed_base_url,
    embed_model: clean(effective.embed_model) || defaults.embed_model,

    rerank_backend: 'server',
    rerank_base_url:
      clean(effective.rerank_base_url) || defaults.rerank_base_url,
    rerank_model: clean(effective.rerank_model) || defaults.rerank_model,

    pdf_parser_api_base:
      clean(effective.pdf_parser_api_base) || defaults.pdf_parser_api_base,
    pdf_image_base_url:
      clean(effective.pdf_image_base_url) || defaults.pdf_image_base_url,
    pdf_image_api_key: clean(effective.pdf_image_api_key),
    pdf_image_model: clean(effective.pdf_image_model) || defaults.pdf_image_model,
  }
}

function syncStoredOverrides(next) {
  if (typeof window === 'undefined') return

  if (next && Object.keys(next).length > 0) {
    window.localStorage.setItem('wikiOverrides', JSON.stringify(next))
    return
  }

  window.localStorage.removeItem('wikiOverrides')
}

export function useOverrides() {
  const [settings, setSettings] = useState(() => buildSettings())
  const [overrides, setOverrides] = useState(() => buildCookiePatch(buildSettings()))

  useEffect(() => {
    let cancelled = false

    api
      .settings()
      .then((backendSettings) => {
        if (cancelled) return

        const nextSettings = buildSettings(backendSettings)
        const nextOverrides = buildCookiePatch(nextSettings)

        setSettings(nextSettings)
        setOverrides(nextOverrides)
        syncStoredOverrides(nextOverrides)
      })
      .catch(() => {
        if (cancelled) return

        const nextSettings = buildSettings()
        const nextOverrides = buildCookiePatch(nextSettings)

        setSettings(nextSettings)
        setOverrides(nextOverrides)
        syncStoredOverrides(nextOverrides)
      })

    return () => {
      cancelled = true
    }
  }, [])

  const applyOverrides = (next) => {
    setOverrides(next)
    syncStoredOverrides(next)
  }

  return { settings, overrides, applyOverrides }
}
