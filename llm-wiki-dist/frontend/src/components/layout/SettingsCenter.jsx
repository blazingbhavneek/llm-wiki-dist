import { Settings } from 'lucide-react'

import { useT } from '../../i18n.jsx'
import { PageHeader } from './Shell'
import { STR } from './strings.js'

export function SettingsCenter({ children }) {
  const t = useT(STR)

  return (
    <div className="h-full overflow-y-auto bg-gradient-to-b from-white to-[#f8fbff]">
      <div className="mx-auto max-w-[920px] px-6 py-6">
        <PageHeader
          icon={Settings}
          title={t.pages.settingsTitle}
          text={t.pages.settingsText}
        />
        {children}
      </div>
    </div>
  )
}

