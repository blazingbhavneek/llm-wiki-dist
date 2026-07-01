// Tiny i18n: a shared language context + a per-file dictionary helper.
//
// There is NO global strings dictionary. Each component keeps its own
//   const STR = { ja: {...}, en: {...} }
// next to its JSX and reads the active map via `useT(STR)`. Missing `en` keys
// fall back to `ja`, so a component is never blank if a translation is absent.
//
// Language is persisted per-browser; default is Japanese.
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
} from 'react'

const LANG_KEY = 'wikiLang'
const DEFAULT_LANG = 'ja'

const LangContext = createContext({ lang: DEFAULT_LANG, setLang: () => {} })

export function LangProvider({ children }) {
  const [lang, setLangState] = useState(() => {
    try {
      return window.localStorage.getItem(LANG_KEY) || DEFAULT_LANG
    } catch {
      return DEFAULT_LANG
    }
  })
  const setLang = useCallback((next) => {
    setLangState(next)
    try {
      window.localStorage.setItem(LANG_KEY, next)
    } catch {
      /* ignore */
    }
  }, [])
  const value = useMemo(() => ({ lang, setLang }), [lang, setLang])
  return <LangContext.Provider value={value}>{children}</LangContext.Provider>
}

export function useLang() {
  return useContext(LangContext)
}

// Resolve a per-file { ja, en } dict to the active-language map, with ja fallback.
export function useT(dict) {
  const { lang } = useLang()
  return useMemo(() => {
    const base = dict.ja || {}
    const active = dict[lang] || base
    return { ...base, ...active }
  }, [dict, lang])
}

// EN / 日本語 toggle. Drop anywhere; reads/writes the shared context.
export function LangToggle({ className = '' }) {
  const { lang, setLang } = useLang()
  const btn = (code, label) => (
    <button
      type="button"
      onClick={() => setLang(code)}
      className={`px-[9px] py-[4px] text-[11px] font-bold transition-colors ${
        lang === code ? 'bg-blue text-white' : 'bg-white text-muted hover:text-ink'
      }`}
    >
      {label}
    </button>
  )
  return (
    <div
      className={`flex overflow-hidden rounded border border-line shadow-sm ${className}`}
    >
      {btn('ja', '日本語')}
      {btn('en', 'EN')}
    </div>
  )
}
