import { useT } from '../../i18n.jsx'
import { STR } from './strings.js'

export function AppFooter() {
  const t = useT(STR)

  return (
    <footer className="flex h-[42px] shrink-0 items-center justify-between border-t border-slate-200 bg-white px-5 text-[12px] font-medium text-slate-400">
      <span>{t.shell.footerCopyright('2026')}</span>

      <div className="flex items-center gap-5">
        <button className="hover:text-slate-700">
          {t.shell.terms}
        </button>
        <button className="hover:text-slate-700">
          {t.shell.privacy}
        </button>
      </div>
    </footer>
  )
}


