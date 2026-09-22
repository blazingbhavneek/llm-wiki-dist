import { useT } from '../../i18n.jsx'
import { STR } from './strings.js'

export function AppFooter() {
  const t = useT(STR)

  return (
    <footer className="grid h-[42px] shrink-0 grid-cols-[1fr_auto_1fr] items-center border-t border-neutral-200 bg-white px-5 text-[11px] font-medium text-neutral-400">
      <span>{t.shell.footerCopyright('2026')}</span>

      <span className="text-center">{t.shell.disclaimer}</span>

      <div className="flex items-center justify-end gap-5">
        <button className="hover:text-neutral-700">{t.shell.terms}</button>
        <button className="hover:text-neutral-700">{t.shell.privacy}</button>
      </div>
    </footer>
  )
}
