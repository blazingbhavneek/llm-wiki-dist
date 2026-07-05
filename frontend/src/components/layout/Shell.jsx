import { Sparkles } from 'lucide-react'

export function PlaceholderPage({ icon: Icon = Sparkles, title, text }) {
  return (
    <div className="grid h-full place-items-center bg-gradient-to-b from-white to-[#f8fbff] px-6">
      <div className="max-w-[460px] rounded-2xl border border-dashed border-slate-300 bg-white p-8 text-center shadow-sm">
        <div className="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-blue-50 text-blue-700">
          <Icon size={24} />
        </div>
        <h1 className="mt-4 text-[22px] font-extrabold tracking-tight text-slate-950">
          {title}
        </h1>
        <p className="mt-2 text-[14px] leading-6 text-slate-500">
          {text}
        </p>
      </div>
    </div>
  )
}

export function PageHeader({ icon: Icon, title, text, aside }) {
  return (
    <div className="mb-5 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-center gap-3">
        <div className="grid h-11 w-11 shrink-0 place-items-center rounded-2xl bg-blue-50 text-blue-700">
          <Icon size={22} />
        </div>

        <div className="min-w-0 flex-1">
          <h1 className="truncate text-[20px] font-extrabold tracking-tight text-slate-950">
            {title}
          </h1>
          <p className="mt-1 text-[13px] leading-5 text-slate-500">
            {text}
          </p>
        </div>

        {aside && (
          <div className="shrink-0 rounded-full border border-blue-200 bg-blue-50 px-3 py-1.5 text-[12px] font-extrabold text-blue-700">
            {aside}
          </div>
        )}
      </div>
    </div>
  )
}

export function Centered({ children }) {
  return (
    <div className="grid h-full place-items-center bg-white text-[14px] text-slate-500">
      {children}
    </div>
  )
}

