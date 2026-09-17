import {
  MessageCircle,
  PanelLeftClose,
  PanelLeftOpen,
  PlusCircle,
  Settings,
} from 'lucide-react'

import { faviconUrl } from '../../data/utils'
import { useT } from '../../i18n.jsx'
import { STR } from './strings.js'

export function LeftSidebar({
  collapsed,
  activeView,
  activeRightTabId,
  rightOpen,
  recentQuestions,
  onToggle,
  onNavigate,
  onNewChat,
}) {
  const t = useT(STR)

  const items = [
    { id: 'chat', label: t.shell.chat, icon: MessageCircle, view: 'chat' },
  ]

  return (
    <aside
      className={`flex h-full shrink-0 flex-col border-r border-neutral-200 bg-white transition-[width] duration-300 ${
        collapsed ? 'w-[76px]' : 'w-[240px]'
      }`}
    >
      <div className="border-b border-neutral-100 px-3 py-4">
        <div
          className={`flex items-center gap-3 ${
            collapsed ? 'justify-center' : ''
          }`}
        >
          <div className="flex w-full justify-center">
            <img
              src={faviconUrl()}
              alt="Logo"
              className="block h-[100px] w-[100px] max-w-full object-contain"
            />
          </div>

          {/* {!collapsed && (
            <div className="min-w-0 flex-1">
              <div className="truncate text-[17px] font-semibold tracking-tight text-neutral-950">
                {t.brand}
              </div>
              <div className="truncate text-[11px] font-medium text-neutral-400">
                {t.brandSubtitle}
              </div>
            </div>
          )} */}
        </div>

        <button
          onClick={onToggle}
          className={`mt-3 grid h-8 place-items-center rounded-lg text-neutral-500 hover:bg-neutral-100 hover:text-neutral-900 ${
            collapsed ? 'mx-auto w-10' : 'w-full'
          }`}
          title={collapsed ? t.shell.expandSidebar : t.shell.collapseSidebar}
          aria-label={collapsed ? t.shell.expandSidebar : t.shell.collapseSidebar}
        >
          {collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
        </button>
      </div>

      <nav className="flex-1 overflow-y-auto px-3 py-4">
        <div className="space-y-1">
          {items.map((item) => {
            const Icon = item.icon
            const active =
              item.view === 'explorer'
                ? rightOpen && activeRightTabId === 'explorer'
                : activeView === item.view

            const content = (
              <>
                <Icon
                  size={18}
                  className={
                    active
                      ? 'text-blue-600'
                      : 'text-neutral-500 group-hover:text-neutral-800'
                  }
                />

                {!collapsed && <span className="truncate">{item.label}</span>}
              </>
            )

            if (item.href) {
              return (
                <a
                  key={item.id}
                  href={item.href}
                  target="_blank"
                  rel="noreferrer"
                  className={`group flex h-10 w-full items-center gap-3 rounded-xl px-3 text-left text-[14px] font-semibold text-neutral-600 transition hover:bg-neutral-100 hover:text-neutral-950 ${collapsed ? 'justify-center' : ''}`}
                  title={item.label}
                >
                  {content}
                </a>
              )
            }

            return (
              <button
                key={item.id}
                onClick={() => onNavigate(item.view)}
                className={`group flex h-10 w-full items-center gap-3 rounded-xl px-3 text-left text-[14px] font-semibold transition ${
                  active
                    ? 'bg-blue-50 text-blue-700'
                    : 'text-neutral-600 hover:bg-neutral-100 hover:text-neutral-950'
                } ${collapsed ? 'justify-center' : ''}`}
                title={collapsed ? item.label : undefined}
                aria-label={item.label}
              >
                {content}
              </button>
            )
          })}
        </div>

        {/* {!collapsed && (
          <div className="mt-8">
            <div className="mb-3 flex items-center justify-between px-1 text-[12px] font-bold text-neutral-500">
              <span>{t.shell.recentQuestions}</span>
              <button className="text-neutral-900 hover:text-neutral-900">
                {t.shell.viewAll}
              </button>
            </div>

            <div className="space-y-1">
              {recentQuestions.length === 0 && (
                <div className="rounded-xl border border-dashed border-neutral-200 p-3 text-[12px] leading-5 text-neutral-400">
                  {t.shell.noRecentQuestions}
                </div>
              )}

              {recentQuestions.map((q, index) => (
                <button
                  key={`${q.text}-${index}`}
                  className="flex w-full items-start gap-2 rounded-lg px-2 py-2 text-left text-[12px] leading-5 text-neutral-600 hover:bg-neutral-50 hover:text-neutral-900"
                >
                  <MessageCircle
                    size={14}
                    className="mt-[2px] shrink-0 text-neutral-400"
                  />
                  <span className="line-clamp-2">{q.text}</span>
                </button>
              ))}
            </div>
          </div>
        )} */}
      </nav>

      <div className="border-t border-neutral-100 p-3">
        <button
          onClick={onNewChat}
          className={`flex h-10 w-full items-center justify-center gap-2 rounded-xl border border-blue-200 bg-blue-50 text-[13px] font-bold text-blue-700 transition hover:bg-blue-100 ${
            collapsed ? 'px-0' : 'px-3'
          }`}
          title={collapsed ? t.shell.newChat : undefined}
          aria-label={t.shell.newChat}
        >
          <PlusCircle size={17} />
          {!collapsed && <span>{t.shell.newChat}</span>}
        </button>

        <button
          onClick={() => onNavigate('settings')}
          className={`mt-2 flex h-10 w-full items-center justify-center gap-2 rounded-xl text-[13px] font-semibold text-neutral-500 transition hover:bg-neutral-100 hover:text-neutral-900 ${
            activeView === 'settings' ? 'bg-blue-50 text-blue-700' : ''
          }`}
          title={collapsed ? t.shell.settings : undefined}
          aria-label={t.shell.settings}
        >
          <Settings size={17} />
          {!collapsed && <span>{t.shell.settings}</span>}
        </button>
      </div>
    </aside>
  )
}
